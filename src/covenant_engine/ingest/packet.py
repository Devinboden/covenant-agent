"""Reading packet: what the spec agent reads first.

  1. The financial covenant section(s), verbatim.
  2. Every definition those sections use, then the definitions THOSE use, and so on
     (breadth-first, nearest first). Pointer definitions ("has the meaning in Section 6.09(c)")
     and inline definitions pull in the section where the term is actually defined.
  3. Sections the covenant section cites (e.g. the equity cure in 7.02), one level deep.

Every chunk keeps its section / page / block ids so quotes can be traced back.
The full document stays available to the agent through read tools; the packet is the start.
"""

import re

from .structure import _is_toc

# Only definitions on the calculation path get expanded further (ratios, EBITDA, debt, cash, interest...).
CALC = re.compile(r"\b(ratio|EBITDA|income|expense|Indebtedness|[Dd]ebt|cash|Cash|interest|charges|amortization|"
                  r"depreciation|[Ll]everage|[Cc]overage|[Pp]ro [Ff]orma|[Tt]est [Pp]eriod|[Rr]eference [Pp]eriod|"
                  r"[Ll]iquidity|[Cc]apital [Ee]xpenditures|[Aa]vailability|[Ff]ixed [Cc]harge|[Nn]et [Ww]orth)\b")

# Boilerplate terms nearly every definition mentions. Included once, never expanded further.
NO_EXPAND = {"Agreement", "Borrower", "Borrowers", "Parent Borrower", "Holdings", "Lender", "Lenders",
             "Administrative Agent", "Person", "person", "Subsidiary", "subsidiary", "Loan Documents",
             "Loan Party", "Loan Parties", "Credit Party", "Credit Parties", "GAAP", "Business Day",
             "Governmental Authority", "Law", "Laws", "Applicable Law", "Requirement of Law",
             "Required Lenders", "Default", "Event of Default", "Obligations", "Loans", "Loan"}


def _is_calc(term, text):
    return bool(CALC.search(term)) or len(CALC.findall(text[:1500])) >= 3


LOAN_BASICS = re.compile(r"[Ff]inancial [Ss]tatements|[Rr]eporting|[Ii]nformation|[Cc]ompliance [Cc]ertificate|"
                         r"[Rr]ight to [Cc]ure|[Ee]quity [Cc]ure|[Cc]ure [Rr]ight|[Ff]iscal [Yy]ear|[Ff]iscal [Pp]eriods|"
                         r"[Cc]ommitments$|[Mm]aturity|[Tt]ermination [Dd]ate")

# Global calculation rules: apply to every ratio but are rarely cited by number, so following references never
# reaches them (ROAD 1.5 "Pro Forma Calculations", TRS 1.09 "Pro Forma Effect"). Pulled by title, always.
CALC_RULES = re.compile(r"[Pp]ro [Ff]orma|[Rr]atio [Cc]alculations|[Cc]ertain [Cc]alculations|[Cc]alculations and [Tt]ests|"
                        r"[Aa]ccounting [Tt]erms|[Aa]ccounting [Pp]rinciples|[Cc]alculation of [Ff]inancial [Cc]ovenants")


def loan_basics(blocks, sections, cov_ids, preamble_blocks=60):
    """For the spec's Loan section: cover page + recitals, and the reporting / cure /
    fiscal-year / accounting / commitments sections, found by title."""
    out = []
    first = sections[0]["start"] if sections else len(blocks)
    pre = [b for b in blocks[:min(first, preamble_blocks)] if b["text_live"]]
    if pre:
        out.append({"kind": "preamble", "id": "cover", "depth": 0, "pages": [pre[0]["page"], pre[-1]["page"]],
                    "blocks": [pre[0]["id"], pre[-1]["id"]], "text": "\n".join(b["text_live"] for b in pre)})
    for s in sections:
        title = s["title"] or ""
        if s["id"] in cov_ids:
            continue
        kind = "calc_rules" if CALC_RULES.search(title) else "loan_section" if LOAN_BASICS.search(title) else None
        if not kind:
            continue
        out.append({"kind": kind, "id": s["id"], "title": s["title"], "depth": 0, "pages": s["pages"],
                    "blocks": s["blocks"], "text": "\n".join(blocks[k]["text_live"] for k in range(s["start"], s["end"]) if blocks[k]["text_live"])})
    return out


