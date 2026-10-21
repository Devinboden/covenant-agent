"""Test the code-driven loop: round 1 (= one-shot) vs final, per deal and model, plus a missing-context test.

  A. loop on all 7 deals x models: problems + missing clauses after round 1 vs after the loop; rounds, escalations,
     cost; answer-key parts found (by quote position, 5 deals with keys)
  B. --strip-context: give the model NO referenced definitions. Does it ask (needs_context / search_for) or guess?

Usage: python -m covenant_engine.abstract.test_loop --models haiku sonnet [--strip TRS ROAD QUAD]
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor

from ..ingest.exceptions import squash
from ..core.run_round import TERMS, key_parts, nearest, positions
from ..ingest.run import ROOT
from .decompose import Bundle, run


def key_found(borrower, comps):
    kp = key_parts(borrower, TERMS[borrower])
    if not kp:
        return None
    live_sq = Bundle(borrower).live_sq
    kpos = positions([k["quote"] for k in kp], live_sq)
    mpos = positions([c["quote"] for c in comps], live_sq)
    return sum(1 for p in kpos if nearest(p, mpos) is not None), len(kp)


def job(args):
    b, m, strip = args
    res = run(b, TERMS[b], model=m, strip_context=strip)
    if "error" in res:
        return {"deal": b, "model": m, "strip": strip, "error": res["error"]}
    t0 = res["trace"][0]
    return {"deal": b, "model": m, "strip": strip,
            "r1_problems": len(t0.get("problems", {})), "r1_missing": len(t0.get("missing", [])),
            "r1_requested": t0.get("requested", {}),
            "final_review": res["review"], "rounds": len(res["trace"]),
            "models": [t["model"] for t in res["trace"]], "cost": res["cost"], "seconds": res["seconds"],
            "key": key_found(b, res["components"]), "n_components": len(res["components"]),
            "code_clauses": len(res["code_clauses"]), "starts_from": res["starts_from"], "result": res}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["haiku", "sonnet"])
    ap.add_argument("--deals", nargs="+", default=list(TERMS))
    ap.add_argument("--strip", nargs="*", default=[])
    ap.add_argument("--out", default="loop_run.json")
    a = ap.parse_args()
    jobs = [(b, m, False) for b in a.deals for m in a.models] + [(b, m, True) for b in a.strip for m in a.models]
    with ThreadPoolExecutor(8) as ex:
        R = list(ex.map(job, jobs))
    outdir = ROOT / "data" / "abstract"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / a.out).write_text(json.dumps(R, indent=1), encoding="utf-8")
    for r in R:
        if "error" in r:
            print(r["deal"], r["model"], "ERROR", r["error"]); continue
        rev = [x for x in r["final_review"] if "custom" not in x["why"]]
        cust = len(r["final_review"]) - len(rev)
        print(f"{r['deal']:5} {r['model']:6} {'STRIP ' if r['strip'] else ''}r1 prob {r['r1_problems']} miss {r['r1_missing']} "
              f"-> final open {len(rev)} (+{cust} custom) | rounds {r['rounds']} {r['models']} | key {r['key']} | "
              f"${r['cost']} {r['seconds']}s | asked {r['r1_requested']}")
        for x in rev:
            print("      open:", x["clause"], "-", x["why"][:140])


if __name__ == "__main__":
    main()
