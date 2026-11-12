"""Build an abstract end to end (spec v1 draft): intake bundle -> step 2 (covenants + regimes) -> step 3 (terms the
covenants use) -> assembled abstract, validated against the spec schema, review list, cost.

The link between the steps is code: each covenant's metric is looked up in the definitions; the terms it uses are
collected; EBITDA-type terms go to step 3 now; the others (debt, cash netting, interest) are listed as pending.

Usage: python -m covenant_engine.abstract.build --deals WEST ROAD TRS CARS MEC QUAD SRI [--model sonnet] [--score]
"""
import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import jsonschema

from ..ingest.packet import CALC
from ..ingest.run import ROOT
from ..store import store
from ..core.decompose_term import bundle_dir
from . import covenants as S2
from . import decompose as S3
from . import calc_rules as C
from . import events as E
from . import loan as L
from . import pricing as PR

SPEC_VERSION = "v1.1-2026-10-13"
TERM_SCHEMA = {"type": "object", "required": ["name", "family", "starts_from", "components", "review"],
               "properties": {"name": {"type": "string"}, "family": {"type": "string"}, "starts_from": {"type": "string"},
                              "components": {"type": "array", "items": S3.COMPONENT}, "review": {"type": "array"}}}
ABSTRACT_SCHEMA = {
    "type": "object", "required": ["spec_version", "header", "loan", "covenants", "regimes", "terms", "terms_pending", "events",
                                   "reported", "calculation_rules", "deemed_values", "pricing_grid",
                                   "flags", "review"],
    "properties": {
        "spec_version": {"const": SPEC_VERSION},
        "header": {"type": "object", "required": ["borrower", "source_file", "sha256", "doc_type", "status", "built", "models"]},
        "covenants": {"type": "array", "items": S2.COVENANT},
        "regimes": {"type": "array", "items": S2.REGIME},
        "terms": {"type": "array", "items": TERM_SCHEMA},
        "terms_pending": {"type": "array", "items": {"type": "string"}},
        "loan": L.SCHEMA,
        "events": E.SCHEMA["properties"]["events"],
        "reported": E.SCHEMA["properties"]["reported"],
        "calculation_rules": C.SCHEMA["properties"]["rules"],
        "deemed_values": {"type": "array"},
        "pricing_grid": {"anyOf": [PR.STORED, {"type": "null"}]},
        "flags": {"type": "array", "items": S2.FLAG},
        "review": {"type": "array"}}}

DECOMPOSE_NOW = re.compile(r"EBITDA", re.I)
SKIP = re.compile(r"^(GAAP|Subsidiar|Lender|Administrative Agent|Agent|Borrower|Holdings|Parent|Loan Part|Statements|"
                  r"Fiscal|Test Period|Reference Period|Agreement)", re.I)


def normalise_covenant(s, metric_sec):
    """'all' | 'Section 6.13' | ratio names -> 'all' or 'Section x; Section y' (names that aren't a covenant metric drop)."""
    s = (s or "").strip()
    if s.lower() in ("", "all"):
        return "all"
    out = []
    for part in re.split(r";|,| and ", s):
        part = part.strip().strip("“”\"")
        m = re.search(r"\d+\.\d+[\d.]*(\([a-z]+\))?", part)
        if m:
            out.append(m.group(0))
        elif part.lower() in metric_sec:
            out.append(metric_sec[part.lower()])
    return "; ".join(f"Section {x}" for x in dict.fromkeys(out)) or "all"


def terms_for(cov, defs, packet_chunks):
    """Defined terms a covenant's test depends on: the metric's definition refs, or (if the metric is defined inline in
    the covenant, e.g. SRI's coverage ratio) the defined terms named in the covenant section itself."""
    found = set()
    m = cov["metric"].split(":")[0].strip().strip("“”\"")
    d = defs["defined"].get(m)
    if d:
        found |= set(d.get("refs", []))
    if not d or not any(DECOMPOSE_NOW.search(t) for t in found):
        text = " ".join(c["text"] for c in packet_chunks if c["kind"] == "covenant_section" and cov["ref"].split("(")[0] in c["id"])
        text += " " + cov["quote"] + " " + cov["metric"]
        found |= {t for t in defs["defined"] if len(t) > 6 and re.search(r"(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])", text)}
    return {t for t in found if not SKIP.match(t) and CALC.search(t) and t != m}


