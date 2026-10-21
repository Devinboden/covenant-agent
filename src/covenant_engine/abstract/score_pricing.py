"""Score pricing grids against the two keys that have one (TRS, CARS): levels, bounds, spreads, fees, metric."""
import argparse
import glob
import json

from ..ingest.run import ROOT

KEY = {"TRS": {"metric": "Leverage Ratio", "levels": [  # (lower, upper, sofr, fee)
           (3.25, None, 0.02, 0.003), (2.75, 3.25, 0.0175, 0.0025), (2.25, 2.75, 0.01625, 0.00225),
           (1.5, 2.25, 0.015, 0.002), (None, 1.5, 0.01375, 0.00175)]},
       "CARS": {"metric": "Senior Secured Net Leverage Ratio", "levels": [
           (3.0, None, 0.0275, 0.004), (2.0, 3.0, 0.025, 0.0035), (1.0, 2.0, 0.0225, 0.003), (None, 1.0, 0.02, 0.0025)],
           "abr": [0.0175, 0.015, 0.0125, 0.01], "late_default": True}}


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    a = ap.parse_args()
    for r in sorted(glob.glob(str(ROOT / a.runs / "run*"))):
        ok = n = 0
        miss = []
        for b, k in KEY.items():
            o = json.loads(open(f"{r}/{b}.pricing.json").read())["output"]
            n += 1
            ok += o["metric"] == k["metric"]
            if o["metric"] != k["metric"]:
                miss.append(f"{b} metric {o['metric']!r}")
            got = {}
            for L in o["levels"]:
                g = {m["type"]: m.get("fraction") for m in L["margins"]}
                got[(f(L["lower"]) if L["lower"] else None, f(L["upper"]) if L["upper"] else None)] = g
            for i, (lo, hi, sofr, fee) in enumerate(k["levels"]):
                g = got.get((lo, hi))
                n += 3
                ok += g is not None
                ok += bool(g) and abs((g.get("term_sofr_spread") or -1) - sofr) < 1e-9
                ok += bool(g) and abs((g.get("commitment_fee") or -1) - fee) < 1e-9
                if not g or abs((g.get("term_sofr_spread") or -1) - sofr) > 1e-9 or abs((g.get("commitment_fee") or -1) - fee) > 1e-9:
                    miss.append(f"{b} level {lo}-{hi}: got {g} want sofr {sofr} fee {fee}")
                if "abr" in k and g:
                    n += 1
                    good = abs((g.get("base_rate_spread") or -1) - k["abr"][i]) < 1e-9
                    ok += good
                    if not good:
                        miss.append(f"{b} level {lo}-{hi} base rate: got {g.get('base_rate_spread')} want {k['abr'][i]}")
            if k.get("late_default"):
                n += 1
                ok += bool(o["default_if_late"])
                if not o["default_if_late"]:
                    miss.append(f"{b} default_if_late missing")
            n += 1
            extra = len(o["levels"]) - len(k["levels"])
            ok += extra == 0
            if extra:
                miss.append(f"{b} {len(o['levels'])} levels vs {len(k['levels'])}")
        print(f"{r.split('/')[-1]}: {ok}/{n}")
        for m in miss:
            print("    -", m)


if __name__ == "__main__":
    main()
