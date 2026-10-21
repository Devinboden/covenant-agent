"""Pre-chew pipeline: one PDF in, an "intake bundle" out, ready for the spec agent.

  1. extract    PDF -> clean blocks (struck text dropped, page numbers/headers removed, tables as rows)
  2. structure  sections, definitions (+ inline), cross-references
  3. locate     financial covenant section(s)
  4. classify   document type, conformed/blackline
  5. packet     covenant sections + the definitions they use (+ what those use), with page/block ids
  +  report     what was found, what's missing, what's undecided

Usage:
  python -m covenant_engine.ingest.run data/pdf/WEST/amendment_amendment-no-6-2026-06-conformed.pdf --borrower WEST
  python -m covenant_engine.ingest.run --all-current          # the 5 current agreements
Bundle written to data/intake/<borrower>/<file stem>/.
"""
import argparse
import hashlib
import json
import re
import time
from pathlib import Path

from .extract import extract
from .locate import classify_document, find_covenant_sections, model_fallback
from .packet import build_packet, render_markdown
from .structure import (split_run_together_headings, join_split_quotes, restore_section_numbers, build_xref, find_attachments, find_definitions, find_inline_definitions, find_sections,
                        reconcile_numbering)

from ..config import ROOT  # noqa: E402  (project root holding data/; COVENANT_HOME overrides)
VERSION = "intake-0.1"
CURRENT = {
    "WEST": "data/pdf/WEST/amendment_amendment-no-6-2026-06-conformed.pdf",
    "TRS": "data/pdf/TRS/amendment_10th-amendment-2026-03-conformed.pdf",
    "CARS": "data/pdf/CARS/amendment_5th-amendment-2024-05-conformed.pdf",
    "ROAD": "data/pdf/ROAD/amendment_6th-amendment-2026-06.pdf",
    "MEC": "data/pdf/MEC/amendment_3rd-amendment-relief-2026-02-conformed.pdf",
}
ATTACH_REF = re.compile(r"\b(Schedule|Exhibit|Annex)\s+([A-Z0-9]{1,3}(?:[.\-]\d{1,2})*(?:\([a-z0-9]{1,3}\))?)")
RECITAL = re.compile(r"((?:Incremental Assumption Agreement and )?Amendment No\.\s*\d+|(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth) Amendment|Consent)"
                     r"[^;]{0,80}?dated as of ([A-Z][a-z]+ \d{1,2}, \d{4})")


STRICT_DEF = re.compile(r"[“\"]\s*([A-Z][A-Za-z0-9’'&/\- ]{1,78}?)\s*[”\"]\s*(?:\([^)]{0,40}\)\s*)?(?:of any [Pp]erson\s+|of the [A-Z][a-z]+\s+)?"
                        r"(?:shall\s+mean|means|has\s+the\s+meaning|shall\s+have\s+the\s+meaning|have\s+the\s+meanings?)\b")


def _self_checks(live, sections, defs, inline, cov_ids, chunks):
    from .structure import DEF_ANY
    packet_text = " ".join(c["text"] for c in chunks)
    seen_def_text = {re.sub(r"\s+", " ", m.group(1)).strip() for m in DEF_ANY.finditer(live)}
    uncaptured = sorted(t for t in seen_def_text if t not in defs and t not in inline and len(t) < 80)
    # LOUD: a missed definition whose term the packet actually uses (e.g. CARS "Indebtedness").
    # Strict pattern: capitalized term + a real definition verb, so "including" / "$" don't count.
    strict = {re.sub(r"\s+", " ", m.group(1)).strip() for m in STRICT_DEF.finditer(live)}
    used = [t for t in uncaptured if t in strict
            and re.search(r"(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])", packet_text)]
    cov_refs = sorted({t for s in sections if s["id"] in cov_ids for t in s.get("refs", [])})
    in_packet = {c["id"] for c in chunks}
    unresolved = [t for t in cov_refs if t not in in_packet and t not in ("Borrower", "Lenders", "Administrative Agent")]
    return {"MISSED_DEFINITIONS_USED_IN_PACKET": used, "covenant_terms_not_in_packet": unresolved,
            "definition_patterns_not_captured": uncaptured}


