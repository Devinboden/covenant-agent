"""Spike: can a model decompose a custom term (e.g. Adjusted Consolidated EBITDA) into library parts?

Input  : intake bundle only (the term's definition + the definitions it references). Nothing from other steps.
Model  : `claude -p --model <opus|sonnet|haiku> --json-schema` (one call).
Code   : verifies every quote against the live agreement text, then scores against the hand-built answer key
         clause by clause (found? category? sign? cap type/value/schedule?).

Usage: python -m covenant_engine.core.decompose_term WEST "Adjusted Consolidated EBITDA" --model opus
"""
import argparse
import json
import re
import time
from pathlib import Path

import yaml

from ..ingest.exceptions import call_model, squash
from ..ingest.packet import NO_EXPAND
from ..ingest.run import CURRENT, ROOT

CATEGORIES = [  # parts library (component categories) — drawn from all 5 deals, plus a 'custom' escape hatch
    "interest", "taxes", "depreciation_amortization", "other_non_cash_charges", "nci_income", "nci_losses",
    "sponsor_management_fees", "receivables_facility_losses", "cash_receipts_from_prior_non_cash_gains",
    "equity_plan_costs", "non_cash_compensation", "pension_amortization", "cost_savings_implementation",
    "restructuring_business_optimization", "transition_closure_costs", "third_party_reimbursed_expenses",
    "public_company_costs", "litigation_regulatory", "transaction_costs", "fx_losses", "fx_gains",
    "quality_of_earnings_adjustments", "projected_savings", "non_cash_gains", "acquired_ebitda", "disposed_ebitda",
    "extraordinary_unusual_items", "impairment_writedowns", "insurance_recoveries", "discontinued_operations",
    "earnouts_contingent_consideration", "asset_sale_gains_losses", "debt_extinguishment", "hedging_mark_to_market",
    "equity_cure", "custom"]
CAP_TYPES = ["none", "pct_of_ebitda", "fixed_amount_per_period", "shared_pool", "lifetime_amount", "per_transaction", "other"]

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["starts_from", "components"],
          "properties": {
              "starts_from": {"type": "string"},
              "components": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False,
                  "required": ["clause", "category", "sign", "quote", "cap_type", "cap_value", "cap_schedule",
                               "cap_shared_with", "cap_basis", "cap_quote", "needs_judgment", "note"],
                  "properties": {
                      "clause": {"type": "string"},
                      "category": {"type": "string", "enum": CATEGORIES},
                      "sign": {"type": "string", "enum": ["+", "-"]},
                      "quote": {"type": "string"},
                      "cap_type": {"type": "string", "enum": CAP_TYPES},
                      "cap_value": {"type": "string"},
                      "cap_schedule": {"type": "array", "items": {
                          "type": "object", "additionalProperties": False,
                          "required": ["period_ending_from", "period_ending_to", "value"],
                          "properties": {"period_ending_from": {"type": "string"}, "period_ending_to": {"type": "string"},
                                         "value": {"type": "string"}}}},
                      "cap_shared_with": {"type": "array", "items": {"type": "string"}},
                      "cap_basis": {"type": "string", "enum": ["after_capped_items", "before_capped_items", "not_stated", "n/a"]},
                      "cap_quote": {"type": "string"},
                      "needs_judgment": {"type": "boolean"},
                      "note": {"type": "string"}}}}}}

SYSTEM = ("You decompose defined terms in commercial credit agreements into components from a fixed parts library. "
          "Work only from the text given. Every quote must be copied exactly, character for character, from the text.")

