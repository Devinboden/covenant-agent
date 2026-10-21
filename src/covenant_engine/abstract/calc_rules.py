"""Calculation rules (#21) and deemed historical values (#22).

#21: Sonnet records the global calculation rules as typed rules (pro forma treatment, GAAP / lease freezes, debt at
     principal or face value, rounding, test period, post-period events, ...) from the Article I calculation sections,
     the Pro Forma definitions and the pro forma paragraphs inside the EBITDA definition. Code checks quotes, thresholds,
     covenant refs.
#22: CODE ONLY. Deemed values follow fixed wording: "$X for the (fiscal) quarter ended <date>" next to "deemed" /
     "shall equal"; a deemed table on a schedule ("set forth on Schedule 1.01(c)") is recorded with whether the
     schedule was provided (intake's missing-attachments list). Unparsed "deemed" wording goes to review.

Usage: python -m covenant_engine.abstract.calc_rules WEST
"""
import argparse
import json
import re
from datetime import datetime

from ..ingest.exceptions import call_model, squash
from ..ingest.packet import CALC_RULES
from ..ingest.run import ROOT
from ..core.decompose_term import bundle_dir
from .decompose import family_of

RULE_TYPES = ["pro_forma_transactions", "pro_forma_cost_savings", "test_period", "gaap_freeze", "lease_accounting",
              "debt_at_principal_or_face", "rounding", "currency_translation", "limited_condition_transaction",
              "post_period_events", "unrestricted_subsidiaries", "other"]
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["rules", "flags"],
          "properties": {"rules": {"type": "array", "items": {
              "type": "object", "additionalProperties": False,
              "required": ["type", "applies_to", "summary", "threshold", "conditions", "section", "quote"],
              "properties": {"type": {"type": "string", "enum": RULE_TYPES},
                             "applies_to": {"type": "string"}, "summary": {"type": "string"},
                             "threshold": {"type": "string"}, "conditions": {"type": "array", "items": {"type": "string"}},
                             "section": {"type": "string"}, "quote": {"type": "string"}}}},
              "flags": {"type": "array", "items": {"type": "string"}}}}
SYSTEM = ("You record the calculation rules of a credit agreement as typed rules. Work only from the text given. Every quote "
          "must be copied exactly. Never compute.")
PROMPT = """Record every rule about HOW the financial covenants are calculated (not what the terms contain):
- pro_forma_transactions: acquisitions / dispositions / debt incurrence treated as if on the first day of the period
  (threshold = a size below which it doesn't apply, e.g. "10000000"); pro_forma_cost_savings: run-rate savings allowed in
  pro forma calculations (conditions: time limit, certification);
- test_period: what period / date each covenant is measured over;
- gaap_freeze: GAAP as of a date, or changes neutralised until amended; lease_accounting: operating vs finance lease
  treatment frozen; debt_at_principal_or_face: debt counted at principal / face, ignoring fair-value accounting;
- rounding; currency_translation; limited_condition_transaction; post_period_events: whether transactions after the
  period end count in the actual covenant test; unrestricted_subsidiaries: excluded from the calculations; other.
applies_to: "all", or the covenant sections it applies to ({covenants}). summary: one short sentence. section: where
the rule is (e.g. "1.04" or "definition of Pro Forma Basis"). quote: exact words (8-40 words) stating the rule.

TEXT:
<<<
{text}
>>>"""

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DEEMED_VAL = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s+(?:for|in respect of)\s+the\s+(?:fiscal\s+)?quarter\s+end(?:ed|ing)\s+"
                        r"(?:on\s+)?((?:" + MONTHS + r")\s+\d{1,2},\s+\d{4})", re.I)
