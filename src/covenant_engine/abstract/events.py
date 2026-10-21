"""Events (#23): the state of the loan at onboarding, from the borrower's filings (latest 10-Q, 10-K, 8-Ks), not the
agreement. What has HAPPENED under the rules the abstract describes: relief periods ended, elections made, cures used,
amendments, and the covenant results the borrower reported.

Code picks the passages (paragraphs naming the credit agreement, a covenant, a regime, a ratio "x.xx to 1.00");
Sonnet extracts events linked to the abstract's covenant and regime ids; code checks quotes against that filing, ids,
dates, and cross-checks every reported threshold against the thresholds the abstract allows on that date (#29: a
mismatch means the abstract or the reading is wrong -> review). Fiscal year end comes from the 10-K cover by regex.

Usage: python -m covenant_engine.abstract.events WEST
"""
import argparse
import json
import re
from datetime import date

import pymupdf

from ..ingest.exceptions import call_model, squash
from ..ingest.run import ROOT

EVENT_TYPES = ["regime_started", "regime_ended", "election_made", "cure_used", "amendment_effective", "waiver",
               "default_or_breach", "other"]
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["events", "reported", "flags"],
          "properties": {
              "events": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False,
                  "required": ["type", "regime_id", "covenant_id", "date", "doc", "quote", "note"],
                  "properties": {"type": {"type": "string", "enum": EVENT_TYPES}, "regime_id": {"type": "string"},
                                 "covenant_id": {"type": "string"}, "date": {"type": "string"}, "doc": {"type": "string"},
                                 "quote": {"type": "string"}, "note": {"type": "string"}}}},
              "reported": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False,
                  "required": ["covenant_id", "period_end", "value", "threshold", "doc", "quote"],
                  "properties": {"covenant_id": {"type": "string"}, "period_end": {"type": "string"},
                                 "value": {"type": "string"}, "threshold": {"type": "string"},
                                 "doc": {"type": "string"}, "quote": {"type": "string"}}}},
              "flags": {"type": "array", "items": {"type": "string"}}}}
SYSTEM = ("You extract facts about a loan's current state from a borrower's SEC filings into a fixed structure. Work only "
          "from the passages given. Every quote must be copied exactly. Never compute; copy numbers and dates as written.")
PROMPT = """The credit agreement's covenants and regimes (use these ids):
COVENANTS: {covenants}
REGIMES: {regimes}

From the filing passages below extract:
1. events: things that HAPPENED under the credit agreement, with the date they happened (YYYY-MM-DD, or "" if not stated):
   a regime started or ended (relief / adjustment period ended early, covenant holiday or step-up began), an election
   made, an equity cure used, an amendment became effective, a waiver, a default. Link regime_id / covenant_id to the ids
   above ("" if none fits). doc = the file name shown in the passage header.
2. reported: each covenant result the borrower reports: covenant_id, period_end (YYYY-MM-DD), value (the ratio, e.g.
   "2.91"), threshold (the covenant level the filing compares it to, e.g. "5.25"; "" if not stated), doc, quote.
   Only the LATEST period reported for each covenant.
3. flags: anything a reviewer should see (e.g. the filing describes a mechanism as available but unused).
Do not report hypothetical statements ("if we were to ...") as events.

PASSAGES:
<<<
{passages}
>>>"""

CUE = re.compile(r"Credit Agreement|covenant|Covenant|to 1\.00|-to-1\.00|\bx\b as of|Amendment No|Relief Period|"
                 r"Adjustment Period|Holiday|Step-Up|Increase Period|equity cure|Cure", re.I)


def filings(borrower):
    """Latest 10-Q, latest 10-K, all 8-Ks for the deal."""
    d = ROOT / "data" / "pdf" / borrower
    qs = sorted(d.glob("10-Q_*.pdf"))
    ks = sorted(d.glob("10-K_*.pdf"))
    eights = sorted(d.glob("8-K_*.pdf"))
    return ([qs[-1]] if qs else []) + ([ks[-1]] if ks else []) + eights


def paragraphs(pdf):
    out = []
    for page in pymupdf.open(pdf):
        for b in page.get_text("blocks"):
            t = re.sub(r"\s+", " ", b[4]).strip()
            if len(t) > 60:
                out.append(t)
    return out


def gather(borrower, names, max_chars=60000):
    docs, parts = {}, []
    for f in filings(borrower):
        paras = paragraphs(f)
        docs[f.name] = squash(" ".join(paras))
        keep = [p for p in paras if CUE.search(p) or any(n.lower() in p.lower() for n in names)]
        budget = 36000 if f.name.startswith("10-Q") else 14000        # the latest 10-Q carries the current state
        txt = "\n".join(keep)[:budget]
        parts.append(f"=== {f.name} ===\n{txt}")
    return "\n\n".join(parts), docs


def fiscal_year_end(borrower):
    ks = sorted((ROOT / "data" / "pdf" / borrower).glob("10-K_*.pdf"))
    if not ks:
        return None
    t = " ".join(p.get_text() for p in list(pymupdf.open(ks[-1]))[:2])
    m = re.search(r"fiscal year ended\s+([A-Z][a-z]+)\s+(\d{1,2}),?\s+\d{4}", t, re.I)
    if not m:
        return None
    mon = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"].index(m.group(1).lower()) + 1
    return {"value": f"{mon:02d}-{int(m.group(2)):02d}", "quote": m.group(0), "doc": ks[-1].name}


