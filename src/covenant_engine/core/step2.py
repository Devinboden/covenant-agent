"""Spike, step 2: maintenance covenants, threshold schedules and the regimes that change them.

Input  : the intake packet (packet.md) only.
Model  : one call per deal, `claude -p --json-schema` (Method 1: enums for every structural choice).
Code   : verifies quotes; scores against the answer keys by EVALUATING the schedule at every quarter-end
         2025-2028 (standard and relief regimes separately), so differently-shaped rows that mean the same thing agree;
         scores regimes by type + parameters (level/increment/ceiling, duration, max uses, gap, early exit).
         QUAD / SRI have no key: model-vs-model agreement only.

Usage: python -m covenant_engine.core.step2 --deals WEST ROAD TRS CARS MEC QUAD SRI --models opus sonnet haiku
       python -m covenant_engine.core.step2 --rescore ...
"""
import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from itertools import combinations

import yaml

from ..ingest.exceptions import call_model, squash
from .decompose_term import ROOT, bundle_dir

ROW = {"type": "object", "additionalProperties": False, "required": ["from", "to", "value", "regime", "quote"],
       "properties": {"from": {"type": "string"}, "to": {"type": "string"}, "value": {"type": "string"},
                      "regime": {"type": "string", "enum": ["standard", "relief", "step_up", "other"]},
                      "quote": {"type": "string"}}}
COVENANT = {"type": "object", "additionalProperties": False,
            "required": ["ref", "name", "metric", "kind", "direction", "frequency", "thresholds", "applies_only_during", "quote"],
            "properties": {"ref": {"type": "string"}, "name": {"type": "string"}, "metric": {"type": "string"},
                           "kind": {"type": "string", "enum": ["ratio", "level"]},
                           "direction": {"type": "string", "enum": ["max", "min"]},
                           "frequency": {"type": "string", "enum": ["quarterly", "monthly", "annual", "other"]},
                           "thresholds": {"type": "array", "items": ROW},
                           "applies_only_during": {"type": "string"}, "quote": {"type": "string"}}}
REGIME = {"type": "object", "additionalProperties": False,
          "required": ["type", "name", "applies_to", "trigger", "adjustment", "level", "increment", "ceiling",
                       "duration_quarters", "max_uses", "min_gap_quarters", "start", "end", "early_termination",
                       "after_early_termination", "conditions", "quote"],
          "properties": {
              "type": {"type": "string", "enum": ["relief_period", "step_up_election", "springing_test", "other"]},
              "name": {"type": "string"},
              "applies_to": {"type": "array", "items": {"type": "string"}},
              "trigger": {"type": "string", "enum": ["borrower_election", "acquisition_automatic", "date", "amendment",
                                                     "usage_threshold", "other"]},
              "adjustment": {"type": "string", "enum": ["replace_schedule", "set_level", "add_increment", "suspend_test", "other"]},
              "level": {"type": "string"}, "increment": {"type": "string"}, "ceiling": {"type": "string"},
              "duration_quarters": {"type": "string"}, "max_uses": {"type": "string"}, "min_gap_quarters": {"type": "string"},
              "start": {"type": "string"}, "end": {"type": "string"},
              "early_termination": {"type": "boolean"},
              "after_early_termination": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False, "required": ["covenant_ref", "value"],
                  "properties": {"covenant_ref": {"type": "string"}, "value": {"type": "string"}}}},
              "conditions": {"type": "array", "items": {"type": "string"}},
              "quote": {"type": "string"}}}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["covenants", "regimes"],
          "properties": {"covenants": {"type": "array", "items": COVENANT}, "regimes": {"type": "array", "items": REGIME}}}

SYSTEM = ("You extract financial maintenance covenants from commercial credit agreements into a fixed structure. "
          "Work only from the text given. Every quote must be copied exactly from the text. Never do arithmetic beyond "
          "copying numbers; never fill a gap from general knowledge (leave the field \"\").")

PROMPT = """From the reading packet below, extract:

1. covenants: every MAINTENANCE financial covenant (tested periodically: leverage, coverage, liquidity, capex limits, cash caps).
   NOT incurrence gates (tests only when taking an action such as an acquisition, debt or a restricted payment), NOT pricing grids.
   - ref: section as cited, e.g. "6.09(a)". metric: the defined ratio or amount tested. direction: max (must not exceed) / min.
   - thresholds: one row per value in the text, ALL rows including past dates. from/to: test dates as YYYY-MM-DD, inclusive
     ("" if open-ended); a row for a single test date has from = to. value: decimal ("4.50", "15000000").
     regime: "standard" for the normal schedule, "relief" for rows that apply only during a relief/adjustment period,
     "step_up" for a raised level available only by election/acquisition.
   - applies_only_during: name of the period this covenant exists in, if limited (e.g. a relief period); else "".
2. regimes: every mechanism that changes which threshold applies.
   - relief_period: a named period with its own schedule. early_termination: can the borrower end it early?
     after_early_termination: values that apply instead if it ends early (only if the text states them).
   - step_up_election: a temporary higher level after an acquisition or an election (incl. "covenant holiday",
     "leverage increase period"). level = the raised cap if stated as a level; increment = the increase if stated as
     "+x"; ceiling = the cap on the raised level; duration_quarters, max_uses, min_gap_quarters (quarters required between uses).
   - Numbers as decimals; "" when the text doesn't say. conditions: short phrases (notice, minimum acquisition size, etc.).

READING PACKET:
<<<
{packet}
>>>"""

