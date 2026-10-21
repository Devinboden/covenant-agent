"""Round runner: several deals x several models, step 3 only. Scores by QUOTE POSITION (works across answer keys
that label clauses differently) and compares models with each other (works without an answer key).

Usage: python -m covenant_engine.core.run_round --deals TRS CARS MEC QUAD SRI --models opus sonnet haiku
"""
import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations

import yaml

from ..ingest.exceptions import call_model, squash
from .decompose_term import PROMPT, ROOT, SCHEMA, SYSTEM, build_input

TERMS = {"WEST": "Adjusted Consolidated EBITDA", "ROAD": "Consolidated Adjusted EBITDA", "TRS": "Consolidated EBITDA",
         "CARS": "Consolidated EBITDA", "MEC": "Consolidated EBITDA", "QUAD": "Consolidated EBITDA", "SRI": "Consolidated EBITDA"}
NEAR = 60             # same clause = quotes start within this many (squashed) chars; NEAREST match wins


def nearest(p, cands):
    """Index of the candidate position nearest to p (within NEAR), else None."""
    best = None
    for i, q in enumerate(cands):
        if p >= 0 and q >= 0 and abs(p - q) <= NEAR and (best is None or abs(p - q) < abs(p - cands[best])):
            best = i
    return best


def positions(quotes, live_sq):
    out = []
    for q in quotes:
        q2 = squash(q)[:80]
        out.append(live_sq.find(q2) if q2 else -1)
    return out


def key_parts(borrower, term):
    p = ROOT / "data" / "abstracts" / f"{borrower}.yaml"
    if not p.exists():
        return None
    ab = yaml.safe_load(p.read_text(encoding="utf-8"))
    t = next((t for t in ab["terms"] if t.get("name") == term), None)
    if not t:
        return None
    parts = []
    for c in t.get("components", []):
        if c["id"].endswith("equity_cure"):
            continue                                # comes from the cure section (step 4), not the definition
        cap = c.get("cap")
        has_cap = bool(cap) and not (isinstance(cap, str) and cap.strip().lower() in ("none", "no cap", "n/a"))
        parts.append({"id": c["id"], "quote": c.get("quote", ""), "sign": c.get("sign"), "has_cap": has_cap})
    return parts


def one(job):
    b, m = job
    definition, context, live = build_input(b, TERMS[b])
    r = call_model(SYSTEM, PROMPT.format(term=TERMS[b], definition=definition, context=context), SCHEMA, model=m, timeout=900)
    return b, m, r, live


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deals", nargs="+", required=True)
    ap.add_argument("--models", nargs="+", default=["opus", "sonnet", "haiku"])
    ap.add_argument("--tag", default="round2")
    ap.add_argument("--rescore", action="store_true", help="re-score saved outputs, no model calls")
    a = ap.parse_args()
    jobs = [(b, m) for b in a.deals for m in a.models]
    out_dir = ROOT / "data" / "spike" / a.tag
    if a.rescore:
        res = []
        for b, m in jobs:
            _, _, live = build_input(b, TERMS[b])
            res.append((b, m, json.loads((out_dir / f"{b}_{m}.json").read_text(encoding="utf-8")), live))
    else:
        with ThreadPoolExecutor(6) as ex:
            res = list(ex.map(one, jobs))
    out_dir.mkdir(parents=True, exist_ok=True)
    by_deal = {}
    print(f"{'deal':5} {'model':7} {'parts':>5} {'quotes ok':>9} {'recall':>8} {'sign':>6} {'caps':>6} {'judg':>4} {'cost':>7} {'sec':>5}")
    for b, m, r, live in res:
        (out_dir / f"{b}_{m}.json").write_text(json.dumps({k: v for k, v in r.items()}, indent=1), encoding="utf-8")
        if not r["ok"]:
            print(f"{b:5} {m:7} FAILED {r.get('error')}")
            continue
        comps = r["output"]["components"]
        live_sq = squash(live)
        mpos = positions([c["quote"] for c in comps], live_sq)
        qok = sum(p >= 0 for p in mpos)
        by_deal.setdefault(b, {})[m] = (comps, mpos)
        key = key_parts(b, TERMS[b])
        rec = sign = caps = "-"
        if key:
            kpos = positions([k["quote"] for k in key], live_sq)
            hit = sgn = capk = capm = 0
            for k, kp in zip(key, kpos):
                j = nearest(kp, mpos)
                if j is not None:
                    hit += 1
                    sgn += comps[j]["sign"] == k["sign"]
                    if k["has_cap"]:
                        capk += 1
                        capm += comps[j]["cap_type"] != "none"
            rec, sign, caps = f"{hit}/{len(key)}", f"{sgn}/{hit}", f"{capm}/{capk}"
        judg = sum(c["needs_judgment"] for c in comps)
        print(f"{b:5} {m:7} {len(comps):>5} {qok:>4}/{len(comps):<4} {rec:>8} {sign:>6} {caps:>6} {judg:>4} ${r['cost_usd']:<6} {r['seconds']:>5}")
    print("\nModel-vs-model (same clause = quotes start within %d chars): clause overlap / category agreement on shared clauses" % NEAR)
    for b, ms in by_deal.items():
        for m1, m2 in combinations(sorted(ms), 2):
            (c1, p1), (c2, p2) = ms[m1], ms[m2]
            pairs = [(i, nearest(p, p2)) for i, p in enumerate(p1)]
            shared = [(i, j) for i, j in pairs if j is not None]
            union = len(c1) + len(c2) - len(shared)
            cat = sum(c1[i]["category"] == c2[j]["category"] for i, j in shared)
            capagree = sum((c1[i]["cap_type"] == "none") == (c2[j]["cap_type"] == "none") for i, j in shared)
            dis = [f"{c1[i]['clause']}: {c1[i]['category']} vs {c2[j]['category']}" for i, j in shared if c1[i]["category"] != c2[j]["category"]]
            print(f"  {b:5} {m1:6} vs {m2:6} clauses {len(shared)}/{union}  category {cat}/{len(shared)}  cap yes/no {capagree}/{len(shared)}  | {'; '.join(dis[:4])}")


if __name__ == "__main__":
    main()
