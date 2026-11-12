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
from .abstract import corrections as C
from . import brief as BR
from . import plain as PL
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


DNAME = "@name"            # marker: locate the defined term ('"Indebtedness" of any Person means') by pattern


def _defined_at(doc, flat, name):
    """(page, rects) of the defined term where the agreement defines it: “Name” [of any Person] means."""
    import re as _re
    want = _flat(name)
    for i, t in enumerate(flat):
        if want + "means" not in t and want + "ofanypersonmeans" not in t and want + "ofanypersonasofanydatemeans" not in t:
            continue
        ws = doc[i].get_text("words", sort=True)
        for k, w in enumerate(ws):
            if not _flat(w[4]).startswith(_flat(name.split()[0])):
                continue
            m = _match_words(ws[k:k + 12], name)
            if m and m[0] == 0:
                z = k + m[1]
                nxt = " ".join(x[4] for x in ws[z + 1:z + 6]).lower()
                if _re.match(r"(of any person (as of any date )?)?means", nxt):
                    return i, _boxes(ws, k, z)
    return None


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
    for t in ab.get("terms", []):                      # Covenants tab: each part of a definition, and what makes it count
        for k, c in enumerate(t["components"]):
            if c.get("quote"):
                q[f"dcl:{t['name']}:{k}"] = c["quote"]
        for lead, sign in (("lead-in", "+"), ("minus lead-in", "-")):
            c = next((c for c in t["components"] if c.get("clause") == lead and c.get("quote")), None)
            if c:
                q[f"dlead:{t['name']}:{sign}"] = c["quote"]
        q[f"dname:{t['name']}"] = DNAME                 # found by pattern: the defined term where it is defined
    for it in rv["items"]:
        for n, e in enumerate(evidences(ab, it)):
            q[f"item:{it['key']}" + (f":{n}" if n else "")] = e["quote"]
    return q


def _component(ab, row):
    """(term, component) the item is about: a term check names term + clause; a second-reading item names term:NAME:i."""
    m = re.match(r"term:(.+):(\d+)$", row.get("id") or "")
    for t in ab.get("terms", []):
        if m and t["name"] == m.group(1) and int(m.group(2)) < len(t["components"]):
            return t, t["components"][int(m.group(2))]
        if row.get("term") == t["name"]:
            c = next((c for c in t["components"] if c.get("clause") == row.get("clause")), None)
            if c:
                return t, c
    return None, None


def evidences(ab, row):
    """Every passage the item's answer rests on, first one first: the abstract's own quote for this part; the definition
    of any defined term it points to (e.g. a cash deduction -> the 'Unrestricted Cash' definition and its cap); and any
    other passage the second reading cited. [{label, quote}], de-duplicated."""
    out, seen = [], set()

    def add(label, quote):
        k = _flat(quote)[:60]
        if quote and k and not any(k in x or x in k for x in seen):
            seen.add(k)
            out.append({"label": label, "quote": quote})
    t, c = _component(ab, row)
    if c:
        add("This part of the agreement", c.get("quote", ""))
        ref = c.get("ref_term")
        if ref and ref != t["name"]:
            rt = next((x for x in ab.get("terms", []) if x["name"] == ref), None)
            if rt and rt.get("components"):
                add(f"Definition of {ref}", rt["components"][0].get("quote", ""))
        for cap in c.get("caps", []):
            add("The limit", cap.get("quote", ""))
        if row.get("evidence"):
            add("Also cited", row["evidence"])
    else:
        add("The agreement", row.get("evidence", ""))
        w = row.get("what") or ""
        m = re.search(r"values for (.+?) are on (Schedule [\w.()]+)", w)
        if m:                                   # a missing schedule: the sentence that points to it
            for d in ab.get("deemed_values", []):
                if d.get("term") == m.group(1) and d.get("schedule") == m.group(2):
                    add("The agreement", d.get("quote", ""))
    return out


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


ABBR = re.compile(r"^(?:\(?[a-z0-9]{1,4}\)|No|Nos|Sec|Secs|U\.S|Inc|Corp|Co|Ltd|L\.P|N\.A|e\.g|i\.e|etc|vs)\.$", re.I)
LOCATE_V = "words-7"      # bump to re-locate every quote when the matching changes


def _ends_sentence(w):
    return w.endswith((".", ".\u201d", '."')) and not ABBR.match(w)


