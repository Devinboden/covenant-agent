"""Step 1: PDF -> clean, ordered text blocks.

What this does (all deterministic, no model):
  - Reads every text span with its position from a born-digital PDF.
  - Detects struck-through (deleted) text from the drawn strike lines, since the PDF
    text layer carries no "strikethrough" attribute. Deleted text is kept in `raw`
    but dropped from `live` (the current agreement).
  - Removes page artifacts: bare page numbers and headers/footers that repeat across pages.
  - Rebuilds paragraphs that a page break split in two.
  - Groups side-by-side blocks (table cells) into one row, joined with " | ".

Output: list of Block dicts (id, page, text_live, text_raw, struck_chars, kind).
"""
import re
from collections import Counter

import pymupdf

PAGE_NUM = re.compile(r"^(page\s+)?[-–(]?\s*([0-9]{1,4}|[ivxlc]{1,6}|[A-Z]-?\d{1,3})\s*[-–)]?$", re.I)
NEW_UNIT = re.compile(r"^(SECTION|Section|ARTICLE|Article|EXHIBIT|SCHEDULE|[“\"]|\([a-zA-Z0-9]{1,5}\)\s|\d+\.\d+\s)")
SENT_END = re.compile(r"[.:;?!][\"”)\]]?\s*$")   # "(y)" at a page end is NOT a sentence end


def _strike_segments(page):
    """Thin horizontal rules drawn on the page (strike lines and underlines)."""
    segs = []
    for dr in page.get_drawings():
        for it in dr["items"]:
            if it[0] == "re":
                r = it[1]
                if r.height < 2.5 and r.width > 1.5:
                    segs.append((r.x0, r.x1, (r.y0 + r.y1) / 2))
            elif it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.y - b.y) < 0.6:
                    segs.append((min(a.x, b.x), max(a.x, b.x), (a.y + b.y) / 2))
    return segs


def _is_struck(bbox, segs):
    x0, y0, x1, y1 = bbox
    h, w = y1 - y0, x1 - x0
    if h <= 0 or w <= 0:
        return False
    covered = 0.0
    for sx0, sx1, sy in segs:
        rel = (sy - y0) / h
        if 0.30 <= rel <= 0.72:                       # through the middle = strike; bottom = underline
            covered += max(0.0, min(x1, sx1) - max(x0, sx0))
    return covered >= 0.5 * w


def _join_lines(lines):
    """Join wrapped lines. Browsers don't soft-hyphenate, so a line ending in '-' is a real
    hyphenated word ("then-" + "applicable") and joins without a space."""
    out = ""
    for x in (l.strip() for l in lines):
        if not x:
            continue
        out = x if not out else (out + x if out.endswith("-") else out + " " + x)
    return out


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0)) / max(1e-6, min(a1 - a0, b1 - b0))


def _visual_lines(lines):
    """PDF 'lines' can split one visual line (e.g. a superscript 'th' becomes its own line).
    Merge lines whose vertical extents overlap, then order spans left to right."""
    groups = []
    for ln in sorted(lines, key=lambda l: (l["bbox"][1], l["bbox"][0])):
        y0, y1 = ln["bbox"][1], ln["bbox"][3]
        if groups and _overlap(groups[-1]["y0"], groups[-1]["y1"], y0, y1) >= 0.4:
            g = groups[-1]
            g["spans"] += ln["spans"]
            g["y0"], g["y1"] = min(g["y0"], y0), max(g["y1"], y1)
        else:
            groups.append({"y0": y0, "y1": y1, "spans": list(ln["spans"])})
    return [sorted(g["spans"], key=lambda s: s["bbox"][0]) for g in groups]


def _span_text(spans, drop=()):
    """Concatenate spans left to right, inserting a space only where the ORIGINAL layout has a
    visible gap. Dropped (struck) spans still count for spacing, so removing the struck '2' in
    '4.|2|75' gives '4.75', not '4. 75'."""
    out, prev = "", None
    for i, sp in enumerate(spans):
        t = sp["text"]
        if prev is not None and not t.startswith(" "):
            gap = sp["bbox"][0] - prev["bbox"][2]
            if gap > 0.15 * max(sp["size"], 1) and out and not out.endswith(" ") and i not in drop:
                out += " "
        if i not in drop:
            out += t
        prev = sp
    return out


