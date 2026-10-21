"""Ingest repairs for filing quirks found on real deals."""
from covenant_engine.ingest.structure import join_split_quotes, restore_section_numbers, split_run_together_headings


def blk(i, text):
    return {"id": f"b{i:05d}", "page": 1, "kind": "para", "text_live": text, "text_raw": text, "struck_chars": 0}


def test_restore_lost_section_numbers_from_toc():
    # CXT: the body prints "09. Financial Covenants." while the TOC keeps "SECTION 6.09. Financial Covenants 162"
    toc = [blk(i, f"SECTION 6.{i:02d}. Title {i} {100 + i}") for i in range(1, 13)]
    toc.append(blk(20, "SECTION 6.09. Financial Covenants 162"))
    body = [blk(30, "09. Financial Covenants. (a) Maximum Leverage."), blk(31, "09. Unrelated Heading. Text.")]
    out, rep = restore_section_numbers(toc + body)
    assert out[-2]["text_live"].startswith("SECTION 6.09. Financial Covenants.")
    assert out[-1]["text_live"].startswith("09. Unrelated")        # title doesn't match the TOC: left alone
    assert rep == ["6.09 Financial Covenants"]


def test_join_a_defined_term_split_across_blocks():
    # QUAD: '... Section 6.11(a). “Total' | 'Net Leverage Ratio” means ...'
    out, rep = join_split_quotes([blk(1, "“Total Leverage Ratio” has the meaning in Section 6.11(a). “Total"),
                                  blk(2, "Net Leverage Ratio” means the ratio of debt to EBITDA.")])
    assert out[0]["text_live"].endswith("6.11(a).")
    assert out[1]["text_live"].startswith("“Total Net Leverage Ratio” means")
    assert len(rep) == 1


def test_split_a_heading_run_into_a_paragraph():
    # SRI: '... the Agreement. 6.3 Accounting Principles; Changes in GAAP. Except as ...'
    out, rep = split_run_together_headings([blk(1, "6.2 Construction. Words have meanings."),
                                            blk(2, "More text here. 6.3 Accounting Principles; Changes in GAAP. Except as provided.")])
    assert [b["text_live"][:10] for b in out] == ["6.2 Constr", "More text ", "6.3 Accoun"]