def _match_words(words, quote):
    """Indexes (first, last) of the page words that spell `quote` (letters and digits only, case-insensitive)."""
    q = _flat(quote)
    if len(q) < 8:
        return None
    flat, owner = [], []
    for k, w in enumerate(words):
        f = _flat(w[4])
        flat.append(f)
        owner += [k] * len(f)
    text = "".join(flat)
    for probe in (q, q[:160], q[:80], q[-80:], q[:40]):
        at = text.find(probe) if len(probe) >= 12 else -1
        if at >= 0:
            if probe is q or probe == q[:160] or probe == q[:80] or probe == q[:40]:
                end = min(len(text), at + len(q)) - 1
            else:
                at, end = max(0, at + len(probe) - len(q)), at + len(probe) - 1
            return owner[at], owner[min(end, len(owner) - 1)]
    return None


ENUM = re.compile(r"^\((?:[a-z]{1,2}|[ivxl]{1,6}|\d{1,2}|[A-Z])\)$")


def _starts_clause(words, k):
    """Word k opens a lettered clause like '(j)' or '(iv)' that follows a comma, semicolon, colon, 'and' or 'or'."""
    if k <= 0 or not ENUM.match(words[k][4]):
        return False
    prev = words[k - 1][4]
    return prev.endswith((",", ";", ":")) or prev in ("and", "or")


def _sentence(words, a, z, cap=80):
    """Widen word range [a, z] to the sentence it sits in. In a long definition, a lettered clause ('(j) ...') counts
    as its own sentence, so the reader gets the whole clause and not the whole definition."""
    def page_number(k):                      # a lone number on its own line: the printed page number
        w = words[k]
        return w[4].isdigit() and sum(1 for x in words if (x[5], x[6]) == (w[5], w[6])) == 1
    s = a
    while s > 0 and a - s < cap and not _ends_sentence(words[s - 1][4]) and not _starts_clause(words, s) \
            and not page_number(s - 1):
        s -= 1
    e = z
    while e < len(words) - 1 and e - z < cap and not _ends_sentence(words[e][4]) and not _starts_clause(words, e + 1) \
            and not page_number(e + 1):
        e += 1
    return s, e


def _boxes(words, a, z):
    """One box per printed line for words a..z."""
    lines = {}
    for w in words[a:z + 1]:
        k = (w[5], w[6])
        x0, y0, x1, y1 = lines.get(k, (w[0], w[1], w[2], w[3]))
        lines[k] = (min(x0, w[0]), min(y0, w[1]), max(x1, w[2]), max(y1, w[3]))
    return [[round(v, 1) for v in b] for b in lines.values()]


def locate(b, ab, rv, abs_dir=config.ABSTRACT_DIR):
    """{key: {page, rects, sentence}} for every quote found in the PDF: the quote is matched word by word on the page
    and widened to its full sentence, which is what the screen highlights and shows. Table rows quoted as
    'cell | cell' highlight each cell on the same line. Cached per abstract hash."""
    st = store(abs_dir)
    quotes = _quotes(ab, rv)
    import hashlib
    path = pdf_path(b, ab)
    # The key covers the PDF's bytes too: positions belong to one rendering of the agreement. A re-rendered PDF
    # (each deploy can render it again) shifts the text, and stale positions highlight the wrong lines.
    pdf_id = hashlib.sha256(path.read_bytes()).hexdigest() if path else ""
    h = hashlib.sha256((LOCATE_V + pdf_id + SO.content_hash(ab) + json.dumps(quotes, sort_keys=True)).encode()).hexdigest()
    cached = st.load(b, "locs", default={})
    if cached.get("hash") == h:
        return cached
    out = {"hash": h, "loc": {}, "pages": [], "npages": 0, "pw": 612, "ph": 792, "pdf": bool(path)}
    if not path:
        return out
    import pymupdf
    doc = pymupdf.open(path)
    out.update(npages=len(doc), pw=doc[0].rect.width, ph=doc[0].rect.height)
    flat = [_flat(page.get_text()) for page in doc]          # cheap pre-filter: only search pages that contain the text
    words = {}
    for key, q in quotes.items():
        if q == DNAME:
            hit = _defined_at(doc, flat, key.split(":", 1)[1])
            if hit:
                out["loc"][key] = {"page": hit[0], "rects": hit[1], "sentence": key.split(":", 1)[1]}
            continue
        parts = [x.strip() for x in (q or "").split("|") if x.strip()]
        if not parts:
            continue
        probe = _flat(parts[0])[:30]
        for i in (i for i, t in enumerate(flat) if probe and probe in t):
            ws = words.setdefault(i, doc[i].get_text("words", sort=True))
            m = _match_words(ws, parts[0])
            if not m:
                continue
            if len(parts) > 1:                                 # a table row: highlight each cell on that line
                rects = _boxes(ws, *m)
                line_y = ws[m[0]][1]
                for extra in parts[1:]:
                    for k, w in enumerate(ws):
                        if abs(w[1] - line_y) < 3:
                            mm = _match_words(ws[k:k + 12], extra)
                            if mm:
                                rects += _boxes(ws, k + mm[0], k + mm[1])
                                break
                out["loc"][key] = {"page": i, "rects": rects, "sentence": re.sub(r"\s+", " ", q)}
            elif key.startswith("dlead:"):                   # the words that make a part count, not the whole sentence
                out["loc"][key] = {"page": i, "rects": _boxes(ws, *m), "sentence": re.sub(r"\s+", " ", q)}
            else:
                s, e = _sentence(ws, *m)
                out["loc"][key] = {"page": i, "rects": _boxes(ws, s, e),
                                   "sentence": re.sub(r"\s+", " ", " ".join(w[4] for w in ws[s:e + 1])).strip()}
            break
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