DEEMED_CUE = re.compile(r"deemed (?:to be )?(?:equal|to equal)|shall (?:be deemed to )?equal|shall be deemed", re.I)
SCHED = re.compile(r"(?:deemed|shall equal)[^.;]{0,150}?(Schedule\s+[\d.]+(?:\([a-z]\))?)|"
                   r"(Schedule\s+[\d.]+(?:\([a-z]\))?)[^.;]{0,80}?(?:deemed|shall equal)", re.I)
ENTITY = re.compile(r"EBITDA of ([A-Z][\w.&\-]*(?:,? [A-Z][\w.&\-]*)*)(?: and its (?:Restricted )?Subsidiaries)? shall")


def gather(borrower):
    d = bundle_dir(borrower)
    P = json.loads((d / "packet.json").read_text(encoding="utf-8"))
    blocks = [json.loads(l) for l in (d / "blocks.jsonl").read_text(encoding="utf-8").splitlines()]
    defs = json.loads((d / "definitions.json").read_text(encoding="utf-8"))["defined"]
    parts = [f"[Section {c['id']} {c.get('title', '')}]\n{c['text']}" for c in P if c["kind"] == "calc_rules"]
    seen = {c["id"] for c in P if c["kind"] == "calc_rules"}
    # Article I sections the packet's title filter doesn't catch (ROAD 1.6 "Rounding", currency, times of day ...)
    S = json.loads((d / "sections.json").read_text(encoding="utf-8"))
    for s in S:
        if s["id"].split(".")[0] == "1" and s["id"] not in seen and re.search(r"Rounding|Currenc|Calculation|Pro Forma|"
                                                                               r"Accounting|Ratio|Limited Condition", s.get("title") or ""):
            txt = "\n".join(blocks[k]["text_live"] for k in range(s["start"], s["end"]) if blocks[k]["text_live"])
            if len(txt) < 20000:
                parts.append(f"[Section {s['id']} {s.get('title', '')}]\n{txt}")
                seen.add(s["id"])
    # calc-rule headings run together inside a block (SRI 6.3 / 6.5): pull those blocks too
    for b in blocks:
        t = b["text_live"] or ""
        if re.search(r"\d+\.\d+\.?\s+(Pro Forma Calculations|Accounting (Terms|Principles)|Ratio Calculations|Certain "
                     r"Calculations)", t) and not any(f"Section {s}" in t[:20] for s in seen) and len(t) < 30000 \
                and t not in "\n".join(parts):
            parts.append(f"[Block {b['id']}]\n{t}")
    for name, v in defs.items():
        if re.search(r"Pro Forma|Test Period|Reference Period|Measurement Period|Limited Condition", name) and len(v["text"]) < 8000:
            parts.append(f"[Definition] {v['text']}")
    for name, v in defs.items():                      # pro forma paragraphs inside the EBITDA definitions
        if family_of(name) == "ebitda":
            for para in re.split(r"(?<=\.)\s+(?=[A-Z“])", v["text"]):
                if re.search(r"pro forma|Pro Forma", para) and len(para) > 80:
                    parts.append(f"[From the definition of {name}] {para}")
    return "\n\n".join(parts), squash((d / "text_live.txt").read_text(encoding="utf-8")), defs


