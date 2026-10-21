"""Run-to-run variance: score N independent builds (data/abstract/variance/run*) against the v1 keys and EBITDA caps,
report each metric's range, and list the parts whose verdict flips between runs.

Usage: python -m covenant_engine.abstract.variance WEST TRS MEC
"""
import glob
import sys
from io import StringIO
from contextlib import redirect_stdout

from ..ingest.run import ROOT
from . import score_caps
from .score_keys import score

METRICS = ["found", "category", "sign", "ref", "tcap"]


def main():
    args = sys.argv[1:]
    folder = "data/abstract/variance"
    if args and args[0].startswith("--runs="):
        folder = args.pop(0).split("=", 1)[1]
    deals = args or ["WEST", "TRS", "MEC"]
    runs = sorted(glob.glob(str(ROOT / folder / "run*")))
    per_run, diff_sets, cap_runs = [], [], []
    for r in runs:
        rel = r[len(str(ROOT)) + 1:]
        tot, diffs = {}, set()
        for b in deals:
            _, _, d, t = score(b, rel)
            for k, v in t.items():
                tot[k] = tot.get(k, 0) + v
            diffs |= {f"{b} {x}" for x in d}
        per_run.append(tot)
        diff_sets.append(diffs)
        c = {}
        for b in deals:
            res, _ = score_caps.score(b, rel)
            for k, v in res.items():
                c[k] = c.get(k, 0) + v
        cap_runs.append(c)
    print(f"{len(runs)} runs, deals {deals}")
    den = {"found": "parts", "category": "found", "sign": "found", "ref": "ref_n", "cap": "cap_n", "tcap": "tcap_n"}
    for m in METRICS:
        vals = [f"{t[m]}/{t[den[m]]}" for t in per_run]
        pct = [t[m] / t[den[m]] for t in per_run if t[den[m]]]
        print(f"  {m:9} {'  '.join(vals)}   range {min(pct):.0%}-{max(pct):.0%}")
    print("  EBITDA caps: " + "  ".join(f"type {c['type']}/{c['caps']} values {c['value']}/{c['value_n']} basis {c['basis']}/{c['basis_n']}"
                                         for c in cap_runs))
    always = set.intersection(*diff_sets)
    flips = set.union(*diff_sets) - always
    print(f"\nIn every run ({len(always)}): systematic")
    for d in sorted(always):
        print("   ", d[:150])
    print(f"\nIn some runs only ({len(flips)}): noise")
    for d in sorted(flips):
        print(f"    [{sum(d in s for s in diff_sets)}/{len(runs)}] {d[:140]}")


if __name__ == "__main__":
    main()
