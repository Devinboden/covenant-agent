"""Steps 2-3: blocks -> sections, definitions, cross-reference graph.

Deterministic, no model.
  - Sections: "Section 6.09", "SECTION 7.11", "SECTION 9.15Financial", "9.16 Financial ...".
    The table of contents repeats every heading, so for each section number we keep the
    occurrence with the most text under it.
  - Definitions: blocks inside the definitions section that start with a quoted term
    (“Term” means / shall mean / has the meaning ...), plus continuation blocks (sub-clauses).
  - Inline definitions: terms defined in passing elsewhere, e.g. (the “Step-Up Election”).
  - Cross-references: which defined terms and sections each definition / section mentions.
"""
import re

HEAD = re.compile(r"^(?:(SECTION|Section)\s*)?(\d{1,2})\.(\d{1,2})(?:\.(\d{1,2}))?([A-Za-z]?)\b\.?\s*(.*)$", re.S)
ARTICLE = re.compile(r"^(ARTICLE|Article)\s+([IVXL]+|\d{1,2})\b\.?\s*(.*)$", re.S)
ATTACH = re.compile(r"^(EXHIBIT|Exhibit|SCHEDULE|Schedule|ANNEX|Annex)\s+([A-Z0-9][\w.\-()]*)\s*(.{0,80})$")
DEF_START = re.compile(r"^[“\"]\s*([^”\"]{1,120}?)\s*[”\"]\s*(?:(?:or|and)\s+[“\"][^”\"]{1,120}?[”\"]\s*)?(?:\([^)]{0,40}\)\s*)?(?:of any [Pp]erson\s+|of the [A-Z][a-z]+\s+)?"
                       r"(?:shall\s+mean|means|mean|shall\s+have|has\s+the\s+meaning|have\s+the\s+meanings?|shall\s+be|is\s+defined|refers|,|:)")
INLINE_DEF = re.compile(r"[(,;]\s*(?:each|collectively|individually|together)?,?\s*(?:the|a|an|such|each|any)?\s*[“\"]\s*([A-Z][^”\"]{1,80}?)\s*[”\"]")
SECTION_REF = re.compile(r"\bSections?\s+(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?[A-Za-z]?)")
SECTION_REF_TITLED = re.compile(r"\bSection\s+(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\s*\[([^\]]{3,80})\]")
TOC_TAIL = re.compile(r"\s\d{1,3}\s*$|\.{4,}|\s\d{1,3}\s+(?:Page|TABLE OF|SECTION|Section|ARTICLE|\d{1,2}\.\d{1,2}\s)")


def _is_toc(text):
    """Table-of-contents lines: 'Title 96', 'Title 111 TABLE OF CONTENTS', several headings in one block."""
    head = text[:220]
    return (len(text) < 260 and bool(TOC_TAIL.search(head))) or len(re.findall(r"(?:SECTION|Section)\s*\d+\.\d+", head)) >= 2


MID_HEAD = re.compile(r"(?<=[.;:])\s+((?:Section|SECTION)\s+)?(\d{1,2})\.(\d{1,2})\.?\s+([A-Z][A-Za-z]+(?:[ ,;]+(?:[A-Z][A-Za-z]+|"
                      r"of|and|the|to|for|in|on|by|with|or))*)\.\s+(?=[A-Z(“]|\d)")


def split_run_together_headings(blocks):
    """#27: PDF extraction can run a section heading into the previous section's paragraph ('... the Agreement. 6.3
    Accounting Principles; Changes in GAAP. If ...' in SRI). Split the block at each such heading so find_sections sees
    it. Guard: the heading's article must match the article of the last heading seen (the block's own or an earlier
    block's), so a stray 'x.y Title.' elsewhere doesn't start a section. Returns (blocks, report)."""
    out, report, art = [], [], None
    for b in blocks:
        t = b["text_live"] or ""
        h = HEAD.match(t)
        if h and not _is_toc(t) and (h.group(1) or re.match(r"^[A-Z\[]", h.group(6) or "")):
            art = h.group(2)
        cuts = [m.start(2) - len(m.group(1) or "") for m in MID_HEAD.finditer(t) if art and m.group(2) == art]
        if not cuts:
            out.append(b)
            continue
        raw = b["text_raw"] or ""
        pieces, pos = [], 0
        for c in cuts + [len(t)]:
            pieces.append(t[pos:c].strip())
            pos = c
        rpos, raws = 0, []
        for k, piece in enumerate(pieces):         # raw text: cut at the same heading text when it is found
            if k == len(pieces) - 1:
                raws.append(raw[rpos:].strip())
                break
            nxt = raw.find(pieces[k + 1][:40], rpos)
            raws.append(raw[rpos:nxt].strip() if nxt > 0 else (raw[rpos:].strip() if k == 0 else ""))
            rpos = nxt if nxt > 0 else rpos
        for k, (piece, rw) in enumerate(zip(pieces, raws)):
            nb = dict(b, text_live=piece, text_raw=rw or piece, id=b["id"] if k == 0 else f"{b['id']}-{k}",
                      struck_chars=b["struck_chars"] if k == 0 else 0)
            out.append(nb)
        report.append({"block": b["id"], "headings": [t[c:c + 60] for c in cuts]})
    return out, report