def build(borrower, model="sonnet", max_terms=25, reuse_step2=False, out="data/abstract/v1"):
    bdir = bundle_dir(borrower)
    report = json.loads((bdir / "report.json").read_text(encoding="utf-8"))
    defs = json.loads((bdir / "definitions.json").read_text(encoding="utf-8"))
    chunks = json.loads((bdir / "packet.json").read_text(encoding="utf-8"))
    prev = ROOT / "data" / "abstract" / "v1" / f"{borrower}.abstract.json"
    if reuse_step2 and prev.exists():               # step 2 unchanged: reuse its output (no model call)
        p = json.loads(prev.read_text(encoding="utf-8"))
        s2 = {"output": {k: p[k] for k in ("covenants", "regimes", "flags")},
              "review": [x["item"] for x in p["review"] if x.get("step") == 2], "cost": 0, "seconds": 0,
              "trace": [{"model": m, "reused": True} for m in p["header"]["models"] if "sonnet" in m or "opus" in m][:1] or [{"model": None}]}
    else:
        s2 = S2.run(borrower, model=model)
    if "error" in s2:
        return {"borrower": borrower, "error": s2["error"]}
    out2 = s2["output"]
    # Link: covenant metric -> its terms -> the terms those reference, breadth-first. Code decides what to decompose
    # (a term with a family); the model's term_reference parts and the ratio's definition refs extend the queue.
    used_by, queue = {}, []
    cov_ref = {c["id"]: c["ref"] for c in out2["covenants"]}
    cov_name = {c["id"]: c["name"] for c in out2["covenants"]}
    for c in out2["covenants"]:
        m = c["metric"].split(":")[0].strip().strip("“”\"")
        start = [m] if m in defs["defined"] and S3.family_of(m) else sorted(terms_for(c, defs, chunks))
        for t in start:
            used_by.setdefault(t, set()).add(c["id"])
            queue.append(t)
    s3, seen, pending, depth = {}, set(), set(), 0
    while queue and depth < 4 and len(s3) < max_terms:
        level = []
        for t in dict.fromkeys(queue):
            if t in seen:
                continue
            seen.add(t)
            if S3.family_of(t) and t in defs["defined"]:
                level.append(t)
            else:
                pending.add(t)
        pending.update(level[max_terms - len(s3):])          # cut by the term limit: recorded, never silent
        level = level[:max_terms - len(s3)]
        with ThreadPoolExecutor(4) as ex:
            res = dict(zip(level, ex.map(lambda t: S3.run(borrower, t, model=model, used_by=[
                f"Section {cov_ref[c]} ({cov_name[c]})" for c in sorted(used_by.get(t, []))]), level)))
        s3.update(res)
        queue = []
        for t, r in res.items():
            nxt = list(r.get("ref_terms", []))
            ft = S3.family_of(t)
            if ft in ("ratio", "debt", "cash"):     # follow the definition's own refs: ratios -> any family; debt -> debt;
                nxt += [x for x in defs["defined"][t].get("refs", []) if S3.family_of(x) and x in defs["defined"]   # cash -> cash
                        and (ft == "ratio" or S3.family_of(x) == ft)]
            for x in nxt:
                used_by.setdefault(x, set()).update(used_by.get(t, set()))
                queue.append(x)
        depth += 1
    pending.update(t for t in queue if t not in seen and t in defs["defined"])   # cut by the depth limit
    # code: cap 'covenant' may name ratios the text uses ("Secured Net Leverage Ratio"); map to the covenants' sections
    metric_sec = {c["metric"].split(":")[0].strip().lower(): c["ref"] for c in out2["covenants"]}
    for r in s3.values():
        for comp in r.get("components", []):
            for cap in comp["caps"]:
                cap["covenant"] = normalise_covenant(cap["covenant"], metric_sec)
    review = [{"step": 2, "item": p} for p in s2["review"]]
    terms = []
    for t, r in s3.items():
        if "error" in r:
            review.append({"step": 3, "item": f"{t}: model call failed"})
            continue
        terms.append({"name": t, "family": r["family"], "starts_from": r["starts_from"], "components": r["components"],
                      "review": r["review"], "used_by": sorted(used_by.get(t, [])), "code_clauses": r["code_clauses"]})
        review += [{"step": 3, "term": t, **x} for x in r["review"]]
    needed = set(s3) | pending
    now = list(s3)
    refs = [c["ref"] for c in out2["covenants"]]
    with ThreadPoolExecutor(4) as ex:                 # loan (#20), calc rules + deemed (#21/#22), pricing (#24), events (#23)
        f_ln = ex.submit(L.run, borrower, refs, model=model)
        f_c = ex.submit(C.run, borrower, refs, model=model)
        f_pr = ex.submit(PR.run, borrower, model=model)
        f_ev = ex.submit(E.run, borrower, {"covenants": out2["covenants"], "regimes": out2["regimes"]}, model=model)
    blank = {"output": None, "review": [], "code_notes": [], "trace": [], "cost": 0, "seconds": 0}
    parts = {}
    for step, f in (("loan", f_ln), ("calc", f_c), ("pricing", f_pr), ("events", f_ev)):
        r = f.result()
        if "error" in r:
            review.append({"step": step, "item": f"model call failed: {r['error']}"})
            r = {**blank, "deemed": r.get("deemed", [])}
        r.setdefault("cost", 0)
        review += [{"step": step, "item": p} for p in r["review"]]
        parts[step] = r
    ln, cr, pr, ev = parts["loan"], parts["calc"], parts["pricing"], parts["events"]
    # code: a schedule row that runs "from the Closing Date" starts at the first quarter end on/after the Closing Date
    # (the loan step has the closing date; step 2 can't know it when the definition says "the date of this Agreement")
    cdv = ((ln["output"] or {}).get("closing_date") or {}).get("value") or ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cdv):
        first = S2.first_quarter_end(date.fromisoformat(cdv))
        for c in out2["covenants"]:
            if c["frequency"] != "quarterly":
                continue
            for r in c["schedule"]:
                if "Closing Date" in r["quote"] and r["start"]["kind"] == "date" and r["start"]["date"] != first.isoformat():
                    ln["code_notes"] = ln.get("code_notes", []) + [
                        f"{c['id']}: row '{r['quote'][:50]}' start {r['start']['date']} -> {first} (first quarter end on/after Closing Date {cdv})"]
                    r["start"]["date"] = first.isoformat()
        # code: "n quarters after the Closing Date" -> the date (n-th FULL quarter when the row says "full")
        for c in out2["covenants"]:
            for r in c["schedule"]:
                for side in ("start", "end"):
                    pt = r[side]
                    if pt["kind"] == "quarters_after" and "closing" in pt["anchor"].lower() and pt["count"].isdigit() \
                            and not pt["date"]:
                        pt["date"] = S2.nth_quarter_end(date.fromisoformat(cdv), pt["count"], "full" in r["quote"].lower()).isoformat()
                        ln["code_notes"] = ln.get("code_notes", []) + [
                            f"{c['id']}: {side} '{pt['count']} quarters after Closing Date' -> {pt['date']}"]
    fy10k = ev.get("fiscal_year_end_10k")
    if ln["output"] and not ln["output"]["fiscal_year_end"]["value"] and fy10k:   # agreement silent -> 10-K cover
        ln["output"]["fiscal_year_end"] = {"value": fy10k["value"], "quote": fy10k["quote"]}
        ln["code_notes"] = ln["code_notes"] + [f"fiscal_year_end from {fy10k['doc']} cover (agreement silent)"]
    abstract = {
        "spec_version": SPEC_VERSION,
        "header": {"borrower": borrower, "source_file": report["source_file"], "sha256": report["sha256"],
                   "doc_type": report["document"], "covenant_sections": report["covenant_sections"],
                   "status": "draft", "built": date.today().isoformat(),
                   "models": sorted(({t["model"] for t in s2["trace"]} | {x["model"] for r in s3.values() for x in r.get("trace", [])}) - {None})},
        "loan": ln["output"], "loan_code_notes": ln["code_notes"],
        "covenants": out2["covenants"], "regimes": out2["regimes"], "terms": terms,
        "terms_pending": sorted(pending),
        "events": (ev["output"] or {}).get("events", []), "reported": (ev["output"] or {}).get("reported", []),
        "event_docs": ev.get("docs", []),
        "calculation_rules": (cr["output"] or {}).get("rules", []), "deemed_values": cr.get("deemed", []),
        "pricing_grid": pr["output"],
        "flags": out2["flags"], "review": review}
    errs = sorted(jsonschema.Draft202012Validator(ABSTRACT_SCHEMA).iter_errors(abstract), key=lambda e: list(e.path))
    abstract["review"] += [{"step": "schema", "item": f"{'/'.join(map(str, e.path))}: {e.message[:160]}"} for e in errs[:20]]
    cost = s2["cost"] + sum(r.get("cost", 0) for r in s3.values()) + sum(r["cost"] for r in parts.values())
    run_info = {"borrower": borrower, "cost": round(cost, 4),
                "seconds": round(s2["seconds"] + sum(r.get("seconds", 0) for r in s3.values()), 1),
                "step2_rounds": len(s2["trace"]), "step2_problems_round1": len(s2["trace"][0].get("problems", [])), "step2_reused": reuse_step2,
                "step3_rounds": {t: len(r.get("trace", [])) for t, r in s3.items()}}
    out_dir = ROOT / out
    out_dir.mkdir(parents=True, exist_ok=True)
    failed = [x for x in abstract["review"] if "model call failed" in str(x.get("item", ""))]
    if failed:                                     # never overwrite a good abstract with a partial one (usage limits)
        store(out).save(borrower, "abstract.failed", abstract)
        return {"borrower": borrower, "error": f"{len(failed)} model calls failed (e.g. {failed[0]['item'][:120]}); "
                                               f"previous abstract kept, partial one in {borrower}.abstract.failed.json"}
    store(out).save(borrower, "abstract", abstract)
    from .signoff import sync                      # #31: status comes from the sign-off record, never from the build
    abstract["header"]["status"] = sync(borrower, out)
    (out_dir / f"{borrower}.run.json").write_text(json.dumps({**run_info, "step2_trace": s2["trace"],
                                                              "step3_trace": {t: r.get("trace") for t, r in s3.items()}}, indent=1),
                                                  encoding="utf-8")
    return {**run_info, "abstract": abstract}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deals", nargs="+", default=["WEST", "ROAD", "TRS", "CARS", "MEC", "QUAD", "SRI"])
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--reuse-step2", action="store_true")
    ap.add_argument("--out", default="data/abstract/v1", help="output folder (step 2 is always reused from data/abstract/v1)")
    a = ap.parse_args()
    with ThreadPoolExecutor(4) as ex:
        R = list(ex.map(lambda b: build(b, a.model, reuse_step2=a.reuse_step2, out=a.out), a.deals))
    from .score import score_abstract
    lines = []
    for r in R:
        if "error" in r:
            lines.append(f"{r['borrower']}: ERROR {r['error']}")
            continue
        ab = r["abstract"]
        s = score_abstract(r["borrower"], ab)
        nrev = [x for x in ab["review"] if "custom" not in str(x.get("why", ""))]
        lines.append(f"{r['borrower']:5} covs {len(ab['covenants'])} regimes {[g['type'] for g in ab['regimes']]} flags {len(ab['flags'])} | "
                     f"terms {len(ab['terms'])} pending {ab['terms_pending']} | review {len(nrev)} | "
                     f"s2 rounds {r['step2_rounds']} (r1 problems {r['step2_problems_round1']}) | ${r['cost']} {r['seconds']}s")
        lines.append(f"        score: {s['summary']}")
        for t in ab["terms"]:
            nrv = [x for x in t["review"] if "custom" not in x.get("why", "")]
            lines.append(f"        term {t['family']:8} {t['name']!r}: {len(t['components'])} parts, code clauses {len(t['code_clauses'])}, "
                         f"custom {sum(c['category'] == 'custom' for c in t['components'])}, open review {len(nrv)}"
                         f"{' | key ' + s['terms'][t['name']] if t['name'] in s.get('terms', {}) else ''} | used by {t['used_by']}")
        for x in s["misses"][:10]:
            lines.append(f"          - {x}")
        for x in nrev[:8]:
            lines.append(f"          review: {json.dumps(x)[:200]}")
    txt = "\n".join(lines)
    print(txt)
    (ROOT / a.out / "summary.txt").write_text(txt, encoding="utf-8")


if __name__ == "__main__":
    main()
