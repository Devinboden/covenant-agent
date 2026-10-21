"""Diagnose WHY a model gets a part wrong: repeat runs (systematic vs noise) + one-change counterfactuals (which change
makes the error go away = the cause). Single calls, no repair loop, so we see raw model behaviour.

Cases (the three silent material errors found by the v1 keys):
  A  TRS Total Net Leverage Ratio: cash cap for the 6.13 covenant ($240M) vs general ($150M)
  B  TRS Total Indebtedness: clause (a) (on-balance-sheet debt) labelled as a scope qualifier
  C  MEC Unrestricted Cash and Cash Equivalents: one-sentence definition decomposed into one part

Usage: python -m covenant_engine.abstract.diagnose [--n 5]
"""
import argparse
import copy
import json
from concurrent.futures import ThreadPoolExecutor

from ..ingest.exceptions import call_model
from ..ingest.run import ROOT
from . import decompose as D

QUAL = D.QUALIFIERS


def prompt_for(borrower, term, family, extra_rule="", library_add=(), library_drop=()):
    b = D.Bundle(borrower)
    definition, context, _ = D.context_for(b, term)
    rules = D.RULES.replace("{family_rules}", D.FAMILY_RULES[family] + ("\n" + extra_rule if extra_rule else ""))
    schema = copy.deepcopy(D.schema_for(family))
    enum = schema["properties"]["components"]["items"]["properties"]["category"]["enum"]
    enum[:] = [c for c in enum if c not in library_drop] + list(library_add)
    p = D.FIRST.format(term=term, used_by="", rules=rules, definition=definition, context=context, rules_text=D.calc_rules(b) or "(none)")
    return p, schema


def judge_A(out):
    c = next((c for c in out["components"] if c["category"] in ("numerator_less_term", "numerator_less_amount")), None)
    v = "|".join(f"{cap['value']}@{cap['covenant']}" for cap in (c or {}).get("caps", []))
    return {"ok": "240000000" in v.replace(",", ""), "value": v, "note_mentions_240": "240" in (c or {}).get("note", "")}


def judge_B(out):
    a = [c for c in out["components"] if D.norm(c["clause"]) == "(a)"]
    return {"ok": any(c["category"] not in QUAL for c in a), "value": [c["category"] for c in a]}


def judge_C(out):
    cats = [c["category"] for c in out["components"]]
    base = any(c["category"] in ("unrestricted_cash", "cash_equivalents") and c["sign"] == "+" for c in out["components"])
    return {"ok": base and len(cats) >= 4, "value": cats}


CASES = {
    "A": ("TRS", "Total Net Leverage Ratio", "ratio", judge_A, {
        "baseline": {},
        "told_covenant": {"extra_rule": "- You are decomposing this term for the covenant in Section 6.13. Where the text gives a "
                                        "value that applies specifically to that covenant, use that value."},
        "two_value_slot": {"extra_rule": "- If the text gives different values for different covenants, put both in cap_value as "
                                         "'general|covenant-specific' and name the covenant's section in note."}}),
    "B": ("TRS", "Total Indebtedness", "debt", judge_B, {
        "baseline": {},
        "add_on_balance_sheet_part": {"library_add": ["on_balance_sheet_debt"]},
        "drop_scope_part": {"library_drop": ["scope_entities"]}}),
    "C": ("MEC", "Unrestricted Cash and Cash Equivalents", "cash", judge_C, {
        "baseline": {},
        "unclaused_instruction": {"extra_rule": "- If the definition has no lettered or numbered clauses, give one component per "
                                                "amount it counts and one per condition or exclusion it applies (label them "
                                                "'base', 'condition 1', ...)."}}),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--model", default="sonnet")
    a = ap.parse_args()
    jobs = []
    for cid, (b, term, fam, judge, variants) in CASES.items():
        for vname, kw in variants.items():
            p, schema = prompt_for(b, term, fam, **kw)
            jobs += [(cid, vname, p, schema, judge)] * a.n

    def run(job):
        cid, vname, p, schema, judge = job
        r = call_model(D.SYSTEM, p, schema, model=a.model, timeout=900)
        res = judge(r["output"]) if r["ok"] else {"ok": False, "value": "CALL FAILED"}
        return cid, vname, res, r.get("cost_usd") or 0

    with ThreadPoolExecutor(8) as ex:
        R = list(ex.map(run, jobs))
    out, cost = {}, sum(x[3] for x in R)
    for cid, vname, res, _ in R:
        out.setdefault(cid, {}).setdefault(vname, []).append(res)
    (ROOT / "data" / "keys_v1" / "diagnose.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"${cost:.2f}")
    for cid, vs in out.items():
        b, term = CASES[cid][0], CASES[cid][1]
        print(f"{cid} {b} {term}")
        for vname, rs in vs.items():
            extra = ""
            if cid == "A":
                extra = f" | note mentions $240M in {sum(r.get('note_mentions_240', False) for r in rs)}/{len(rs)}"
            print(f"   {vname:28} correct {sum(r['ok'] for r in rs)}/{len(rs)}{extra} | {[r['value'] for r in rs]}"[:400])


if __name__ == "__main__":
    main()