# Expected regimes, hand-mapped from the answer keys (data/abstracts/*.yaml). None = key doesn't say -> not scored.
EXPECTED = {
    "WEST": [{"type": "relief_period", "early_termination": True},
             {"type": "step_up_election", "level": 4.5, "duration_quarters": 4, "max_uses": 2}],
    "ROAD": [{"type": "step_up_election", "level": 4.25, "duration_quarters": 4}],
    "TRS": [{"type": "step_up_election", "level": 4.5, "duration_quarters": 4, "max_uses": 2, "min_gap_quarters": 2}],
    "CARS": [{"type": "step_up_election", "increment": 0.5, "ceiling": 4.0, "duration_quarters": 4}],
    "MEC": [{"type": "relief_period", "early_termination": True, "after_early_termination": {3.5, 3.0}},
            {"type": "step_up_election", "increment": 0.5, "ceiling": 4.0, "duration_quarters": 4, "max_uses": 2,
             "min_gap_quarters": 1}],
}
QTRS = [date(y, m, d) for y in range(2025, 2029) for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))]


def nref(r):
    return re.sub(r"^(section|§)\s*", "", str(r).strip().lower()).replace(" ", "")


def num(x):
    try:
        return float(str(x).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def d(x):
    if x in (None, "", "null"):
        return None
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def key_rows(c):
    rows = []
    for r in c.get("thresholds") or []:
        reg = r.get("regime", "standard")
        if "date" in r:
            rows.append((d(r["date"]), d(r["date"]), num(r["value"]), reg))
        else:
            rows.append((d(r.get("from")), d(r.get("to")), num(r.get("value")), reg))
    if not rows and c.get("value") is not None:
        rows.append((None, None, num(c["value"]), "standard"))
    return rows


def model_rows(c):
    return [(d(r["from"]), d(r["to"]), num(r["value"]), r["regime"]) for r in c["thresholds"]]


def at(rows, q, regime):
    vals = [v for f, t, v, reg in rows if reg == regime and (f is None or f <= q) and (t is None or q <= t)]
    return vals[-1] if vals else None          # later rows win (amended text listed last)


def key_maintenance(b):
    p = ROOT / "data" / "abstracts" / f"{b}.yaml"
    if not p.exists():
        return None
    ab = yaml.safe_load(p.read_text(encoding="utf-8"))
    return [c for c in ab["covenants"] if c.get("kind") in ("ratio", "level") and c.get("direction") in ("max", "min")]


def score_deal(b, out, live_sq):
    quotes = [r["quote"] for c in out["covenants"] for r in c["thresholds"]] + [c["quote"] for c in out["covenants"]] + \
             [g["quote"] for g in out["regimes"]]
    qok = sum(1 for q in quotes if q and squash(q)[:80] in live_sq)
    res = {"covenants": len(out["covenants"]), "regimes": [g["type"] for g in out["regimes"]],
           "quotes_ok": f"{qok}/{len(quotes)}"}
    keys = key_maintenance(b)
    if keys is None:
        return res
    mc = {}
    found = dir_ok = pts = pts_ok = 0
    bad = []
    for kc in keys:
        # a key covenant may be split by the model into sub-clauses (6.09(a)(i) standard, 6.09(a)(ii) relief): pool them
        ms = [c for c in out["covenants"] if nref(c["ref"]) == nref(kc["ref"]) or nref(c["ref"]).startswith(nref(kc["ref"]) + "(")]
        if not ms:
            bad.append(f"{kc['ref']} not found")
            continue
        found += 1
        dir_ok += all(m["direction"] == kc["direction"] for m in ms)
        kr, mr = key_rows(kc), [r for m in ms for r in model_rows(m)]
        key_has_relief = any(reg == "relief" for *_, reg in kr)
        for regime in ("standard", "relief"):
            for q in QTRS:
                kv = at(kr, q, regime)
                if kv is None:
                    continue
                pts += 1
                mv = at(mr, q, regime)
                if mv is None and not key_has_relief:      # covenant that exists only during relief: regime label is moot
                    mv = at(mr, q, "relief")
                if mv is not None and abs(mv - kv) < 1e-6:
                    pts_ok += 1
                elif len(bad) < 12:
                    bad.append(f"{kc['ref']} {regime} {q}: key {kv} model {mv}")
    res.update({"key_covenants": f"{found}/{len(keys)}", "direction": f"{dir_ok}/{found}",
                "schedule_points": f"{pts_ok}/{pts}"})
    # regimes
    exp_ok = exp_n = 0
    for e in EXPECTED.get(b, []):
        cands = [g for g in out["regimes"] if g["type"] == e["type"]]
        exp_n += 1 + len(e) - 1
        if not cands:
            bad.append(f"regime {e['type']} missing")
            continue
        best, best_s, best_bad = None, -1, []
        for g in cands:
            s, gb = 1, []
            for k, v in e.items():
                if k == "type":
                    continue
                if k == "early_termination":
                    ok = g[k] == v
                elif k == "after_early_termination":
                    ok = {num(x["value"]) for x in g[k]} == v
                else:
                    ok = num(g[k]) is not None and abs(num(g[k]) - v) < 1e-6
                s += ok
                if not ok:
                    gb.append(f"{k}: key {v} model {g[k] if k != 'after_early_termination' else [x['value'] for x in g[k]]}")
            if s > best_s:
                best, best_s, best_bad = g, s, gb
        exp_ok += best_s
        bad += [f"regime {e['type']} {x}" for x in best_bad]
    res.update({"regime_fields": f"{exp_ok}/{exp_n}", "misses": bad})
    return res


def at_loose(rows, other, q, regime):
    """Value at q; if neither model labels any row of this covenant 'relief' vs 'standard' the same way (e.g. a
    covenant that only exists during relief), ignore the label."""
    v = at(rows, q, regime)
    if v is None and not any(r[3] == regime for r in rows):
        alt = "relief" if regime == "standard" else "standard"
        if any(r[3] == regime for r in other) and not any(r[3] == alt for r in other):
            v = at(rows, q, alt)
    return v


def agreement(o1, o2):
    """Model vs model: covenant refs in common, and schedule values that agree at each quarter-end."""
    def pool(o):                                   # group sub-clauses under the base ref, e.g. 6.09(a)(ii) -> 6.09(a)
        g = {}
        for c in o["covenants"]:
            m = re.match(r"[\d.]+(\([a-z]\))?", nref(c["ref"]))
            g.setdefault(m.group(0) if m else nref(c["ref"]), []).extend(model_rows(c))
        return g
    c1, c2 = pool(o1), pool(o2)
    common = set(c1) & set(c2)
    pts = ok = 0
    for r in common:
        for regime in ("standard", "relief"):
            for q in QTRS:
                a, b_ = at_loose(c1[r], c2[r], q, regime), at_loose(c2[r], c1[r], q, regime)
                if a is None and b_ is None:
                    continue
                pts += 1
                ok += a is not None and b_ is not None and abs(a - b_) < 1e-6
    t1 = sorted(g["type"] for g in o1["regimes"])
    t2 = sorted(g["type"] for g in o2["regimes"])
    return f"covenants {len(common)}/{len(set(c1) | set(c2))} schedule {ok}/{pts} regimes {'same' if t1 == t2 else f'{t1} vs {t2}'}"


def one(job):
    b, m = job
    packet = (bundle_dir(b) / "packet.md").read_text(encoding="utf-8")
    return b, m, call_model(SYSTEM, PROMPT.format(packet=packet), SCHEMA, model=m, timeout=1200)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deals", nargs="+", default=["WEST", "ROAD", "TRS", "CARS", "MEC", "QUAD", "SRI"])
    ap.add_argument("--models", nargs="+", default=["opus", "sonnet", "haiku"])
    ap.add_argument("--tag", default="step2")
    ap.add_argument("--rescore", action="store_true")
    a = ap.parse_args()
    out_dir = ROOT / "data" / "spike" / a.tag
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(b, m) for b in a.deals for m in a.models]
    if a.rescore:
        res = [(b, m, json.loads((out_dir / f"{b}_{m}.json").read_text(encoding="utf-8"))) for b, m in jobs]
    else:
        with ThreadPoolExecutor(7) as ex:
            res = list(ex.map(one, jobs))
        for b, m, r in res:
            (out_dir / f"{b}_{m}.json").write_text(json.dumps(r, indent=1), encoding="utf-8")
    lines, outs = [], {}
    for b, m, r in res:
        if not r["ok"]:
            lines.append(f"{b:5} {m:7} FAILED {r.get('error')}")
            continue
        live_sq = squash((bundle_dir(b) / "text_live.txt").read_text(encoding="utf-8"))
        s = score_deal(b, r["output"], live_sq)
        outs.setdefault(b, {})[m] = r["output"]
        lines.append(f"{b:5} {m:7} covs {s['covenants']:>2} key {s.get('key_covenants', '-'):>5} dir {s.get('direction', '-'):>5} "
                     f"schedule {s.get('schedule_points', '-'):>7} regime fields {s.get('regime_fields', '-'):>5} "
                     f"quotes {s['quotes_ok']:>7} ${r['cost_usd']} {r['seconds']}s | regimes {s['regimes']}")
        for x in s.get("misses", []):
            lines.append(f"        - {x}")
    lines.append("\nModel vs model:")
    for b, ms in outs.items():
        for m1, m2 in combinations(sorted(ms), 2):
            lines.append(f"  {b:5} {m1:6} vs {m2:6} {agreement(ms[m1], ms[m2])}")
    txt = "\n".join(lines)
    print(txt)
    (out_dir / "summary.txt").write_text(txt, encoding="utf-8")


if __name__ == "__main__":
    main()