def join_split_quotes(blocks):
    """A defined term's opening quote at the end of one block and its closing quote in the next ('... Section 6.11(a).
    “Total' | 'Net Leverage Ratio” means ...' in QUAD) hides the definition start. Move the dangling fragment onto the
    next block. Found by the GPT second-model check (2026-10-13). Returns (blocks, report)."""
    out, report = [dict(b) for b in blocks], []
    for k in range(len(out) - 1):
        t = out[k]["text_live"] or ""
        i = t.rfind("“")
        if i < 0 or "”" in t[i:] or len(t) - i > 80:
            continue
        nxt = out[k + 1]["text_live"] or ""
        j = nxt.find("”")
        if j < 0 or j > 80 or not re.match(r"”\s*(?:\([^)]{0,40}\)\s*)?(means|shall mean|has the meaning|shall have the meaning)", nxt[j:]):
            continue
        frag = t[i:]
        out[k]["text_live"] = t[:i].rstrip()
        out[k + 1]["text_live"] = frag.rstrip() + " " + nxt
        for f in ("text_raw",):
            if out[k].get(f, "").endswith(frag.strip()):
                out[k][f] = out[k][f][:len(out[k][f]) - len(frag.strip())].rstrip()
                out[k + 1][f] = frag.strip() + " " + out[k + 1][f]
        report.append({"block": out[k]["id"], "term_start": (frag.strip() + " " + nxt[:j + 1])[:80]})
    return out, report


TOC_ENTRY = re.compile(r"SECTION\s+(\d{1,2})\.(\d{1,2})\.?\s+([A-Z][^0-9]{2,90}?)\s+\d{1,3}(?=\s|$)")
LOST_HEAD = re.compile(r"^(\d{2})\.\s+([A-Z][^.]{2,90})\.")


def restore_section_numbers(blocks):
    """Some filers' HTML loses Word auto-numbering in the body: headings print as "09. Financial Covenants." while the
    table of contents keeps "SECTION 6.09. Financial Covenants 162" (Crane NXT, 2026-10-13). Restore the number from
    the TOC entry with the same two digits AND the same title; nothing is restored without both. Returns
    (blocks, report)."""
    toc = {}
    for b in blocks[:400]:
        for m in TOC_ENTRY.finditer(b["text_live"] or ""):
            toc.setdefault((m.group(2).zfill(2), m.group(3).strip().lower()[:25]), f"{int(m.group(1))}.{m.group(2).zfill(2)}")
    if len(toc) < 10:
        return blocks, []
    out, report = [], []
    for b in blocks:
        t = b["text_live"] or ""
        m = LOST_HEAD.match(t)
        sid = toc.get((m.group(1), m.group(2).strip().lower()[:25])) if m else None
        if sid:
            new = f"SECTION {sid}. " + t[len(m.group(1)) + 1:].lstrip()
            b = dict(b, text_live=new, text_raw="SECTION " + sid.split(".")[0] + "." + (b["text_raw"] or ""))
            report.append(f"{sid} {m.group(2)[:40]}")
        out.append(b)
    return out, report