SOURCE = {"income_statement": "the income statement", "borrower_schedule": "a borrower schedule (not in the financials)",
          "cash_flow_statement": "the cash flow statement", "balance_sheet": "the balance sheet",
          "compliance_certificate": "the compliance certificate"}
WHY = {}


def _grams(t, n=5):
    w = re.findall(r"[a-z0-9]+", (t or "").lower())
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def _categories():
    from .core.decompose_term import CATEGORIES
    return list(CATEGORIES)


def term_context(ab):
    """What an analyst needs to decide a term-check item, from the abstract (code only): how each clause of a defined
    term is stored, which covenants the term feeds, and clauses that share wording (e.g. an add-back and its matching
    deduction). Returns ({(term, clause): detail}, {term: [component rows]})."""
    metric = {c["id"]: f"{c['metric']} ({c['ref']})" for c in ab["covenants"]}
    flagged = {(r.get("term"), r.get("clause")) for r in ab.get("review", []) if r.get("step") == 3}
    details, terms = {}, {}
    for t in ab.get("terms", []):
        comps = t.get("components", [])
        rows = [{"clause": c.get("clause", ""), "sign": c.get("sign", ""), "category": c.get("category", ""),
                 "quote": (c.get("quote") or "")[:160], "flagged": (t["name"], c.get("clause")) in flagged} for c in comps]
        if any(r["flagged"] for r in rows):
            terms[t["name"]] = {"starts_from": t.get("starts_from", ""), "rows": rows}
        g = {c.get("clause"): _grams(c.get("quote")) for c in comps}
        df = {}                                    # phrases shared by only two clauses are a real link, not boilerplate
        for v in g.values():
            for x in v:
                df[x] = df.get(x, 0) + 1
        g = {k: {x for x in v if df[x] == 2} for k, v in g.items()}
        for c in comps:
            if (t["name"], c.get("clause")) not in flagged:
                continue
            related, seen = [], set()
            for o in comps:
                if o is not c and o.get("clause") not in seen and g[c.get("clause")] & g[o.get("clause")]:
                    seen.add(o.get("clause"))
                    related.append({"clause": o.get("clause"), "sign": o.get("sign"), "category": o.get("category"),
                                    "flagged": (t["name"], o.get("clause")) in flagged})
            details[(t["name"], c.get("clause"))] = {
                "term": t["name"], "clause": c.get("clause"),
                "effect": {"+": "Add-back (increases EBITDA)", "-": "Deduction (decreases EBITDA)"}.get(c.get("sign"), c.get("sign") or "—"),
                "category": c.get("category", ""), "source": SOURCE.get(c.get("source"), c.get("source") or "—"),
                "caps": [x.get("quote") or x.get("type") or "" for x in c.get("caps", [])],
                "needs_judgment": bool(c.get("needs_judgment")),
                "affects": [metric.get(u, u) for u in t.get("used_by", [])], "related": related}
    return details, terms