def deemed_values(borrower, defs):
    """#22, code only."""
    report = json.loads((bundle_dir(borrower) / "report.json").read_text(encoding="utf-8"))
    missing = {re.sub(r"\s+", " ", m) for m in report["attachments_referenced_in_packet_but_missing"]}
    out, review = [], []
    for name, v in defs.items():
        if family_of(name) != "ebitda":
            continue
        for sent in re.split(r"(?<=[.;])\s+(?=[A-Z“(])", v["text"]):
            if not DEEMED_CUE.search(sent):
                continue
            vals = DEEMED_VAL.findall(sent)
            ent = ENTITY.search(sent)
            if vals:
                for amt, d in vals:
                    entity = ent.group(1).strip(", ") if ent and ent.group(1) not in ("Borrower", "Parent Borrower") else "borrower"
                    out.append({"term": name, "entity": entity,
                                "period_end": datetime.strptime(d.replace("  ", " "), "%B %d, %Y").date().isoformat(),
                                "value": amt.replace(",", ""), "source": "definition_text", "quote": sent[:300]})
            else:
                sm = SCHED.search(sent)
                if sm:
                    ref = re.sub(r"\s+", " ", sm.group(1) or sm.group(2))
                    out.append({"term": name, "entity": "borrower", "period_end": "", "value": "", "source": "schedule",
                                "schedule": ref, "schedule_provided": ref not in missing, "quote": sent[:300]})
                    if ref in missing:
                        review.append(f"deemed values for {name} are on {ref}, which is not in the filed documents: "
                                      f"obtain the schedule if any test period still uses them")
                elif re.search(r"(deemed|shall equal)[^.;]{0,150}\$\s?[\d,]{5,}", sent, re.I):
                    review.append(f"deemed wording in {name} that code couldn't parse: {sent[:160]!r}")
    return out, review


def check(out, live_sq, covenant_refs):
    P = []
    refs = {r.split("(")[0] for r in covenant_refs}
    for r in out["rules"]:
        w = f"rule {r['type']}"
        if r["quote"] and squash(r["quote"])[:80] not in live_sq:
            P.append(f"{w}: quote not found verbatim in the agreement: {r['quote'][:70]!r}")
        if r["threshold"]:
            try:
                float(r["threshold"].replace(",", ""))
            except ValueError:
                P.append(f"{w}: threshold {r['threshold']!r} must be a bare number")
        if r["applies_to"].strip().lower() not in ("all", ""):
            for s in re.findall(r"\d+\.\d+[\d.]*", r["applies_to"]):
                if s.split("(")[0] not in refs and not any(x.startswith(s) or s.startswith(x) for x in refs):
                    P.append(f"{w}: applies_to {s} is not a covenant section {sorted(refs)}")
    return P


def run(borrower, covenant_refs, model="sonnet", max_repairs=2):
    text, live_sq, defs = gather(borrower)
    deemed, deemed_review = deemed_values(borrower, defs)
    prompt = PROMPT.format(covenants=", ".join(covenant_refs), text=text)
    trace = []
    r = call_model(SYSTEM, prompt, SCHEMA, model=model, timeout=900)
    trace.append({k: r.get(k) for k in ("model", "cost_usd", "seconds", "ok")})
    if not r["ok"]:
        return {"error": r.get("error"), "trace": trace, "deemed": deemed}
    out = r["output"]
    for _ in range(max_repairs):
        probs = check(out, live_sq, covenant_refs)
        trace[-1]["problems"] = probs
        if not probs:
            break
        r = call_model(SYSTEM, prompt + "\n\nYOUR PREVIOUS ANSWER FAILED THESE CHECKS; return the complete corrected answer:\n"
                       + "\n".join(f"- {p}" for p in probs) + "\n\nPREVIOUS ANSWER:\n" + json.dumps(out), SCHEMA,
                       model=model, timeout=900)
        trace.append({k: r.get(k) for k in ("model", "cost_usd", "seconds", "ok")})
        if not r["ok"]:
            break
        out = r["output"]
    probs = check(out, live_sq, covenant_refs)
    trace[-1]["problems"] = probs
    return {"output": out, "deemed": deemed, "review": probs + deemed_review, "trace": trace, "input_chars": len(text),
            "cost": round(sum(t["cost_usd"] or 0 for t in trace), 4), "seconds": round(sum(t["seconds"] or 0 for t in trace), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("borrower")
    a = ap.parse_args()
    ab = json.loads((ROOT / "data" / "abstract" / "v1" / f"{a.borrower}.abstract.json").read_text(encoding="utf-8"))
    print(json.dumps(run(a.borrower, [c["ref"] for c in ab["covenants"]]), indent=1)[:6000])


if __name__ == "__main__":
    main()