def allowed_thresholds(ab, cov_id, on):
    """Every threshold the abstract allows for this covenant on this date: standard rows, relief rows, early-exit
    levels, step-up levels/increments. A reported threshold outside this set means the abstract or the reading is off."""
    def rows_at(rows):
        vals = set()
        for r in rows:
            s, e = r["start"], r["end"]
            sd = date.fromisoformat(s["date"]) if s["kind"] == "date" and s["date"] else None
            ed = date.fromisoformat(e["date"]) if e["kind"] == "date" and e["date"] else None
            if (sd is None or sd <= on) and (ed is None or on <= ed):
                try:
                    vals.add(round(float(r["value"]), 4))
                except ValueError:
                    pass
        return vals
    cov = next((c for c in ab["covenants"] if c["id"] == cov_id), None)
    if not cov:
        return set()
    vals = rows_at(cov["schedule"])
    for g in ab["regimes"]:
        for s in g["schedules"]:
            if s["covenant_id"] == cov_id:
                vals |= rows_at(s["rows"])
        for a in g["early_exit"]["after_levels"]:
            if a["covenant_id"] == cov_id:
                try:
                    vals.add(round(float(a["value"]), 4))
                except ValueError:
                    pass
        if cov_id in g["applies_to"] and g["type"] == "step_up":
            for v in (g["level"],):
                try:
                    vals.add(round(float(v), 4))
                except ValueError:
                    pass
            try:
                inc, ceil = float(g["increment"]), float(g["ceiling"]) if g["ceiling"] else None
                vals |= {round(min(x + inc, ceil) if ceil else x + inc, 4) for x in list(vals)}
            except ValueError:
                pass
    return vals


def check(out, ab, docs):
    P, review = [], []
    cids = {c["id"] for c in ab["covenants"]}
    rids = {g["id"] for g in ab["regimes"]}

    def q(where, doc, s):
        if doc not in docs:
            P.append(f"{where}: doc {doc!r} is not one of {sorted(docs)}")
        elif s and squash(s)[:80] not in docs[doc]:
            P.append(f"{where}: quote not found verbatim in {doc}: {s[:70]!r}")

    for e in out["events"]:
        w = f"event {e['type']}"
        q(w, e["doc"], e["quote"])
        if e["regime_id"] and e["regime_id"] not in rids:
            P.append(f"{w}: regime_id {e['regime_id']!r} is not one of {sorted(rids)}")
        if e["covenant_id"] and e["covenant_id"] not in cids:
            P.append(f"{w}: covenant_id {e['covenant_id']!r} is not one of {sorted(cids)}")
        if e["date"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["date"]):
            P.append(f"{w}: date {e['date']!r} must be YYYY-MM-DD or ''")
        if e["type"] in ("regime_started", "regime_ended", "election_made") and not e["regime_id"]:
            P.append(f"{w}: link it to one of the regime ids")
    for r in out["reported"]:
        w = f"reported {r['covenant_id']}"
        q(w, r["doc"], r["quote"])
        if r["covenant_id"] not in cids:
            P.append(f"{w}: covenant_id is not one of {sorted(cids)}")
            continue
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["period_end"]):
            P.append(f"{w}: period_end {r['period_end']!r} must be YYYY-MM-DD")
            continue
        try:
            float(r["value"])
        except ValueError:
            P.append(f"{w}: value {r['value']!r} must be a number")
        if r["threshold"]:
            try:
                th = round(float(r["threshold"]), 4)
            except ValueError:
                P.append(f"{w}: threshold {r['threshold']!r} must be a number")
                continue
            allowed = allowed_thresholds(ab, r["covenant_id"], date.fromisoformat(r["period_end"]))
            if allowed and th not in allowed:      # cross-check (#29): evidence for a reviewer, not something to repair
                review.append(f"10-Q threshold check: {r['covenant_id']} at {r['period_end']} the filing states {th}, the "
                              f"abstract allows {sorted(allowed)}")
    return P, review


def run(borrower, ab, model="sonnet", max_repairs=2):
    names = [c["metric"] for c in ab["covenants"]] + [g["name"] for g in ab["regimes"]]
    passages, docs = gather(borrower, names)
    covs = "; ".join(f"{c['id']} = Section {c['ref']} {c['name']} ({c['direction']})" for c in ab["covenants"])
    regs = "; ".join(f"{g['id']} = {g['name']} ({g['type']})" for g in ab["regimes"]) or "(none)"
    prompt = PROMPT.format(covenants=covs, regimes=regs, passages=passages)
    trace = []
    r = call_model(SYSTEM, prompt, SCHEMA, model=model, timeout=900)
    trace.append({k: r.get(k) for k in ("model", "cost_usd", "seconds", "ok")})
    if not r["ok"]:
        return {"error": r.get("error"), "trace": trace}
    out = r["output"]
    for _ in range(max_repairs):
        probs, _ = check(out, ab, docs)
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
    probs, review = check(out, ab, docs)
    trace[-1]["problems"] = probs
    return {"output": out, "review": probs + review, "fiscal_year_end_10k": fiscal_year_end(borrower),
            "docs": sorted(docs), "passage_chars": len(passages), "trace": trace,
            "cost": round(sum(t["cost_usd"] or 0 for t in trace), 4), "seconds": round(sum(t["seconds"] or 0 for t in trace), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("borrower")
    a = ap.parse_args()
    ab = json.loads((ROOT / "data" / "abstract" / "v1" / f"{a.borrower}.abstract.json").read_text(encoding="utf-8"))
    print(json.dumps(run(a.borrower, ab), indent=1)[:6000])


if __name__ == "__main__":
    main()