def gpt_for(ab, item):
    """GPT's independent verdicts on the claims this item is about (from the second-model check), or None if the check
    hasn't run. A term clause -> its one claim; a flag / GPT item on a covenant -> that covenant's and its regimes' claims."""
    smr = (ab.get("second_model_review") or {}).get("claims")
    if not smr:
        return None
    ids = []
    if item.get("term") and item.get("clause"):
        for t in ab.get("terms", []):
            if t["name"] == item["term"]:
                ids += [f"term:{t['name']}:{i}" for i, c in enumerate(t["components"]) if c.get("clause") == item["clause"]]
    elif item.get("id"):
        ids.append(item["id"])
    else:
        covs = [c["id"] for c in ab["covenants"] if c["ref"] and c["ref"] == item.get("where")]
        regs = [g["id"] for g in ab["regimes"] if set(g.get("applies_to", [])) & set(covs)]
        ids += [k for k in smr if any(k.startswith(f"cov:{c}:") for c in covs) or any(k.split(":")[:2] == ["reg", g] for g in regs)]
    out = [{"id": k, **smr[k], "plain": PL.gpt_diff(smr[k].get("value_in_text")) if smr[k]["verdict"] != "agree" else ""}
           for k in ids if k in smr]
    return {"claims": out, "agree": sum(1 for x in out if x["verdict"] == "agree"), "total": len(out)}


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
    details, terms = term_context(ab)
    items = [{"key": i["key"], "kind": i["kind"], "type": i["type"], "where": i["where"], "what": i["what"],
              "evidence": i["evidence"], "resolution": i["resolution"],
              "why": WHY.get((i["what"] or "").split(". Model's reading")[0], ""), "reading": i.get("note", ""),
              "detail": details.get((i.get("term"), i.get("clause"))), "fields": C.fields(ab, i), "gpt": gpt_for(ab, i)} for i in rv["items"]]
    comp = {(t["name"], c.get("clause")): c for t in ab.get("terms", []) for c in t["components"]}
    for it, raw in zip(items, rv["items"]):
        it["check_detail"], it["what"] = it["what"], PL.item_text(raw, ab)
        it["where_label"] = PL.where_label(raw, ab)
        it["explain"] = PL.explain(raw, ab, it.get("gpt"))
        if raw.get("kind") == "flag":           # the model's note, minus what the explanation line already says
            flat = re.sub(r"\W", "", it["explain"].lower())
            rest = [x for x in PL._sentences(PL.clean(it["what"])) if re.sub(r"\W", "", x.lower())[:60] not in flat]
            it["reading"] = " ".join(rest)
        it["evidences"] = [e | {"loc": f"item:{it['key']}" + (f":{n}" if n else "")} for n, e in enumerate(evidences(ab, raw))]
        # On a second-reading item the stored note is the second reading's, not the abstract's: don't show it as "the model's".
        it["reading"] = "" if raw.get("kind") == "review/gpt" else PL.reading(it.get("reading"))
        for f in it["fields"]:
            if f["label"] == "Category":
                f["labels"] = {o: BR.cat_label(o) for o in f["options"]}
        it["statement"] = BR.statement(ab, raw, it["fields"])
        it |= BR.level(raw, it["gpt"], comp.get((raw.get("term"), raw.get("clause"))))
    locs = locate(b, ab, rv, abs_dir)
    for it in items:                       # two quotes from the same sentence are one passage on screen
        seen, keep = set(), []
        for e in it["evidences"]:
            k = re.sub(r"\W", "", ((locs["loc"].get(e["loc"]) or {}).get("sentence") or e["quote"]).lower())[:200]
            if k not in seen:
                seen.add(k)
                keep.append(e)
        it["evidences"] = keep
    its, open_, deferred, _ = SO.summary(ab, rec)
    trail = [{"date": ab["header"].get("built"), "by": "system",
              "what": f"abstract built from {ab['header'].get('source_file')}; {len(its)} items for review"}]
    word = {"accept": "confirmed", "correct": "changed", "defer": "later", "reject": "doesn't belong"}
    trail += [{"date": r["date"], "by": r["by"], "what": f"{word.get(r['decision'], r['decision'])}: {r['item'][:90]}"
               + (f" — note: {r['note']}" if r.get("note") else "")} for r in rec["resolutions"].values()]
    trail += [{"date": x["date"], "by": x["by"], "what": f"changed {x['group']}: {x['label']} {x['before']} → {x['after']}"}
              for x in rec.get("corrections", [])]
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
        "categories": _categories() if terms else [],
        "sheet": BR.covenant_sheet(ab, BR.other_tests(b, abs_dir)), "accuracy": BR.ACCURACY,
        "covs": covs, "regs": regs, "items": items, "terms": terms, "trail": trail, "locs": locs,
    }