def build_packet(blocks, sections, defs, inline, cov_ids, max_depth=3):
    by_id = {s["id"]: s for s in sections}
    sec_text = lambda s: "\n".join(blocks[k]["text_live"] for k in range(s["start"], s["end"]) if blocks[k]["text_live"])

    chunks, seen_terms, seen_secs = [], set(), set()
    frontier = []
    for sid in cov_ids:
        s = by_id[sid]
        seen_secs.add(sid)
        chunks.append({"kind": "covenant_section", "id": sid, "title": s["title"], "depth": 0,
                       "pages": s["pages"], "blocks": s["blocks"], "text": sec_text(s)})
        frontier += s.get("refs", [])

    depth = 1
    while frontier and depth <= max_depth:
        nxt = []
        for t in frontier:
            if t in seen_terms:
                continue
            seen_terms.add(t)
            if t in defs:
                d = defs[t]
                chunks.append({"kind": "definition", "id": t, "depth": depth, "pages": [d["page"]],
                               "blocks": d["blocks"], "text": d["text"]})
                target = d.get("defined_in_section")
                if target and target in by_id and target not in seen_secs:
                    seen_secs.add(target)
                    s = by_id[target]
                    chunks.append({"kind": "defining_section", "id": target, "title": s["title"], "for_term": t,
                                   "depth": depth, "pages": s["pages"], "blocks": s["blocks"], "text": sec_text(s)})
                if t not in NO_EXPAND and (depth == 1 or _is_calc(t, d["text"])):
                    nxt += d.get("refs", [])
            elif t in inline:
                # Defined in passing, e.g. (the “Step-Up Election”): include just that paragraph.
                inl = inline[t]
                k = next((i for i, b in enumerate(blocks) if b["id"] == inl["block"]), None)
                if k is not None:
                    chunks.append({"kind": "inline_definition", "id": t, "section": inl["section"], "depth": depth,
                                   "pages": [inl["page"]], "blocks": [inl["block"], inl["block"]], "text": blocks[k]["text_live"]})
        frontier = nxt
        depth += 1

    # Sections the covenant cites: listed with their titles (the agent can fetch them);
    # included in full only when they bear on the test (equity cure, financial statements, compliance cert).
    for sid in cov_ids:
        for ref in by_id[sid].get("section_refs", []):
            if ref in by_id and ref not in seen_secs:
                seen_secs.add(ref)
                s = by_id[ref]
                full = bool(re.search(r"[Cc]ure|[Ff]inancial [Ss]tatements|[Cc]ompliance [Cc]ertificate|[Rr]eports", s["title"] or ""))
                chunks.append({"kind": "cited_section", "id": ref, "title": s["title"], "depth": 1,
                               "pages": s["pages"], "blocks": s["blocks"], "text": sec_text(s) if full else "",
                               "included": full})
    seen_ids = {c["id"] for c in chunks}
    chunks += [c for c in loan_basics(blocks, sections, cov_ids) if c["id"] not in seen_ids]

    # Reverse lookup: paragraphs elsewhere that USE a covenant ratio or cite the covenant section.
    # This is where incurrence tests, pricing grids and equity cures live.
    ratio_terms = [t for sid in cov_ids for t in by_id[sid].get("refs", []) if re.search(r"Ratio$", t)]
    pats = [re.compile(r"(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])") for t in dict.fromkeys(ratio_terms)]
    pats += [re.compile(r"Sections?\s+" + re.escape(sid) + r"(?![\d])") for sid in cov_ids]
    covered = set()
    for c in chunks:
        if c["blocks"][0]:
            lo = next((i for i, b in enumerate(blocks) if b["id"] == c["blocks"][0]), None)
            hi = next((i for i, b in enumerate(blocks) if b["id"] == c["blocks"][-1]), None)
            if lo is not None and hi is not None:
                covered.update(range(lo, hi + 1))
    sec_of = {k: s["id"] for s in sections for k in range(s["start"], s["end"])}
    snippet_terms = []
    for k, b in enumerate(blocks):
        if k in covered or not b["text_live"] or not any(p.search(b["text_live"]) for p in pats):
            continue
        if _is_toc(b["text_live"]):                         # table-of-contents lines mention the section but aren't usage
            continue
        chunks.append({"kind": "usage", "id": b["id"], "section": sec_of.get(k), "depth": 1, "pages": [b["page"]],
                       "blocks": [b["id"], b["id"]], "text": b["text_live"]})
        snippet_terms += [t for t in defs if t in b["text_live"] and t not in seen_terms
                          and re.search(r"Cure|Equity Contribution|Ratio$|Applicable (Rate|Margin)", t)]
    for t in dict.fromkeys(snippet_terms):                # definitions those paragraphs depend on (cure, pricing)
        seen_terms.add(t)
        d = defs[t]
        chunks.append({"kind": "definition", "id": t, "depth": 2, "pages": [d["page"]], "blocks": d["blocks"], "text": d["text"]})
    # cure / pricing definitions referenced from loan-basics sections
    for c in [c for c in chunks if c["kind"] == "loan_section"]:
        for t in by_id[c["id"]].get("refs", []):
            if t not in seen_terms and t in defs and re.search(r"Cure|Equity Contribution", t):
                seen_terms.add(t)
                d = defs[t]
                chunks.append({"kind": "definition", "id": t, "depth": 2, "pages": [d["page"]], "blocks": d["blocks"], "text": d["text"]})
    # Terms reached but not expanded: names only, so the agent knows they exist.
    chunks.append({"kind": "index", "id": "more_terms", "depth": max_depth + 1, "pages": [0], "blocks": ["", ""],
                   "text": ", ".join(sorted(t for t in frontier if t not in seen_terms))})
    return chunks


