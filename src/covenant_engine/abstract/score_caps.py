"""Score caps on the EBITDA terms against the original answer keys (data/abstracts/<B>.yaml), with the same
format-insensitive comparison as score_keys. Gives a real cap sample (the v1 keys for non-EBITDA terms hold only 8).

Old key cap types are mapped to v1: pct_of_ebitda -> pct_of_ebitda; fixed_per_test_period -> fixed_amount_per_period;
lesser_of -> lesser_of; cumulative_over_term -> lifetime_amount; fixed_per_acquisition -> per_transaction;
shared_pool -> shared_pool (type only). unlimited / time_window / formula are not amount caps: skipped.
Basis (before / after the capped items) is scored where the key states it.

Usage: python -m covenant_engine.abstract.score_caps [--abs data/abstract/v1] WEST ROAD TRS CARS MEC
"""
import argparse
import json
import re

import yaml

from ..ingest.exceptions import squash
from ..ingest.run import ROOT
from ..core.decompose_term import key_clause
from ..core.run_round import TERMS
from .clauses import norm
from .decompose import Bundle
from .score_keys import _vals, occurrences

TYPE = {"pct_of_ebitda": "pct_of_ebitda", "fixed_per_test_period": "fixed_amount", "lesser_of": "lesser_of",
        "greater_of": "greater_of", "cumulative_over_term": "lifetime_amount", "fixed_per_acquisition": "per_transaction",
        "shared_pool": "POOL"}


def key_caps(cap):
    """-> list of (v1_type, set_of_numbers or None, basis or None) for a key cap (dict or list of dicts)."""
    out = []
    for c in (cap if isinstance(cap, list) else [cap]):
        if not isinstance(c, dict) or c.get("type") not in TYPE:
            continue
        t = TYPE[c["type"]]
        nums = set()
        for k in ("value", "fixed", "pct_of_ebitda"):
            if isinstance(c.get(k), (int, float)):
                nums.add(round(float(c[k]), 6))
        for r in c.get("schedule") or []:
            if isinstance(r, dict) and isinstance(r.get("value"), (int, float)):
                nums.add(round(float(r["value"]), 6))
        basis = None
        b = str(c.get("basis", "")).lower()
        if "before" in b:
            basis = "before_capped_items"
        elif "after" in b:
            basis = "after_capped_items"
        out.append((t, nums or None, basis))
    return out


def model_nums(caps):
    nums = set()
    for c in caps:
        nums |= _vals(c["value"].replace("excess_over:", ""))[0]
    return nums


def score(b, abs_dir):
    ab = json.loads((ROOT / abs_dir / f"{b}.abstract.json").read_text(encoding="utf-8"))
    key = yaml.safe_load((ROOT / "data" / "abstracts" / f"{b}.yaml").read_text(encoding="utf-8"))
    kt = next(t for t in key["terms"] if t["name"] == TERMS[b])
    mt = next((t for t in ab["terms"] if t["name"] == TERMS[b]), None)
    live = Bundle(b).live_sq
    res = {"caps": 0, "type": 0, "value": 0, "value_n": 0, "basis": 0, "basis_n": 0, "found": 0}
    diffs = []
    if not mt:
        return res, [f"{TERMS[b]} not in abstract"]
    mcomps = mt["components"]
    for kc in kt["components"]:
        for t, nums, basis in key_caps(kc.get("cap")):
            res["caps"] += 1
            cl = norm(key_clause(kc["id"]))
            cand = [c for c in mcomps if norm(c["clause"]) == cl or norm(c["clause"]).startswith(cl + "(")]
            if not cand and kc.get("quote"):
                kp = occurrences(live, squash(kc["quote"])[:80])
                cand = [c for c in mcomps if any(abs(p - q) <= 60 for p in occurrences(live, squash(c["quote"])[:80]) for q in kp)]
            if not cand:
                diffs.append(f"{kc['id']} ({t}): no matching model part")
                continue
            res["found"] += 1
            caps = [cap for c in cand for cap in c["caps"]]
            if t == "POOL":                      # key: a shared pool -> model must mark some cap on this part as pooled
                ok = any(cap["pool"] for cap in caps)
                res["type"] += ok
                if not ok:
                    diffs.append(f"{kc['id']}: key shared pool, model caps {[(c['type'], c['value'], c['pool']) for c in caps]}")
                continue
            same = [cap for cap in caps if cap["type"] == t]
            if same:
                res["type"] += 1
            else:
                diffs.append(f"{kc['id']}: type key {t} model {[c['type'] for c in caps] or 'none'}")
            if nums is not None:
                res["value_n"] += 1
                if nums <= model_nums(same or caps):
                    res["value"] += 1
                else:
                    diffs.append(f"{kc['id']}: values key {sorted(nums)} model {sorted(model_nums(same or caps))}")
            if basis and t == "pct_of_ebitda":
                res["basis_n"] += 1
                if any(cap["basis"] == basis for cap in same):
                    res["basis"] += 1
                else:
                    diffs.append(f"{kc['id']}: basis key {basis} model {[c['basis'] for c in same]}")
    return res, diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("deals", nargs="*", default=["WEST", "ROAD", "TRS", "CARS", "MEC"])
    ap.add_argument("--abs", default="data/abstract/v1")
    a = ap.parse_args()
    tot = {}
    for b in a.deals:
        r, diffs = score(b, a.abs)
        for k, v in r.items():
            tot[k] = tot.get(k, 0) + v
        print(f"{b}: caps {r['caps']} | part found {r['found']} | type {r['type']} | values {r['value']}/{r['value_n']} | "
              f"basis {r['basis']}/{r['basis_n']}")
        for d in diffs:
            print("   -", d)
    print(f"ALL: caps {tot['caps']} | part found {tot['found']} | type {tot['type']}/{tot['caps']} | values "
          f"{tot['value']}/{tot['value_n']} | basis {tot['basis']}/{tot['basis_n']}")


if __name__ == "__main__":
    main()