def find_sections(blocks):
    cands = []
    for i, b in enumerate(blocks):
        t = b["text_live"]
        m = HEAD.match(t)
        if not m or _is_toc(t):
            continue
        kw, a, s, sub, suf, rest = m.groups()
        if not kw and not re.match(r"^[A-Z\[]", rest or ""):
            continue                                   # "4.50 to 1.00" is a number, not a heading
        sid = f"{int(a)}.{s}" + (f".{sub}" if sub else "") + suf
        title = re.split(r"(?<=[a-z\]])\.\s|\.$", rest.strip(), maxsplit=1)[0][:120] if rest else ""
        cands.append({"id": sid, "start": i, "title": title.strip()})
    cands.sort(key=lambda c: c["start"])
    for j, c in enumerate(cands):
        c["end"] = cands[j + 1]["start"] if j + 1 < len(cands) else len(blocks)
        c["chars"] = sum(len(blocks[k]["text_live"]) for k in range(c["start"], c["end"]))
    best = {}
    for c in cands:
        if c["id"] not in best or c["chars"] > best[c["id"]]["chars"]:
            best[c["id"]] = c
    secs = sorted(best.values(), key=lambda c: c["start"])
    for j, c in enumerate(secs):                        # re-close spans after dropping duplicates
        c["end"] = secs[j + 1]["start"] if j + 1 < len(secs) else len(blocks)
        c["article"] = c["id"].split(".")[0]
        c["pages"] = [blocks[c["start"]]["page"], blocks[c["end"] - 1]["page"]]
        c["blocks"] = [blocks[c["start"]]["id"], blocks[c["end"] - 1]["id"]]
    return secs


def reconcile_numbering(sections, blocks):
    """Printed headings can disagree with the agreement's own cross-references (e.g. an auto-numbered
    list in the filing continued from the amendment's sections, so 'Section 8.2.16' is printed
    '13.2.16'). Find the article offset that makes the most cited section numbers resolve; if it
    clearly beats no offset, renumber the agreement body to its own numbering. Returns a report."""
    text = " ".join(b["text_live"] for b in blocks)
    cited = set(SECTION_REF.findall(text))
    titled = SECTION_REF_TITLED.findall(text)
    body_start = next((s["start"] for s in sections if re.search(r"[Dd]efinition|[Dd]efined [Tt]erms", s["title"] or "")), 0)
    body = [s for s in sections if s["start"] >= body_start]
    ids = {s["id"] for s in body}

    def shift(sid, k):
        a, rest = sid.split(".", 1)
        return f"{int(a) + k}.{rest}"

    def resolved(k):
        return sum(shift(c, k) in ids for c in cited) / max(1, len(cited))

    scores = {k: resolved(k) for k in range(-12, 13)}
    best = max(scores, key=scores.get)
    rep = {"cited_section_numbers": len(cited), "resolve_as_printed": round(scores[0], 2),
           "best_offset": best, "resolve_with_offset": round(scores[best], 2), "renumbered": False}
    if best != 0 and scores[best] >= 0.6 and scores[best] - scores[0] >= 0.3:
        title_of = {s["id"]: (s["title"] or "").lower() for s in body}
        rep["titled_refs_confirming"] = sum(1 for n, t in titled if t.lower()[:15] in title_of.get(shift(n, best), ""))
        rep["titled_refs_total"] = len(titled)
        for s in body:
            s["printed_id"] = s["id"]
            a, rest = s["id"].split(".", 1)
            s["id"] = f"{int(a) - best}.{rest}"
            s["article"] = s["id"].split(".")[0]
        for s in sections:
            if s["start"] < body_start:                     # amendment preamble keeps its own numbers, namespaced
                s["printed_id"] = s["id"]
                s["id"] = "amendment:" + s["id"]
        rep["renumbered"] = True
    return rep


def find_attachments(blocks):
    out = []
    for b in blocks:
        m = ATTACH.match(b["text_live"].strip())
        if m and len(b["text_live"]) < 120:
            out.append({"kind": m.group(1).upper(), "id": m.group(2), "title": m.group(3).strip(), "block": b["id"], "page": b["page"]})
    return out


DEF_ANY = re.compile(DEF_START.pattern.lstrip("^"))


def _def_starts(text):
    """Positions where a definition starts: the block start, or mid-block right after a sentence
    end (PDF extraction can run two definitions into one paragraph)."""
    pos = [0] if DEF_START.match(text) else []
    for m in DEF_ANY.finditer(text):
        i = m.start()
        if i > 0 and re.search(r"[.;:]\s*$", text[max(0, i - 3):i]):
            pos.append(i)
    return sorted(set(pos))


