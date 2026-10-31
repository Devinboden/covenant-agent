"""What the review screen (MCP App view) shows, built by code from the abstract, the sign-off record and the PDF.

- payload(b): deal facts, covenants with schedule rows, regimes, review items with resolutions, sign-off status and
  trail, and where each quote sits in the PDF (page + rectangles), so the view can highlight it.
- page_image(b, n): one PDF page as a JPEG data URI (the view draws the highlight boxes on top).
Quote locations are found by text search in the PDF (PyMuPDF), cached next to the abstract as <B>.locs.json.
"""
import base64
import json
import re
from functools import lru_cache

from . import config
from .abstract import signoff as SO
from .abstract.new_deal import review_data
from .store import store


def _dp(p):
    if p.get("kind") == "date":
        return p.get("date")
    if p.get("kind") == "open":
        return "open"
    return f"{p.get('count')} qtrs after {p.get('anchor')}"


def pdf_path(b, ab):
    p = config.ROOT / "data" / "pdf" / b / ab["header"]["source_file"]
    return p if p.exists() else None


def _quotes(ab, rv):
    q = {}
    for c in ab["covenants"]:
        q["cov:" + c["id"]] = c["quote"]
        for k, r in enumerate(c["schedule"]):
            q[f"row:{c['id']}:{k}"] = r["quote"]
    for g in ab["regimes"]:
        q["reg:" + g["id"]] = g["quote"]
        for s in g["schedules"]:
            for k, r in enumerate(s["rows"]):
                q[f"rrow:{g['id']}:{s['covenant_id']}:{k}"] = r["quote"]
    for it in rv["items"]:
        q["item:" + it["key"]] = it["evidence"]
    return q


def _search(page, text):
    text = re.sub(r"\s+", " ", text or "").strip()
    for n in (90, 60, 40, 25):
        if len(text) >= min(n, 12):
            r = page.search_for(text[:n])
            if r:
                return r
    return []


def _flat(t):
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def locate(b, ab, rv, abs_dir=config.ABSTRACT_DIR):
    """{key: {page, rects}} for every quote found in the PDF. Table rows quoted as 'cell | cell' are searched cell by
    cell on the same page. Cached per abstract hash."""
    st = store(abs_dir)
    quotes = _quotes(ab, rv)
    # The cache key covers the quotes too: a change in what the screen quotes (not just in the abstract) re-locates.
    import hashlib
    h = hashlib.sha256((SO.content_hash(ab) + json.dumps(quotes, sort_keys=True)).encode()).hexdigest()
    cached = st.load(b, "locs", default={})
    if cached.get("hash") == h:
        return cached
    path = pdf_path(b, ab)
    out = {"hash": h, "loc": {}, "pages": [], "npages": 0, "pw": 612, "ph": 792, "pdf": bool(path)}
    if not path:
        return out
    import pymupdf
    doc = pymupdf.open(path)
    out.update(npages=len(doc), pw=doc[0].rect.width, ph=doc[0].rect.height)
    flat = [_flat(page.get_text()) for page in doc]          # cheap pre-filter: only search pages that contain the text
    for key, q in quotes.items():
        parts = [x.strip() for x in (q or "").split("|") if x.strip()]
        if not parts:
            continue
        probe = _flat(parts[0])[:30]
        cands = [i for i, t in enumerate(flat) if probe and probe in t]
        hit = None
        for n in (90, 60, 40, 25):                            # longest match on any candidate page wins
            frag = re.sub(r"\s+", " ", parts[0]).strip()[:n]
            hit = next(((i, r) for i in cands for r in [doc[i].search_for(frag)] if r), None)
            if hit or len(frag) < n:
                break
        if hit:
            i, r = hit
            top, bot = min(x.y0 for x in r), max(x.y1 for x in r)
            for extra in parts[1:]:                           # other cells of the same table row: same line only
                r = r + [x for x in _search(doc[i], extra) if x.y1 > top - 2 and x.y0 < bot + 2]
            out["loc"][key] = {"page": i, "rects": [[round(x.x0, 1), round(x.y0, 1), round(x.x1, 1), round(x.y1, 1)] for x in r]}
    out["pages"] = sorted({v["page"] for v in out["loc"].values()})
    st.save(b, "locs", out)
    return out


