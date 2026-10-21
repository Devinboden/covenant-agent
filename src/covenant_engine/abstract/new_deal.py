"""Single entry point (#33): one new deal, PDF in -> abstract out, ready for human review.

  python -m covenant_engine.abstract.new_deal CXT data/pdf/CXT/amendment_6th-amendment-2026-07.pdf

Steps: register the deal -> ingest (with the Haiku exception path) -> build the abstract (covenants, terms, loan,
events, calculation rules, pricing; all code checks) -> GPT second-model check if Codex is signed in (CODEX_HOME)
-> sign-off record (status draft; every flag and review item listed for a person to resolve) -> review file
(<B>.review.json) for the review page. Filings for the events section are read from data/pdf/<B>/10-Q_*.pdf,
10-K_*.pdf, 8-K_*.pdf when present. Nothing here marks an abstract verified: only a person does (signoff.py).
"""
import argparse
import json
import os
import shutil
import subprocess

from ..ingest.run import ROOT, run as ingest
from ..core.decompose_term import DEALS
from . import build as BUILD
from . import gpt_review as G
from ..store import store
from .signoff import items, load, save

from ..config import ABSTRACT_DIR, CODEX_BIN as CODEX


def register(borrower, pdf):
    deals = json.loads(DEALS.read_text(encoding="utf-8")) if DEALS.exists() else {}
    deals[borrower] = str(pdf)
    DEALS.write_text(json.dumps(deals, indent=1), encoding="utf-8")


def gpt_check(borrower, out_dir="data/gpt_review"):
    home = os.environ.get("CODEX_HOME")
    if not home or not CODEX or not os.path.exists(CODEX):
        return {"skipped": "Codex not signed in (set CODEX_HOME)"}
    G.prepare([borrower], out_dir)
    d = ROOT / out_dir / borrower
    with open(d / "prompt.md") as fin, open(d / "codex.log", "w") as log:
        r = subprocess.run([CODEX, "exec", "--skip-git-repo-check", "--ephemeral", "--output-schema", "schema.json",
                            "-o", "verdicts.json", "-"], cwd=d, stdin=fin, stdout=log, stderr=subprocess.STDOUT,
                           timeout=2400)
    if r.returncode or not (d / "verdicts.json").exists():
        return {"error": f"codex exit {r.returncode}; see {d / 'codex.log'}"}
    tally, _ = G.score(borrower, out_dir, write=True)
    return tally


