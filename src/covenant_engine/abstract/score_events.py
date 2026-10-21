"""Score Events (#23) against facts in the answer keys. Regime state at 2026-06-30 and the Q2 2026 reported ratios
and thresholds (keys' replay 'reported' blocks). Regime ids differ by run, so regimes are matched by type/name words.

Usage: python -m covenant_engine.abstract.score_events --runs data/abstract/events
"""
import argparse
import glob
import json

from ..ingest.run import ROOT

# (deal, covenant ref, value, threshold) from the keys' Q2 2026 reported blocks
REPORTED = [("ROAD", "9.16(a)", 3.17, 4.75), ("ROAD", "9.16(b)", 5.37, 2.75), ("CARS", "7.11(a)", 0.08, 3.5),
            ("CARS", "7.11(b)", 7.2, 3.0), ("MEC", "9.15(a)", 2.91, 5.25), ("MEC", "9.15(b)", 3.27, 2.75)]
# regime state: (deal, words in the regime name, expected) -- ended early on date / still running / not active
REGIME_STATE = [("WEST", "relief", ("ended", "2026-06-30")), ("MEC", "adjustment", ("running", None)),
                ("TRS", "holiday", ("not_active", None))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    a = ap.parse_args()
    for r in sorted(glob.glob(str(ROOT / a.runs / "run*"))):
        ok = n = 0
        misses = []
        for b, ref, val, th in REPORTED:
            ev = json.loads(open(f"{r}/{b}.events.json").read())
            ab = json.loads(open(ROOT / "data" / "abstract" / "v1" / f"{b}.abstract.json").read())
            ids = {c["id"] for c in ab["covenants"] if c["ref"].replace(" ", "").startswith(ref)}
            got = [x for x in ev["output"]["reported"] if x["covenant_id"] in ids]
            n += 2
            v_ok = any(abs(float(x["value"]) - val) < 1e-6 for x in got)
            t_ok = any(x["threshold"] and abs(float(x["threshold"]) - th) < 1e-6 for x in got)
            ok += v_ok + t_ok
            if not (v_ok and t_ok):
                misses.append(f"{b} {ref}: got {[(x['value'], x['threshold'], x['period_end']) for x in got]} want {val} / {th}")
        for b, word, (state, d) in REGIME_STATE:
            ev = json.loads(open(f"{r}/{b}.events.json").read())
            ab = json.loads(open(ROOT / "data" / "abstract" / "v1" / f"{b}.abstract.json").read())
            rids = {g["id"] for g in ab["regimes"] if word in g["name"].lower()}
            evs = [e for e in ev["output"]["events"] if e["regime_id"] in rids]
            n += 1
            if state == "ended":
                good = any(e["type"] == "regime_ended" and e["date"] == d for e in evs)
            else:
                good = not any(e["type"] in ("regime_ended", "regime_started") for e in evs)
            ok += good
            if not good:
                misses.append(f"{b} {word}: expected {state} {d or ''}, events {[(e['type'], e['date']) for e in evs]}")
        print(f"{r.split('/')[-1]}: {ok}/{n}")
        for m in misses:
            print("    -", m)


if __name__ == "__main__":
    main()