@lru_cache(maxsize=64)
def _render(path, n, mtime):
    import pymupdf
    doc = pymupdf.open(path)
    pix = doc[n].get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6))
    return "data:image/jpeg;base64," + base64.b64encode(pix.tobytes("jpeg", jpg_quality=62)).decode()


def page_image(b, n, abs_dir=config.ABSTRACT_DIR):
    ab = store(abs_dir).load(b, "abstract")
    path = pdf_path(b, ab)
    if not path:
        raise FileNotFoundError(f"the PDF for {b} is not on this machine (run scripts/fetch_filings.py {b})")
    return _render(str(path), int(n), path.stat().st_mtime)


def payload(b, viewer, abs_dir=config.ABSTRACT_DIR):
    ab, rec, h = SO.load(b, abs_dir)
    rv = review_data(b, abs_dir)
    loan = ab.get("loan") or {}
    fac = (loan.get("facilities") or [{}])[0]
    val = lambda x: x.get("value") if isinstance(x, dict) else x
    covs = [{"id": c["id"], "ref": c["ref"], "metric": c["metric"], "direction": c["direction"], "frequency": c["frequency"],
             "unit": c.get("unit", ""), "applies_during": c.get("applies_during", ""),
             "rows": [{"from": _dp(r["start"]), "to": _dp(r["end"]), "value": r["value"], "loc": f"row:{c['id']}:{k}"}
                      for k, r in enumerate(c["schedule"])]} for c in ab["covenants"]]
    regs = [{"id": g["id"], "type": g["type"], "name": g["name"], "effect": g["effect"], "applies_to": g["applies_to"],
             "increment": g.get("increment", ""), "level": g.get("level", ""), "ceiling": g.get("ceiling", ""),
             "duration": g.get("duration"), "max_uses": g.get("max_uses", ""), "early_exit": g.get("early_exit"),
             "rows": [{"cov": s["covenant_id"], "from": _dp(r["start"]), "to": _dp(r["end"]), "value": r["value"],
                       "loc": f"rrow:{g['id']}:{s['covenant_id']}:{k}"} for s in g["schedules"] for k, r in enumerate(s["rows"])]}
            for g in ab["regimes"]]
    items = [{"key": i["key"], "kind": i["kind"], "type": i["type"], "where": i["where"], "what": i["what"],
              "evidence": i["evidence"], "resolution": i["resolution"]} for i in rv["items"]]
    its, open_, deferred, _ = SO.summary(ab, rec)
    trail = [{"date": ab["header"].get("built"), "by": "system",
              "what": f"abstract built from {ab['header'].get('source_file')}; {len(its)} items for review"}]
    trail += [{"date": r["date"], "by": r["by"], "what": f"{r['decision']}: {r['item'][:90]}"} for r in rec["resolutions"].values()]
    trail += [{"date": t["date"], "by": t["by"], "what": f"{t['from']} → {t['to']}" + (f" ({t['why']})" if t.get("why") else "")}
              for t in rec.get("transitions", [])]
    trail.sort(key=lambda t: t["date"] or "")
    return {
        "borrower": b, "name": loan.get("borrower") or b, "viewer": viewer,
        "status": rec["status"], "verified_by": rec.get("verified_by"), "signed_by": rec.get("signed_by"),
        "open": len(open_), "deferred": len(deferred), "total": len(its),
        "header": {"source_file": ab["header"].get("source_file"), "built": ab["header"].get("built"),
                   "spec": ab.get("spec_version"), "models": ab["header"].get("models", [])},
        "facts": {"agent": loan.get("agent"), "commitment": fac.get("commitment"), "facility": fac.get("type"),
                  "maturity": fac.get("maturity"), "latest_amendment": val(loan.get("latest_amendment_date")),
                  "closing": val(loan.get("closing_date"))},
        "covs": covs, "regs": regs, "items": items, "trail": trail, "locs": locate(b, ab, rv, abs_dir),
    }