def review_file(borrower, abs_dir=ABSTRACT_DIR):
    """Everything a reviewer needs, one row per item: key (stable, for the sign-off record), kind, where, what code /
    GPT says, the evidence quote, plus the abstract's headline facts."""
    ab, rec, h = load(borrower, abs_dir)
    its = items(ab)
    rows = []
    for f in ab.get("flags", []):
        from .signoff import item_key
        k = item_key("flag", f)
        rows.append({"key": k, "kind": "flag", "type": f["type"], "where": f["ref"], "what": f["note"], "evidence": f["quote"],
                     "resolution": rec["resolutions"].get(k)})
    for r in ab.get("review", []):
        from .signoff import item_key
        k = item_key("review", r)
        rows.append({"key": k, "kind": f"review/{r.get('step')}", "type": r.get("step"),
                     "where": " ".join(x for x in (r.get("term", ""), r.get("clause", "") or r.get("id", "")) if x),
                     "what": r.get("why") or r.get("item") or r.get("claim") or "",
                     "gpt_reads": r.get("gpt_reads", ""), "evidence": r.get("evidence", "") or r.get("note", ""),
                     "resolution": rec["resolutions"].get(k)})
    cov = [{"id": c["id"], "ref": c["ref"], "name": c["name"], "metric": c["metric"], "direction": c["direction"],
            "frequency": c["frequency"], "event_tests": c.get("event_tests", []), "applies_during": c.get("applies_during", ""),
            "schedule": [{"from": r["start"].get("date") or f"{r['start']['count']} {r['start']['kind']} {r['start']['anchor']}".strip(),
                          "to": r["end"].get("date") or r["end"]["kind"], "value": r["value"]} for r in c["schedule"]],
            "quote": c["quote"]} for c in ab["covenants"]]
    reg = [{"id": g["id"], "type": g["type"], "name": g["name"], "applies_to": g["applies_to"], "effect": g["effect"],
            "level": g.get("level", ""), "increment": g.get("increment", ""),
            "rows": [{"covenant": s["covenant_id"], "from": r["start"].get("date"), "to": r["end"].get("date"), "value": r["value"]}
                     for s in g["schedules"] for r in s["rows"]]} for g in ab["regimes"]]
    terms = [{"name": t["name"], "family": t["family"], "parts": len(t["components"]),
              "components": [{"clause": c["clause"], "category": c["category"], "sign": c["sign"], "ref_term": c["ref_term"],
                              "caps": [f"{k['type']} {k['value']}" for k in c["caps"]], "quote": c["quote"][:200]}
                             for c in t["components"]]} for t in ab["terms"]]
    out = {"borrower": borrower, "status": rec["status"], "spec_version": ab["spec_version"],
           "source_file": ab["header"]["source_file"], "built": ab["header"]["built"],
           "loan": ab.get("loan"), "covenants": cov, "regimes": reg, "terms": terms,
           "pricing_grid": ab.get("pricing_grid"), "reported": ab.get("reported", []),
           "second_model": ab.get("second_model_review"), "items": rows, "open": sum(1 for r in rows if not r["resolution"])}
    st = store(abs_dir)
    st.save(borrower, "review", out)
    return st._p(borrower, "review"), out


def abstract_deal(borrower, pdf, run_ingest=True, gpt=True, progress=print):
    """The front door. PDF in -> abstract + sign-off record + review file. Never marks anything verified.
    Returns {"status": "ready_for_review" | "stopped" | "failed", "deal", "reason"?, "cost"?, "review"?, "steps": [...]}.
    "stopped" means a person must look before anything else runs (e.g. no covenant section found)."""
    steps = []

    def step(name, **kw):
        steps.append({"step": name, **kw})
        progress(f"{name}: {kw}")

    register(borrower, pdf)
    if run_ingest:
        out, rep = ingest(ROOT / pdf, borrower, use_model_fallback=True)
        cs = rep["covenant_sections"]
        step("ingest", doc_type=rep["document"]["doc_type"], sections=rep["structure"]["sections"],
             definitions=rep["structure"]["definitions"], covenant_sections=cs["ids"], confidence=cs["confidence"],
             missing_attachments=rep["attachments_referenced_in_packet_but_missing"][:6])
        if not cs["ids"]:
            return {"status": "stopped", "deal": borrower, "steps": steps,
                    "reason": "no financial covenant section found: a person must look at the document"}
    r = BUILD.build(borrower)
    if "error" in r:
        step("build", error=r["error"])
        return {"status": "failed", "deal": borrower, "steps": steps, "reason": r["error"]}
    ab = r["abstract"]
    step("build", cost=r["cost"], covenants=len(ab["covenants"]), terms=len(ab["terms"]), review_items=len(ab["review"]))
    if gpt:
        step("gpt_check", **gpt_check(borrower))
    ab, rec, h = load(borrower)
    save(borrower, ab, rec)
    p, out = review_file(borrower)
    step("review", status=out["status"], items=len(out["items"]), open=out["open"])
    return {"status": "ready_for_review", "deal": borrower, "cost": r["cost"], "steps": steps,
            "review": {"items": len(out["items"]), "open": out["open"], "file": str(p)}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("borrower")
    ap.add_argument("pdf")
    ap.add_argument("--skip-ingest", action="store_true")
    ap.add_argument("--skip-gpt", action="store_true")
    a = ap.parse_args()
    r = abstract_deal(a.borrower, a.pdf, run_ingest=not a.skip_ingest, gpt=not a.skip_gpt)
    print(r["status"], r.get("reason", ""))
    if r["status"] != "ready_for_review":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
