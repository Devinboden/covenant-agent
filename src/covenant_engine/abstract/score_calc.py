"""Score calculation rules (#21) and deemed values (#22) against facts in the answer keys.
Test period is scored in the Loan section (it lives in the covenant text, not the calculation sections).
Rules: the rule TYPES each key records must be present (plus details where the key gives them). Deemed: exact values.

Usage: python -m covenant_engine.abstract.score_calc --runs data/abstract/calc
"""
import argparse
import glob
import json

from ..ingest.run import ROOT

RULES = {"WEST": [("pro_forma_transactions", "10000000")],
         "ROAD": [("rounding", None), ("debt_at_principal_or_face", None), ("pro_forma_transactions", None)],
         "TRS": [("pro_forma_transactions", None)],
         "CARS": [("pro_forma_transactions", None), ("post_period_events", None)],
         "MEC": [("debt_at_principal_or_face", None), ("lease_accounting", None)]}
DEEMED = {"WEST": {("borrower", "2021-09-30", "11462000"), ("borrower", "2021-12-31", "11544000"),
                   ("borrower", "2022-03-31", "10420000"), ("borrower", "2022-06-30", "12471000")},
          "ROAD": set(), "TRS": set(), "CARS": {("schedule", "Schedule 1.01(c)", False)},
          "MEC": {("Accu-Fab, LLC", "2024-12-31", "3242517"), ("Accu-Fab, LLC", "2025-03-31", "3691078"),
                  ("Accu-Fab, LLC", "2025-06-30", "5011409")}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    a = ap.parse_args()
    for r in sorted(glob.glob(str(ROOT / a.runs / "run*"))):
        ok = n = dok = dn = 0
        misses = []
        for b, want in RULES.items():
            res = json.loads(open(f"{r}/{b}.calc.json").read())
            rules = res["output"]["rules"]
            for t, th in want:
                n += 1
                hits = [x for x in rules if x["type"] == t]
                good = bool(hits) and (th is None or any(x["threshold"].replace(",", "") == th for x in hits))
                ok += good
                if not good:
                    misses.append(f"{b} {t}{' threshold ' + th if th else ''}: got {[(x['type'], x['threshold']) for x in rules]}")
            got = {(x["entity"], x["period_end"], x["value"]) if x["source"] == "definition_text"
                   else ("schedule", x["schedule"], x["schedule_provided"]) for x in res["deemed"]}
            dn += 1
            dok += got == DEEMED[b]
            if got != DEEMED[b]:
                misses.append(f"{b} deemed: got {sorted(got)} want {sorted(DEEMED[b])}")
        print(f"{r.split('/')[-1]}: rules {ok}/{n} | deemed values exact {dok}/{dn} deals")
        for m in misses:
            print("    -", m)


if __name__ == "__main__":
    main()