def _render(lines, segs):
    """lines (PyMuPDF line dicts) -> (live text, raw text, struck char count)."""
    live_lines, raw_lines, struck = [], [], 0
    for spans in _visual_lines(lines):
        drop = set()
        for i, sp in enumerate(spans):
            if sp["text"].strip() and _is_struck(sp["bbox"], segs):
                struck += len(sp["text"].strip())
                drop.add(i)
        raw_lines.append(_span_text(spans))
        live_lines.append(_span_text(spans, drop))
    return (re.sub(r"\s+", " ", _join_lines(live_lines)).strip(),
            re.sub(r"\s+", " ", _join_lines(raw_lines)).strip(), struck)


def _good_tables(page):
    """Bordered tables only (strategy='lines'), and only well-formed ones: rejects grids
    that PyMuPDF invents from underlines inside running text."""
    out = []
    try:
        found = page.find_tables(strategy="lines").tables
    except Exception:
        return out
    for t in found:
        rows = t.rows
        if len(rows) < 2 or t.col_count < 2 or t.col_count > 8:
            continue
        cells = [c for r in rows for c in r.cells]
        filled = sum(1 for c in cells if c is not None)
        if filled < 0.75 * len(cells):
            continue
        out.append(t)
    return out


def _page_blocks(page, pno):
    segs = _strike_segments(page)
    d = page.get_text("dict")
    tblocks = [b for b in d["blocks"] if b.get("type") == 0]
    all_lines = [l for b in tblocks for l in b["lines"]]       # snapshot before superscript folding
    tables = _good_tables(page)
    cell_rects = [pymupdf.Rect(c) for t in tables for r in t.rows for c in r.cells if c is not None]

    def center(bb):
        return pymupdf.Point((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)

    # Tiny blocks (superscripts like the 'th' in 10th) fold into the block that contains them.
    big = [b for b in tblocks if not (b["bbox"][3] - b["bbox"][1] < 10 and len("".join(s["text"] for l in b["lines"] for s in l["spans"]).strip()) <= 4)]
    for b in tblocks:
        if b in big:
            continue
        host = next((h for h in big if pymupdf.Rect(h["bbox"]) + (-2, -4, 2, 4) & pymupdf.Rect(b["bbox"]) and
                     pymupdf.Rect(h["bbox"]).intersects(pymupdf.Rect(b["bbox"]) + (-2, -4, 2, 4))), None)
        if host is not None:
            host["lines"] = list(host["lines"]) + list(b["lines"])
        else:
            big.append(b)

    out = []
    for b in big:
        # Spans inside a table cell are rebuilt from the grid below; keep everything else.
        lines = [{**l, "spans": [sp for sp in l["spans"] if not any(r.contains(center(sp["bbox"])) for r in cell_rects)]}
                 for l in b["lines"]] if cell_rects else b["lines"]
        lines = [l for l in lines if l["spans"]]
        if not lines:
            continue
        live_t, raw_t, struck = _render(lines, segs)
        if not raw_t:
            continue
        ys = [sp["bbox"][1] for l in lines for sp in l["spans"]] + [sp["bbox"][3] for l in lines for sp in l["spans"]]
        bb = [b["bbox"][0], min(ys), b["bbox"][2], max(ys)]
        out.append({"page": pno, "bbox": [round(v, 1) for v in bb], "kind": "para",
                    "text_live": live_t, "text_raw": raw_t, "struck_chars": struck})

    for t in tables:
        for r in t.rows:
            live_cells, raw_cells, struck = [], [], 0
            for c in r.cells:
                if c is None:
                    continue
                cr = pymupdf.Rect(c)
                lines = [{**l, "spans": [sp for sp in l["spans"] if cr.contains(center(sp["bbox"]))]} for l in all_lines]
                lines = [l for l in lines if l["spans"]]
                lt, rt, st = _render(lines, segs)
                live_cells.append(lt)
                raw_cells.append(rt)
                struck += st
            if any(raw_cells):
                out.append({"page": pno, "bbox": [round(v, 1) for v in r.bbox], "kind": "row",
                            "text_live": " | ".join(x for x in live_cells if x),
                            "text_raw": " | ".join(x for x in raw_cells if x), "struck_chars": struck})
    return out


def _group_rows(blocks):
    """Merge blocks that sit on the same visual row (table cells) into one row block."""
    blocks = sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))
    rows, cur = [], []
    for b in blocks:
        if b["kind"] == "row":                      # already a table row from the grid
            if cur:
                rows.append(cur)
            rows.append([b])
            cur = []
            continue
        same_band = cur and _overlap(min(c["bbox"][1] for c in cur), max(c["bbox"][3] for c in cur),
                                     b["bbox"][1], b["bbox"][3]) >= 0.5
        if same_band and b["bbox"][0] > max(c["bbox"][2] for c in cur) - 2:
            cur.append(b)
        else:
            if cur:
                rows.append(cur)
            cur = [b]
    if cur:
        rows.append(cur)
    merged = []
    for r in rows:
        if len(r) == 1:
            merged.append(r[0])
        else:
            r = sorted(r, key=lambda b: b["bbox"][0])
            merged.append({"page": r[0]["page"],
                           "bbox": [r[0]["bbox"][0], min(b["bbox"][1] for b in r), r[-1]["bbox"][2], max(b["bbox"][3] for b in r)],
                           "text_live": " | ".join(b["text_live"] for b in r if b["text_live"]),
                           "text_raw": " | ".join(b["text_raw"] for b in r),
                           "struck_chars": sum(b["struck_chars"] for b in r),
                           "kind": "row"})
    return merged