PROMPT = """Decompose the defined term “{term}” into its components.

Rules:
- One component per lettered/numbered clause that adds, deducts or includes/excludes an amount (e.g. (a)(xii), (b), (c)(ii), proviso (I)). Never merge two clauses; split a clause only if it has separately capped parts (e.g. (xv)(x) and (xv)(y)).
- clause: the clause reference as written, e.g. "(a)(xii)".
- category: pick the closest part from the enum; use "custom" if none fits without bending its meaning.
- sign: "+" adds to the term, "-" deducts from it (provisos that exclude an amount are "-").
- quote: the opening words of that clause (8-30 words), exact copy.
- cap: if the clause is limited, give cap_type, cap_value (e.g. "0.15" for 15%, "2500000" for $2.5M; "" if none), cap_schedule for caps that change by date (one row per period, dates as YYYY-MM-DD or "" if open; value as a decimal), cap_shared_with = other clauses sharing the same limit, cap_basis = whether the % is measured after or before giving effect to the capped items, and cap_quote = exact words stating the cap.
- needs_judgment: true if the clause depends on judgment words ("reasonably", "factually supportable", "good faith", "acceptable to the Agent").
- starts_from: the base amount the term starts from (e.g. "Consolidated Net Income").
- Decompose only “{term}” itself. The referenced definitions are context.

DEFINITION OF “{term}”:
<<<
{definition}
>>>

REFERENCED DEFINITIONS (context):
<<<
{context}
>>>"""


HOLDOUT = {"QUAD": "data/pdf/QUAD/credit_agreement_a-r-2025-08.pdf", "SRI": "data/pdf/SRI/credit_agreement_2026-03.pdf"}


DEALS = ROOT / "data" / "deals.json"          # deals added through the single entry point (engine/abstract/new_deal.py)


def deal_pdf(borrower):
    extra = json.loads(DEALS.read_text(encoding="utf-8")) if DEALS.exists() else {}
    return {**CURRENT, **HOLDOUT, **extra}[borrower]


def bundle_dir(borrower):
    return ROOT / "data" / "intake" / borrower / Path(deal_pdf(borrower)).stem


def build_input(borrower, term, max_ctx_chars=60000):
    bundle = bundle_dir(borrower)
    defs = json.loads((bundle / "definitions.json").read_text(encoding="utf-8"))
    d = defs["defined"][term]
    ctx, used = [], 0
    for r in d.get("refs", []):
        if r in NO_EXPAND or r not in defs["defined"]:
            continue
        t = defs["defined"][r]["text"][:6000]
        if used + len(t) > max_ctx_chars:
            break
        ctx.append(t)
        used += len(t)
    for name, inl in defs["inline"].items():         # terms defined in passing inside this definition
        if name in d["text"] and name not in d.get("refs", []):
            pass
    live = (bundle / "text_live.txt").read_text(encoding="utf-8")
    return d["text"], "\n\n".join(ctx), live


CLAUSE_OF_ID = lambda cid: cid  # answer-key ids look like ebitda.a.xii -> "(a)(xii)"


def key_clause(cid):
    parts = cid.split(".")[1:]
    if parts[0] == "proviso":
        return f"({parts[1]})"
    if parts[0] == "equity_cure":
        return "equity_cure"
    return "".join(f"({p})" for p in parts if p not in ("projected_savings",))


def norm_clause(c):
    c = c.replace(" ", "").lower()
    c = re.sub(r"^proviso", "", c)
    return c