def _fmt_cov(c):
    rows = "; ".join(f"{r['value']} ({r['from']} to {r['to']})" for r in c["rows"]) or "no schedule"
    return f"{c['ref']} {c['metric']} ({c['direction']}, {c['frequency']}): {rows}"


def package(b, viewer, abs_dir=config.ABSTRACT_DIR):
    """Everything about a deal's review in one read, for the model to answer questions while the person reviews:
    facts, status, covenants, regimes, every item (with the model's answer, the evidence sentence, the model's note, the fields a correction can
    change and any decision or change made), flagged definitions, the second-model tally and the recent trail. Same data
    as the screen, minus page images and highlight boxes. Reads only."""
    p = payload(b, viewer, abs_dir)
    ab = SO.load(b, abs_dir)[0]
    smr = ab.get("second_model_review") or {}
    items = [{k: it[k] for k in ("key", "kind", "where", "what", "check_detail", "evidence", "resolution")}
             | {"why": it.get("why") or "", "stored": it.get("detail"), "model_note": it.get("reading") or "",
                "evidence": [{"label": e["label"], "sentence": (p["locs"]["loc"].get(e["loc"]) or {}).get("sentence", e["quote"])}
                             for e in it.get("evidences") or []],
                "level": it.get("level"), "tier": it.get("tier"),
                "abstract_says": "".join(x["t"] if "t" in x else str(it["fields"][x["f"]]["value"] or "—") for x in it.get("statement") or [] if not x.get("edit")),
                "correctable_fields": [{"label": f["label"], "group": f["group"], "value": f["value"]} for f in it.get("fields") or []]}
             for it in p["items"]]
    open_ = [i for i in items if not i["resolution"]]
    lines = [f"{p['name']} ({b}): status {p['status']}, {p['open']} of {p['total']} items open"
             + (f", {p['deferred']} deferred" if p["deferred"] else "") + ".",
             f"Source: {p['header']['source_file']} (built {p['header']['built']}).",
             *[c["sentence"] + (f" Last reported {c['reported']['value']} at {c['reported']['period']} (cushion {c['reported'].get('cushion', '—')})."
                                 if c.get("reported") else "") for c in p["sheet"]["covenants"]],
             f"{len(p['sheet']['deal']['other_tests'])} other ratio tests (incurrence; found by code scan, not reviewed).",
             *(f"Regime: {g['name']} ({g['type']}): increment {g['increment'] or '—'}, ceiling {g['ceiling'] or '—'}, level "
               f"{g['level'] or '—'}, duration {(g.get('duration') or {}).get('count') or '—'} {(g.get('duration') or {}).get('unit') or ''}, "
               f"max uses {g['max_uses'] or 'no limit stated'}" for g in p["regs"]),
             f"Open items: {sum(1 for i in open_ if i['level'] != 'confirm')} high priority, "
             f"{sum(1 for i in open_ if i['level'] == 'confirm')} low priority. Every item needs the reviewer's decision.",
             *(f"- [{i['key']}] ({i['level']}) §{i['where'] or '—'}: {i['what'][:160]}"
                              for i in open_)]
    return {"borrower": b, "name": p["name"], "status": p["status"], "open": p["open"], "total": p["total"],
            "verified_by": p["verified_by"], "signed_by": p["signed_by"], "summary": "\n".join(lines),
            "facts": p["facts"], "header": p["header"], "covenants": p["covs"], "regimes": p["regs"], "items": items,
            "definitions": p.get("terms", {}), "covenant_sheet": p["sheet"],
            "trail": p["trail"][-15:]}


def brief(pkg):
    """The short version delivered with the screen: the summary text and where to get the rest."""
    return pkg["summary"] + "\nFor every item's evidence, GPT's answer and the correctable fields, call view_covenant_package."
