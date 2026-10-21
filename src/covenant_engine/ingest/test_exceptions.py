"""Benchmark the Haiku exception path against code + filenames (truth).

  A. classify_document on all 45 filings (truth = filename prefix)
  B. find_covenant_sections on the 5 agreements, BLIND (no code answer given) vs the code answer
  C. repair_definition with the CARS mid-paragraph bug injected (run separately via run.py --inject-midblock-bug --model)

Usage: python -m covenant_engine.ingest.test_exceptions
"""
import glob
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import exceptions as X
from .extract import extract
from .run import CURRENT, ROOT
from .locate import find_covenant_sections as code_locate
from .structure import find_definitions, find_sections


def truth(path):
    n = Path(path).name
    return next(t for p, t in [("10-Q", "10-Q"), ("10-K", "10-K"), ("8-K", "8-K"), ("amendment", "amendment"),
                               ("credit_agreement", "credit_agreement")] if n.startswith(p))


def prep_cover(path):
    """Cover page from the first 6 pages; definition count from plain text of the whole file (fast)."""
    import re
    import pymupdf
    from .structure import DEF_ANY
    blocks, stats = extract(path, max_pages=6)
    full = " ".join(p.get_text() for p in pymupdf.open(path))
    n_defs = len({m.group(1) for m in DEF_ANY.finditer(re.sub(r"\s+", " ", full))})
    return path, blocks, n_defs, stats["struck_chars"]


def classify_one(prepped):
    path, blocks, n_defs, struck = prepped
    e = X.classify_document(blocks, n_defs, struck)
    got = e.get("result", {}).get("doc_type") if e["decided_by"] == "applied" else None
    return {"file": str(Path(path).relative_to(ROOT / "data" / "pdf")), "truth": truth(path), "model": got,
            "decided_by": e["decided_by"], "why": e.get("why"), "cost": e["model_call"].get("cost_usd"),
            "seconds": e["model_call"].get("seconds")}


def prep_sections(item):
    b, pdf = item
    blocks, _ = extract(ROOT / pdf)
    return b, blocks, find_sections(blocks)


def locate_one(prepped):
    b, blocks, sections = prepped
    code_ids, conf, _ = code_locate(sections, blocks)
    e = X.find_covenant_sections(sections, blocks, [], "blind test")
    return {"borrower": b, "code": code_ids, "code_conf": conf, "model": e.get("model_answer"),
            "verified": e["decided_by"] != "needs_review", "why": e.get("why"),
            "cost": e["model_call"].get("cost_usd"), "seconds": e["model_call"].get("seconds")}


def main():
    paths = sorted(glob.glob(str(ROOT / "data" / "pdf" / "*" / "*.pdf")))
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(8) as px:                       # PDF parsing is CPU-bound
        covers = list(px.map(prep_cover, paths))
        secs = list(px.map(prep_sections, CURRENT.items()))
    with ThreadPoolExecutor(8) as ex:                        # model calls are I/O-bound
        A = list(ex.map(classify_one, covers))
        B = list(ex.map(locate_one, secs))
    right = sum(a["model"] == a["truth"] for a in A)
    print(f"A. classify: {right}/{len(A)} correct and verified; cost ${sum(a['cost'] or 0 for a in A):.3f}; "
          f"median {sorted(a['seconds'] or 0 for a in A)[len(A) // 2]}s")
    for a in A:
        if a["model"] != a["truth"]:
            print(f"   MISS {a['file']}: truth {a['truth']} model {a['model']} ({a['decided_by']}: {a['why']})")
    print("B. covenant sections, blind:")
    for r in B:
        agree = sorted(r["model"] or []) == sorted(r["code"])
        print(f"   {r['borrower']:5} code {r['code']} ({r['code_conf']}) | haiku {r['model']} | agree={agree} | verified={r['verified']}"
              f"{' (' + r['why'] + ')' if r['why'] else ''} | ${r['cost']}")
    out = ROOT / "data" / "intake" / "exception_benchmark.json"
    out.write_text(json.dumps({"classify": A, "locate_blind": B}, indent=1), encoding="utf-8")
    print(f"saved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