def score(borrower, term, out, live):
    ab = yaml.safe_load((ROOT / "data" / "abstracts" / f"{borrower}.yaml").read_text(encoding="utf-8"))
    key_term = next(t for t in ab["terms"] if t["name"] == term)
    key = {norm_clause(key_clause(c["id"])): c for c in key_term["components"] if not c["id"].endswith("equity_cure")}
    got = {}
    for c in out["components"]:
        got.setdefault(norm_clause(c["clause"]), c)
    rows, live_sq = [], squash(live)
    for k, kc in key.items():
        g = got.get(k)
        row = {"clause": k, "key_category": kc["category"], "found": bool(g)}
        if g:
            kcap = kc.get("cap") or {}
            if isinstance(kcap, str) and kcap.strip().lower() in ("none", "no cap", "n/a", ""):
                kcap = {}                                   # "none" is not a cap
            if not isinstance(kcap, dict):                  # agent-built keys sometimes write caps as prose
                kcap = {"type": "prose", "text": str(kcap)}
            sched_key = []
            for r in kcap.get("schedule", []) if isinstance(kcap.get("schedule"), list) else []:
                try:
                    sched_key.append((str(r.get("from") or ""), str(r.get("to") or ""), float(r["value"])))
                except (AttributeError, KeyError, TypeError, ValueError):
                    pass
            sched_got = []
            for r in g["cap_schedule"]:
                try:
                    sched_got.append((r["period_ending_from"], r["period_ending_to"], float(r["value"])))
                except ValueError:
                    pass
            row.update({
                "model_category": g["category"], "category_ok": g["category"] == kc["category"],
                "sign_ok": g["sign"] == kc["sign"],
                "quote_verified": squash(g["quote"]) in live_sq,
                "cap_expected": bool(kcap), "cap_found": g["cap_type"] != "none",
                "cap_value_ok": (not kcap) or _cap_value_ok(kcap, g),
                "schedule_ok": (not sched_key) or [v for _, _, v in sched_key] == [v for _, _, v in sched_got[-len(sched_key):]],
                "judgment": g["needs_judgment"]})
        rows.append(row)
    extra = [c["clause"] for k, c in got.items() if k not in key]
    return rows, extra


def _cap_value_ok(kcap, g):
    if kcap.get("schedule"):
        return True                                    # checked by schedule_ok
    try:
        return abs(float(g["cap_value"]) - float(kcap["value"])) < 1e-6
    except (ValueError, TypeError, KeyError):
        return None                                    # key cap not machine-comparable -> review by hand


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("borrower")
    ap.add_argument("term")
    ap.add_argument("--model", default="opus")
    a = ap.parse_args()
    definition, context, live = build_input(a.borrower, a.term)
    prompt = PROMPT.format(term=a.term, definition=definition, context=context)
    t0 = time.time()
    r = call_model(SYSTEM, prompt, SCHEMA, model=a.model, timeout=900)
    if not r["ok"]:
        print("model call failed:", r.get("error"))
        return
    out = r["output"]
    outdir = ROOT / "data" / "spike"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"_raw_{a.borrower}_{a.model}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")   # save before scoring
    rows, extra = score(a.borrower, a.term, out, live)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = f"{a.borrower}_{re.sub(r'[^A-Za-z]+', '_', a.term)}_{a.model}"
    (outdir / f"{stem}.json").write_text(json.dumps({"model_call": {k: v for k, v in r.items() if k != "output"},
                                                    "output": out, "score": rows, "extra": extra}, indent=1), encoding="utf-8")
    n = len(rows)
    f = lambda k: sum(1 for x in rows if x.get(k))
    print(f"{a.borrower} · {a.term} · {r['model']} · ${r['cost_usd']} · {r['seconds']}s · prompt ~{len(prompt) // 4:,} tok")
    print(f"  starts_from: {out['starts_from']!r}")
    print(f"  clauses in key: {n} | found {f('found')} | category ok {f('category_ok')} | sign ok {f('sign_ok')} | "
          f"quote verified {f('quote_verified')} | caps expected {sum(x.get('cap_expected', False) for x in rows)} "
          f"found {sum(1 for x in rows if x.get('cap_expected') and x.get('cap_found'))} value ok "
          f"{sum(1 for x in rows if x.get('cap_expected') and x.get('cap_value_ok') and x.get('schedule_ok'))}")
    print(f"  extra clauses (not in key): {extra}")
    for x in rows:
        if not x["found"] or not all(x.get(k, True) for k in ("category_ok", "sign_ok", "quote_verified", "cap_value_ok", "schedule_ok")):
            print("   ", {k: v for k, v in x.items() if k in ("clause", "key_category", "found", "model_category", "category_ok",
                                                          "sign_ok", "quote_verified", "cap_value_ok", "schedule_ok")})


if __name__ == "__main__":
    main()
