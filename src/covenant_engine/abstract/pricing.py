"""Pricing grid (#24, in scope): the leverage-based margin grid. Sonnet reads levels, boundaries and margins (copied as
written, unit percent or bps; code converts to fractions); code checks levels don't overlap, the metric is a defined
term, and margins fall as leverage falls (a swapped column shows up as margins rising). A grid that sits on a schedule
the filing doesn't include goes to review instead of being guessed.

Usage: python -m covenant_engine.abstract.pricing TRS
"""
import argparse
import json
import re

from ..ingest.exceptions import call_model, squash
from ..ingest.run import ROOT
from ..core.decompose_term import bundle_dir

NAME = re.compile(r"Applicable (Rate|Margin|Percentage|Spread|Commitment Fee)|Pricing (Level|Grid)|Commitment Fee Rate")
MARGIN = {"type": "object", "additionalProperties": False, "required": ["type", "value", "unit"],
          "properties": {"type": {"type": "string", "enum": ["term_sofr_spread", "base_rate_spread", "commitment_fee",
                                                             "letter_of_credit_fee", "other"]},
                         "value": {"type": "string"}, "unit": {"type": "string", "enum": ["percent", "bps"]}}}
LEVEL = {"type": "object", "additionalProperties": False,
         "required": ["level", "lower", "lower_inclusive", "upper", "upper_inclusive", "margins"],
         "properties": {"level": {"type": "string"}, "lower": {"type": "string"}, "lower_inclusive": {"type": "boolean"},
                        "upper": {"type": "string"}, "upper_inclusive": {"type": "boolean"},
                        "margins": {"type": "array", "items": MARGIN}}}
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["present", "metric", "levels", "effective", "default_if_late", "on_schedule", "quote", "flags"],
          "properties": {"present": {"type": "boolean"}, "metric": {"type": "string"},
                         "levels": {"type": "array", "items": LEVEL}, "effective": {"type": "string"},
                         "default_if_late": {"type": "string"}, "on_schedule": {"type": "string"},
                         "quote": {"type": "string"}, "flags": {"type": "array", "items": {"type": "string"}}}}
# the stored grid: code adds 'fraction' (converted margin) to every margin
import copy
STORED = copy.deepcopy(SCHEMA)
STORED["properties"]["levels"]["items"]["properties"]["margins"]["items"]["properties"]["fraction"] = {"type": ["number", "null"]}
SYSTEM = ("You read the pricing grid of a credit agreement into a fixed structure. Work only from the text given. Copy "
          "numbers exactly as written (no conversion). Every quote must be copied exactly.")
PROMPT = """Extract the pricing grid (the margin / fee levels that move with a financial ratio).
- present: false if pricing does not move with a ratio (flat margin), or if the grid is not in the text.
- metric: the exact defined term of the ratio the grid uses (e.g. "Total Net Leverage Ratio").
- levels: one per row. lower / upper: the ratio bounds as written ("3.25"; "" if open), with whether each bound is
  inclusive ("greater than or equal to" = inclusive; "greater than" = not). margins: every column, with value as written
  ("2.75" for 2.75%, "275" for 275 bps) and its unit; read the column headers carefully (term SOFR vs base rate vs fee).
- effective: when a level change takes effect. default_if_late: the level that applies if financials are late ("" if none).
- on_schedule: if the grid itself is on a schedule (e.g. "Schedule 1.1(A)"), its name; else "".
- quote: the words that introduce or state the grid. flags: anything a reviewer must see.

TEXT:
<<<
{text}
>>>"""


def gather(borrower):
    d = bundle_dir(borrower)
    defs = json.loads((d / "definitions.json").read_text(encoding="utf-8"))["defined"]
    parts = [f"[Definition] {v['text']}" for n, v in defs.items() if NAME.search(n) and len(v["text"]) < 15000]
    report = json.loads((d / "report.json").read_text(encoding="utf-8"))
    return "\n\n".join(parts), squash((d / "text_live.txt").read_text(encoding="utf-8")), defs, \
        {re.sub(r"\s+", " ", m) for m in report["attachments_referenced_in_packet_but_missing"]}