def render_markdown(meta, chunks):
    lines = [f"# Reading packet: {meta['borrower']} · {meta['source_file']}",
             "",
             f"Covenant section(s): {', '.join(meta['covenant_sections'])} ({meta['covenant_confidence']}). "
             f"{len(chunks)} chunks, ~{sum(len(c['text']) for c in chunks) // 4:,} tokens "
             f"(full document ~{meta['doc_tokens']:,}). Deleted (struck) text and page numbers removed.",
             ""]
    order = {"covenant_section": 0, "definition": 1, "inline_definition": 1, "defining_section": 1, "cited_section": 2,
             "usage": 2, "calc_rules": 1, "preamble": 3, "loan_section": 3, "index": 4}
    for c in sorted(chunks, key=lambda c: (order[c["kind"]], c["depth"])):
        head = {"covenant_section": f"## Covenant section {c['id']} {c.get('title', '')}",
                "definition": f"### Definition: “{c['id']}”",
                "inline_definition": f"### Defined in passing: “{c['id']}” (Section {c.get('section')})",
                "defining_section": f"### Section {c['id']} {c.get('title', '')} (defines “{c.get('for_term')}”)",
                "cited_section": f"### Cited section {c['id']} {c.get('title', '')}" + ("" if c.get("included", True) else " (not included; fetch with get_section)"),
                "usage": f"### Uses a covenant ratio (Section {c.get('section')})",
                "preamble": "## Loan basics: cover page and recitals",
                "calc_rules": f"## Calculation rules (apply to every ratio): Section {c['id']} {c.get('title', '')}",
                "loan_section": f"## Loan basics: Section {c['id']} {c.get('title', '')}",
                "index": "### Further terms referenced (fetch with get_definition)"}[c["kind"]]
        lines += [head, f"<sub>pages {c['pages'][0]}–{c['pages'][-1]} · blocks {c['blocks'][0]}–{c['blocks'][-1]} · depth {c['depth']}</sub>", "", c["text"], ""]
    return "\n".join(lines)