def run(pdf_path, borrower, out_root=ROOT / "data" / "intake", use_model_fallback=False, split_mid_block=True,
        blocks_in=None, out_name=None, extra_report=None):
    """blocks_in: process given blocks instead of extracting the PDF (#28 stacked amendments); out_name: bundle folder."""
    t0 = time.time()
    pdf_path = Path(pdf_path)
    sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

    if blocks_in is not None:
        blocks = blocks_in
        stats = {"pages": max(b["page"] for b in blocks), "struck_chars": sum(b.get("struck_chars", 0) for b in blocks),
                 "from": "stacked blocks"}
    else:
        blocks, stats = extract(pdf_path)                                        # 1
    blocks, split_report = split_run_together_headings(blocks)                   # #27 headings run into a paragraph
    blocks, quote_report = join_split_quotes(blocks)                             # defined term split across blocks
    blocks, restored = restore_section_numbers(blocks)                           # "09. Title." -> "SECTION 6.09." (TOC)
    sections = find_sections(blocks)                                             # 2
    numbering = reconcile_numbering(sections, blocks)
    defs = find_definitions(blocks, sections, split_mid_block=split_mid_block)
    inline = find_inline_definitions(blocks, sections, defs)
    build_xref(blocks, sections, defs, inline)
    attachments = find_attachments(blocks)
    cov_ids, cov_conf, cov_ev = find_covenant_sections(sections, blocks)         # 3
    doc = classify_document(blocks, len(defs), stats["struck_chars"])            # 4

    live = "\n\n".join(b["text_live"] for b in blocks if b["text_live"])
    chunks = build_packet(blocks, sections, defs, inline, cov_ids) if cov_ids else []
    checks_before = _self_checks(live, sections, defs, inline, cov_ids, chunks)

    # ---- exception path: one item at a time to a small model, verified by code
    exceptions, undecided = [], []
    if use_model_fallback:
        from . import exceptions as X
        if doc["confidence"] in ("undecided", "low"):
            e = X.classify_document(blocks, len(defs), stats["struck_chars"])
            exceptions.append(e)
            if e["decided_by"] == "applied":
                doc = e["result"]
        if cov_conf in ("undecided", "medium") and doc.get("contains_full_agreement"):
            e = X.find_covenant_sections(sections, blocks, cov_ids, cov_conf)
            exceptions.append(e)
            if e.get("result"):
                cov_ids = e["result"]
                cov_conf = e.get("confidence", "needs_review") if e["decided_by"] != "needs_review" else f"{cov_conf} (needs_review)"
        repaired = False
        for term in checks_before["MISSED_DEFINITIONS_USED_IN_PACKET"]:
            k = next((i for i, b in enumerate(blocks) if X.squash(f"“{term}”") in X.squash(b["text_live"])), None)
            if k is None:
                continue
            e = X.repair_definition(term, blocks, k)
            exceptions.append(e)
            if e["decided_by"] == "applied" and e.get("result"):
                defs[term] = e["result"]
                repaired = True
        if repaired:
            build_xref(blocks, sections, defs, inline)
        if repaired or any(e["task"] == "find_covenant_sections" for e in exceptions):
            chunks = build_packet(blocks, sections, defs, inline, cov_ids) if cov_ids else []
    else:
        if doc["confidence"] in ("undecided", "low"):
            undecided.append({"task": "classify_document", **model_fallback("classify_document", None, False)})
        if cov_conf == "undecided" and doc.get("contains_full_agreement"):
            undecided.append({"task": "find_covenant_sections", **model_fallback("find_covenant_sections", None, False)})
        if checks_before["MISSED_DEFINITIONS_USED_IN_PACKET"]:
            undecided.append({"task": "repair_definition", "terms": checks_before["MISSED_DEFINITIONS_USED_IN_PACKET"],
                              **model_fallback("repair_definition", None, False)})
    undecided += [{"task": e["task"], "term": e.get("term"), "why": e.get("why")} for e in exceptions if e["decided_by"] == "needs_review"]
    raw = "\n\n".join(b["text_raw"] for b in blocks)
    meta = {"borrower": borrower, "source_file": pdf_path.name, "covenant_sections": cov_ids,
            "covenant_confidence": cov_conf, "doc_tokens": len(live) // 4}

    # Report: what the agent and the analyst should know before extraction starts
    packet_text = " ".join(c["text"] for c in chunks)
    have = {a["id"] for a in attachments}
    referenced = sorted({f"{k} {i}" for k, i in ATTACH_REF.findall(packet_text)})
    missing = [r for r in referenced if r.split(" ", 1)[1] not in have]
    head = " ".join(b["text_live"] for b in blocks[:80])
    recitals = [{"what": w, "dated": d} for w, d in dict.fromkeys(RECITAL.findall(head))]
    checks_after = _self_checks(live, sections, defs, inline, cov_ids, chunks)
    report = {
        "version": VERSION, "borrower": borrower, "source_file": pdf_path.name, "sha256": sha,
        "document": doc, "extraction": stats,
        "structure": {"headings_split": split_report, "quotes_joined": quote_report, "section_numbers_restored": restored, "sections": len(sections), "definitions": len(defs), "inline_definitions": len(inline),
                      "numbering": numbering,
                      "attachments_present": sorted(have)},
        "covenant_sections": {"ids": cov_ids, "confidence": cov_conf, "evidence": cov_ev},
        "packet": {"chunks": len(chunks), "definitions": sum(c["kind"] == "definition" for c in chunks),
                   "sections": sum(c["kind"] in ("covenant_section", "defining_section", "cited_section") for c in chunks),
                   "tokens": len(packet_text) // 4, "doc_tokens": len(live) // 4},
        "attachments_referenced_in_packet_but_missing": missing,
        "self_checks": checks_after,
        "self_checks_before_exceptions": checks_before if use_model_fallback else None,
        "exceptions": exceptions,
        "model_cost_usd": round(sum((e.get("model_call") or {}).get("cost_usd", 0) for e in exceptions), 4),
        "amendments_recited": recitals,
        "undecided": undecided,
        "seconds": round(time.time() - t0, 1),
        **(extra_report or {}),
    }

    out = Path(out_root) / borrower / (out_name or pdf_path.stem)
    out.mkdir(parents=True, exist_ok=True)
    (out / "blocks.jsonl").write_text("\n".join(json.dumps(b, ensure_ascii=False) for b in blocks), encoding="utf-8")
    (out / "text_live.txt").write_text(live, encoding="utf-8")
    (out / "text_raw.txt").write_text(raw, encoding="utf-8")
    (out / "sections.json").write_text(json.dumps(sections, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "definitions.json").write_text(json.dumps({"defined": defs, "inline": inline}, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "packet.json").write_text(json.dumps(chunks, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "packet.md").write_text(render_markdown(meta, chunks), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return out, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", nargs="?")
    ap.add_argument("--borrower")
    ap.add_argument("--all-current", action="store_true")
    ap.add_argument("--model", action="store_true", help="enable the Haiku exception path")
    ap.add_argument("--inject-midblock-bug", action="store_true", help="test: disable mid-paragraph definition splitting")
    a = ap.parse_args()
    jobs = CURRENT.items() if a.all_current else [(a.borrower, a.pdf)]
    for b, p in jobs:
        out, r = run(ROOT / p if not Path(p).is_absolute() else p, b, use_model_fallback=a.model,
                     split_mid_block=not a.inject_midblock_bug)
        d = r["document"]
        print(f"{b:5} {d['doc_type']}{' +agreement' if d.get('contains_full_agreement') else ''}{' (blackline)' if d.get('is_blackline') else ''}"
              f" | pages {r['extraction']['pages']} | sections {r['structure']['sections']} defs {r['structure']['definitions']}"
              f" | covenants {r['covenant_sections']['ids']} ({r['covenant_sections']['confidence']})"
              f" | packet {r['packet']['chunks']} chunks ~{r['packet']['tokens']:,} tok of ~{r['packet']['doc_tokens']:,}"
              f" | missing {r['attachments_referenced_in_packet_but_missing'][:4]} | {r['seconds']}s")
        nb = r["structure"]["numbering"]
        print(f"      numbering: {nb['cited_section_numbers']} cited section numbers resolve {nb['resolve_as_printed']:.0%} as printed"
              + (f"; RENUMBERED by offset {nb['best_offset']} -> {nb['resolve_with_offset']:.0%} "
                 f"(bracketed titles confirming {nb.get('titled_refs_confirming')}/{nb.get('titled_refs_total')})" if nb["renumbered"] else ""))
        sc = r["self_checks"]
        print(f"      self-check: MISSED DEFINITIONS USED IN PACKET {sc['MISSED_DEFINITIONS_USED_IN_PACKET']};"
              f" covenant terms not in packet {sc['covenant_terms_not_in_packet']};"
              f" other uncaptured patterns {len(sc['definition_patterns_not_captured'])}")
        for e in r["exceptions"]:
            mc = e.get("model_call") or {}
            print(f"      exception {e['task']}{' ' + e['term'] if e.get('term') else ''}: {e['decided_by']}"
                  f"{' -> ' + str(e.get('result') if not isinstance(e.get('result'), dict) else e['result'].get('doc_type', e['result'].get('term'))) if e.get('result') else ''}"
                  f"{' (' + e['why'] + ')' if e.get('why') else ''} [{mc.get('model')}, ${mc.get('cost_usd')}, {mc.get('seconds')}s]")
        if r["undecided"]:
            print(f"      NEEDS REVIEW: {r['undecided']}")


if __name__ == "__main__":
    main()