def frac(m):
    try:
        v = float(m["value"].replace("%", "").replace(",", ""))
    except ValueError:
        return None
    return round(v / 100 if m["unit"] == "percent" else v / 10000, 6)


def _f(x):
    try:
        return float(x)
    except ValueError:
        return None


def check(out, live_sq, defs, missing):
    P, review = [], []
    if not out["present"]:
        if out["on_schedule"]:
            ref = re.sub(r"\s+", " ", out["on_schedule"])
            if any(ref.startswith(m) or m.startswith(ref) for m in missing):
                review.append(f"pricing grid is on {ref}, which is not in the filed documents: obtain it")
        return P, review
    if out["quote"] and squash(out["quote"])[:80] not in live_sq:
        P.append(f"quote not found verbatim: {out['quote'][:70]!r}")
    if out["metric"] and out["metric"] not in defs:
        P.append(f"metric {out['metric']!r} is not a defined term; give its exact name")
    lv = []
    for L in out["levels"]:
        lo, hi = _f(L["lower"]) if L["lower"] else None, _f(L["upper"]) if L["upper"] else None
        if (L["lower"] and lo is None) or (L["upper"] and hi is None):
            P.append(f"level {L['level']}: bounds must be numbers")
            continue
        for m in L["margins"]:
            if frac(m) is None:
                P.append(f"level {L['level']}: margin {m['type']} value {m['value']!r} must be a number")
        lv.append((lo if lo is not None else -1e9, hi if hi is not None else 1e9, L))
    lv.sort(key=lambda x: x[0])
    for (l1, h1, A), (l2, h2, B) in zip(lv, lv[1:]):
        if h1 > l2 or (h1 == l2 and A["upper_inclusive"] and B["lower_inclusive"]):
            P.append(f"levels {A['level']} and {B['level']} overlap")
        if h1 < l2:
            P.append(f"gap between levels {A['level']} and {B['level']} ({h1} to {l2})")
        for t in ("term_sofr_spread", "base_rate_spread", "commitment_fee"):
            a = [frac(m) for m in A["margins"] if m["type"] == t]
            b = [frac(m) for m in B["margins"] if m["type"] == t]
            if a and b and a[0] is not None and b[0] is not None and a[0] > b[0]:
                P.append(f"{t} falls as the ratio rises (level {A['level']} {a[0]} > level {B['level']} {b[0]}): check the columns")
    return P, review


def run(borrower, model="sonnet", max_repairs=2):
    text, live_sq, defs, missing = gather(borrower)
    if not text:
        return {"output": None, "review": ["no pricing definition found"], "trace": [], "cost": 0, "seconds": 0}
    prompt = PROMPT.format(text=text)
    trace = []
    r = call_model(SYSTEM, prompt, SCHEMA, model=model, timeout=900)
    trace.append({k: r.get(k) for k in ("model", "cost_usd", "seconds", "ok")})
    if not r["ok"]:
        return {"error": r.get("error"), "trace": trace}
    out = r["output"]
    for _ in range(max_repairs):
        probs, _ = check(out, live_sq, defs, missing)
        trace[-1]["problems"] = probs
        if not probs:
            break
        r = call_model(SYSTEM, prompt + "\n\nYOUR PREVIOUS ANSWER FAILED THESE CHECKS; return the complete corrected answer:\n"
                       + "\n".join(f"- {p}" for p in probs) + "\n\nPREVIOUS ANSWER:\n" + json.dumps(out), SCHEMA,
                       model=model, timeout=900)
        trace.append({k: r.get(k) for k in ("model", "cost_usd", "seconds", "ok")})
        if not r["ok"]:
            break
        out = r["output"]
    probs, review = check(out, live_sq, defs, missing)
    trace[-1]["problems"] = probs
    for L in out["levels"]:                      # code converts margins to fractions
        for m in L["margins"]:
            m["fraction"] = frac(m)
    return {"output": out, "review": probs + review, "trace": trace,
            "cost": round(sum(t["cost_usd"] or 0 for t in trace), 4), "seconds": round(sum(t["seconds"] or 0 for t in trace), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("borrower")
    a = ap.parse_args()
    print(json.dumps(run(a.borrower), indent=1)[:5000])


if __name__ == "__main__":
    main()