def find_definitions(blocks, sections, split_mid_block=True):
    """Definitions live in the definitions section (1.01 / 1.1). Fall back to the whole doc."""
    dsec = next((s for s in sections if s["id"] in ("1.01", "1.1", "1.1A") and re.search(r"[Dd]efin", s["title"] or "Defined")), None) \
        or next((s for s in sections if re.search(r"^(Certain )?Definitions$|^Defined Terms$", (s["title"] or "").strip())), None)
    lo, hi = (dsec["start"], dsec["end"]) if dsec else (0, len(blocks))
    defs, cur, mid_block = {}, None, 0
    for i in range(lo, hi):
        t = blocks[i]["text_live"]
        starts = (_def_starts(t) if split_mid_block else ([0] if DEF_START.match(t) else [])) if t else []
        if not starts:
            if cur is not None and t and not HEAD.match(t):
                cur["parts"].append(t)
                cur["end"] = i + 1
            continue
        if starts[0] > 0 and cur is not None:            # text before the first start continues the previous definition
            cur["parts"].append(t[:starts[0]].strip())
            cur["end"] = i + 1
        for j, a in enumerate(starts):
            seg = t[a: starts[j + 1] if j + 1 < len(starts) else len(t)].strip()
            m = DEF_START.match(seg)
            term = re.sub(r"\s+", " ", m.group(1)).strip()
            alias = re.match(r"^[“\"]\s*[^”\"]{1,120}?\s*[”\"]\s+(?:or|and)\s+[“\"]\s*([^”\"]{1,120}?)\s*[”\"]", seg)
            mid_block += a > 0
            cur = {"term": term, "start": i, "end": i + 1, "section": dsec["id"] if dsec else None, "parts": [seg]}
            if term not in defs:                          # first definition wins (later ones are usually redline remnants)
                defs[term] = cur
                if alias:                                 # “Pro Forma Basis” or “Pro Forma Effect” means ...
                    a = re.sub(r"\s+", " ", alias.group(1)).strip()
                    if a not in defs:
                        defs[a] = cur
            else:
                cur = None
    for d in {id(v): v for v in defs.values()}.values():   # aliases share one record
        d["text"] = " ".join(p for p in d.pop("parts") if p)
        d["page"] = blocks[d["start"]]["page"]
        d["blocks"] = [blocks[d["start"]]["id"], blocks[d["end"] - 1]["id"]]
        d["kind"] = "defined"
        m = re.search(r"meaning (?:assigned to|set forth|given)[^.]{0,60}?Section\s+(\d{1,2}\.\d{1,2}[A-Za-z]?)", d["text"][:300])
        if m:
            d["defined_in_section"] = m.group(1)          # pointer definition ("has the meaning in Section 6.09(c)")
    find_definitions.mid_block = mid_block
    return defs


def find_inline_definitions(blocks, sections, defs):
    sec_of = {}
    for s in sections:
        for k in range(s["start"], s["end"]):
            sec_of[k] = s["id"]
    inline = {}
    for i, b in enumerate(blocks):
        for m in INLINE_DEF.finditer(b["text_live"]):
            term = re.sub(r"\s+", " ", m.group(1)).strip()
            if term in defs and "defined_in_section" not in defs[term]:
                continue
            if term not in inline:
                inline[term] = {"term": term, "kind": "inline", "section": sec_of.get(i), "block": b["id"], "page": b["page"]}
    return inline


def _term_regex(terms):
    variants = {}
    for t in terms:
        variants[t] = t
        variants.setdefault(t + "s", t)
        if t.endswith("y"):
            variants.setdefault(t[:-1] + "ies", t)
    alts = sorted(variants, key=len, reverse=True)
    rx = re.compile(r"(?<![A-Za-z])(" + "|".join(re.escape(a) for a in alts) + r")(?![A-Za-z])")
    return rx, variants


def build_xref(blocks, sections, defs, inline):
    terms = list(defs) + [t for t in inline if t not in defs]
    rx, variants = _term_regex(terms)
    def refs(text, self_term=None):
        found = []
        for m in rx.finditer(text):
            t = variants[m.group(1)]
            if t != self_term and t not in found:
                found.append(t)
        return found, sorted(set(SECTION_REF.findall(text)))
    for term, d in defs.items():
        d["refs"], d["section_refs"] = refs(d["text"], term)
    for s in sections:
        text = " ".join(blocks[k]["text_live"] for k in range(s["start"], s["end"]))
        s["refs"], s["section_refs"] = refs(text)
    return {"terms": terms}
