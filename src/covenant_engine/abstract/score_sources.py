"""Score the 'source' field (#19). There is no answer key for "where would this number come from", so this reports:
  plausible  - the source is one of the acceptable ones for the part's category (SOURCE_OK), n/a for structural parts
  stable     - for parts seen in every run (same term, clause, category), how often all runs give the same source
  mix        - how sources are distributed (how much would need the borrower / isn't public)

Usage: python -m covenant_engine.abstract.score_sources --runs data/abstract/s19 WEST TRS MEC
"""
import argparse
import glob
import json
from collections import Counter, defaultdict

from ..ingest.run import ROOT
from .clauses import norm
from .decompose import NA_CATS, SOURCE_OK


def plausible(c):
    cat, src = c["category"], c["source"]
    if cat in NA_CATS:
        return src == "n/a"
    if cat in SOURCE_OK:
        return src in SOURCE_OK[cat]
    return src != "n/a" or cat == "custom"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("deals", nargs="+")
    ap.add_argument("--runs", required=True)
    a = ap.parse_args()
    runs = sorted(glob.glob(str(ROOT / a.runs / "run*"))) or [str(ROOT / a.runs)]
    seen = defaultdict(list)
    for r in runs:
        ok = n = 0
        mix, bad = Counter(), []
        for b in a.deals:
            ab = json.loads(open(f"{r}/{b}.abstract.json").read())
            for t in ab["terms"]:
                for c in t["components"]:
                    n += 1
                    good = plausible(c)
                    ok += good
                    mix[c["source"]] += 1
                    seen[(b, t["name"], norm(c["clause"]), c["category"])].append(c["source"])
                    if not good:
                        bad.append(f"{b} {t['name'][:28]} {c['clause']} {c['category']} -> {c['source']}")
        print(f"{r.split('/')[-1]}: plausible {ok}/{n} ({ok / n:.0%}) | mix {dict(mix.most_common())}")
        for x in bad[:12]:
            print("    implausible:", x)
    multi = {k: v for k, v in seen.items() if len(v) == len(runs) and len(runs) > 1}
    if multi:
        same = sum(len(set(v)) == 1 for v in multi.values())
        print(f"stable across {len(runs)} runs: {same}/{len(multi)} parts ({same / len(multi):.0%}) give the same source every run")
        flips = Counter(tuple(sorted(set(v))) for v in multi.values() if len(set(v)) > 1)
        print("   most common flips:", flips.most_common(6))


if __name__ == "__main__":
    main()
