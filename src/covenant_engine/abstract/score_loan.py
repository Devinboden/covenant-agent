"""Score the Loan section (#20) against the original answer keys (data/abstracts/<B>.yaml, 'agreement' block).
Expected values transcribed from the keys (EXPECTED below); None = the key doesn't state it (not scored).
ROAD facilities and WEST facilities are not scored: the ROAD key took them from the 10-Q, WEST's key has none.

Usage: python -m covenant_engine.abstract.score_loan --runs data/abstract/loan WEST ROAD TRS CARS MEC
"""
import argparse
import glob
import json
import re

from ..ingest.run import ROOT

EXPECTED = {
    "WEST": {"borrower": "Westrock Beverage Solutions", "agent": "Wells Fargo", "closing_date": "2022-08-29",
             "fiscal_year_end": None, "annual_days": 90, "quarterly_days": 45, "facilities": None,
             # fiscal_year_end: key's 12-31 is from the 10-K; the agreement doesn't state it -> filings (#23)
             "cure": {"present": True, "covenants": {"6.09"}, "deadline": (15, "business_days"),
                      "limits": {"cure_free_quarters_in_four": 2, "max_total": 5, "amount_limited_to_shortfall": None}}},
    "ROAD": {"borrower": "Construction Partners", "agent": "PNC", "closing_date": None, "agreement_date": "2022-06-30",
             "fiscal_year_end": "09-30", "annual_days": 120, "quarterly_days": 45, "facilities": None,
             "cure": {"present": True, "covenants": {"9.16"}, "deadline": (10, "business_days"), "prepayment": True,
                      "limits": {"cure_free_quarters_in_four": 2, "max_per_fiscal_year": 1, "max_total": 3,
                                 "amount_limited_to_shortfall": None}}},
    "TRS": {"borrower": "TriMas Company", "agent": "JPMorgan", "closing_date": "2013-10-16", "fiscal_year_end": "12-31",
            "annual_days": 90, "quarterly_days": 45, "facilities": [(250000000, "2030-03-31")], "cure": {"present": False}},
    "CARS": {"borrower": "Cars.com", "agent": "JPMorgan", "closing_date": None, "agreement_date": "2017-05-31",
             "fiscal_year_end": "12-31", "annual_days": 120, "quarterly_days": 60, "facilities": [(350000000, None)],
             "cure": {"present": False}},
    "MEC": {"borrower": "Mayville Engineering", "agent": "Wells Fargo", "closing_date": "2023-06-28",
            "fiscal_year_end": "12-31", "annual_days": 120, "quarterly_days": 45, "facilities": [(275000000, "2028-06-28")],
            "cure": {"present": False}},
}


def words(s):
    return {w for w in re.findall(r"[a-z]+", (s or "").lower()) if len(w) > 2 and w not in ("the", "inc", "llc", "bank", "national", "association", "company")}


def num(x):
    try:
        return float(str(x).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def score(b, out):
    e = EXPECTED[b]
    rows = []

    def add(field, ok, got, want):
        rows.append((field, bool(ok), got, want))

    add("borrower", words(e["borrower"]) <= words(out["borrower"]), out["borrower"], e["borrower"])
    add("agent", words(e["agent"]) <= words(out["agent"]), out["agent"], e["agent"])
    if e.get("closing_date"):
        add("closing_date", out["closing_date"]["value"] == e["closing_date"], out["closing_date"]["value"], e["closing_date"])
    if e.get("agreement_date"):
        got = out["agreement_date"]["value"]
        add("agreement_date", got == e["agreement_date"], got, e["agreement_date"])
    if e["fiscal_year_end"]:
        add("fiscal_year_end", out["fiscal_year_end"]["value"] == e["fiscal_year_end"], out["fiscal_year_end"]["value"], e["fiscal_year_end"])
    add("annual_days", num(out["reporting"]["annual_days"]) == e["annual_days"], out["reporting"]["annual_days"], e["annual_days"])
    add("quarterly_days", num(out["reporting"]["quarterly_days"]) == e["quarterly_days"], out["reporting"]["quarterly_days"], e["quarterly_days"])
    if e["facilities"]:
        for amt, mat in e["facilities"]:
            f = [x for x in out["facilities"] if num(x["commitment"]) == amt]
            add(f"facility {amt / 1e6:.0f}M", bool(f), [(x["type"], x["commitment"], x["maturity"]) for x in out["facilities"]], amt)
            if mat:
                add(f"maturity {amt / 1e6:.0f}M", any(x["maturity"] == mat for x in f), [x["maturity"] for x in f], mat)
    c, ec = out["equity_cure"], e["cure"]
    add("cure present", c["present"] == ec["present"], c["present"], ec["present"])
    if ec["present"] and c["present"]:
        got_cov = {re.sub(r"^Section\s+", "", x, flags=re.I).split("(")[0] for x in c["covenants"]}
        add("cure covenants", ec["covenants"] <= got_cov, sorted(got_cov), sorted(ec["covenants"]))
        add("cure deadline", (num(c["deadline_count"]), c["deadline_unit"]) == ec["deadline"],
            (c["deadline_count"], c["deadline_unit"]), ec["deadline"])
        if "prepayment" in ec:
            add("cure prepayment", c["prepayment_required"] == ec["prepayment"], c["prepayment_required"], ec["prepayment"])
        got_l = {}
        for lim in c["limits"]:
            got_l.setdefault(lim["type"], num(lim["value"]))
        for t, v in ec["limits"].items():
            ok = t in got_l and (v is None or got_l[t] == v)
            add(f"cure limit {t}", ok, got_l.get(t, "missing"), v if v is not None else "present")
        extra = set(got_l) - set(ec["limits"]) - {"other"}
        if extra:
            add("cure extra limits", False, sorted(extra), "none")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("deals", nargs="+")
    ap.add_argument("--runs", required=True)
    a = ap.parse_args()
    runs = sorted(glob.glob(str(ROOT / a.runs / "run*"))) or [str(ROOT / a.runs)]
    for r in runs:
        tot = ok = 0
        misses = []
        for b in a.deals:
            out = json.loads(open(f"{r}/{b}.loan.json").read())["output"]
            for field, good, got, want in score(b, out):
                tot += 1
                ok += good
                if not good:
                    misses.append(f"{b} {field}: got {got} want {want}")
        print(f"{r.split('/')[-1]}: {ok}/{tot} fields")
        for m in misses:
            print("    -", m)


if __name__ == "__main__":
    main()