def extract(pdf_path, max_pages=None):
    doc = pymupdf.open(pdf_path)
    pages = [_group_rows(_page_blocks(p, i + 1)) for i, p in enumerate(doc) if max_pages is None or i < max_pages]
    n_pages = len(pages)

    # Headers/footers: short text that shows up as the first or last block on many pages.
    edge = Counter()
    for blks in pages:
        for b in (blks[:1] + blks[-1:]):
            key = re.sub(r"\d+", "#", b["text_raw"].lower())
            if len(key) < 80:
                edge[key] += 1
    repeated = {k for k, c in edge.items() if n_pages >= 6 and c >= max(3, 0.3 * n_pages)}

    blocks, removed = [], Counter()
    for blks in pages:
        for j, b in enumerate(blks):
            at_edge = j < 2 or j >= len(blks) - 2
            key = re.sub(r"\d+", "#", b["text_raw"].lower())
            if at_edge and PAGE_NUM.match(b["text_raw"].strip()):
                removed["page_number"] += 1
                continue
            if at_edge and key in repeated:
                removed["repeated_header_footer"] += 1
                continue
            if not b["text_live"]:
                removed["fully_struck_block"] += 1
                b = {**b, "kind": "struck"}
            blocks.append(b)

    # Re-join paragraphs split by a page break.
    joined = []
    for b in blocks:
        prev = joined[-1] if joined else None
        if (prev and b["page"] > prev["page"] and prev["kind"] == "para" and b["kind"] == "para"
                and prev["text_live"] and not SENT_END.search(prev["text_live"])
                and not NEW_UNIT.match(b["text_live"])):
            prev["text_live"] += " " + b["text_live"]
            prev["text_raw"] += " " + b["text_raw"]
            prev["struck_chars"] += b["struck_chars"]
            prev["continued_on_page"] = b["page"]
            removed["page_break_joins"] += 1
            continue
        joined.append(dict(b))

    for i, b in enumerate(joined):
        b["id"] = f"b{i:05d}"
    stats = {"pages": n_pages, "blocks": len(joined),
             "struck_chars": sum(b["struck_chars"] for b in joined), **removed}
    return joined, stats
