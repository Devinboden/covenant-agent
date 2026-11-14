"""Plain-English brief, review levels and the incurrence scan (code only)."""
from covenant_engine import brief as B
from covenant_engine import review_view as RV
from covenant_engine.abstract import signoff as SO
from covenant_engine.tools import incurrence as I


def test_cars_sheet_two_covenants_with_cushion():
    ab = SO.load("CARS")[0]
    s = B.covenant_sheet(ab, B.other_tests("CARS", "data/abstract/v1"))
    assert [c["ref"] for c in s["covenants"]] == ["7.11(a)", "7.11(b)"]
    a = s["covenants"][0]
    assert "must not exceed 3.50x" in a["sentence"]
    assert a["formula"]["denominator"] == ["Consolidated EBITDA"]
    assert "rise by 0.50x (to at most 4.00x) for 12 months" in a["regimes"][0]
    r = a["reported"]
    assert r["cushion"] == "3.42x" and r["passes"]                         # 3.50 - 0.08, computed by code
    assert s["deal"]["cure"] == "No equity cure right."
    assert any(t["ratio"] == "Total Net Leverage Ratio" and t["level"] == "4.25" for t in s["deal"]["other_tests"])


def test_statement_fields_round_trip():
    p = RV.payload("CARS", "x")
    for it in p["items"]:
        for seg in it["statement"]:
            assert ("t" in seg) != ("f" in seg)
            if "f" in seg:
                assert 0 <= seg["f"] < len(it["fields"])
        cats = [f for f in it["fields"] if f["label"] == "Category"]
        assert all(f["value"] in f["options"] for f in cats)                 # a select never shows a different value


def test_levels_never_confirm_a_real_disagreement():
    for b in ("CARS", "WEST", "UCTT"):
        for it in RV.payload(b, "x")["items"]:
            g = it["gpt"]
            if it["level"] == "confirm" and it["kind"] != "review/gpt":
                assert g and g["agree"] == g["total"] > 0
            if g and any(c["verdict"] == "disagree" for c in g["claims"]):
                assert it["level"] == "decide"
            if it["kind"] == "flag" and it["type"] in B.HARD_FLAGS:
                assert it["level"] == "decide"


def test_incurrence_scan_skips_maintenance_and_grid():
    text = ("SECTION 7.11 Financial Covenants.\n(a) The Borrower will not permit the Total Net Leverage Ratio to exceed "
            "4.00 to 1.00 as of the end of each quarter.\nSECTION 7.07 Restricted Payments.\n(g) the Borrower may make "
            "Restricted Payments provided the Total Net Leverage Ratio is less than or equal to 2.50 to 1.00.\n"
            "| Level | Total Net Leverage Ratio is less than 3.00 to 1.00 |\n")
    rows = I.scan(text, [], {"7.11"})
    assert [(r["section"], r["level"], r["test"], r["gates"]) for r in rows] == \
        [("7.07", "2.50", "max", "Paying dividends / restricted payments")]


def test_reviewer_text_has_no_pipeline_jargon():
    """What the reviewer reads describes the covenant, not the pipeline (no 'code can't check', 'ref_term', raw diffs)."""
    import re
    bad = re.compile(r"\bcode\b|library|ref_term|cap_on_total|extraction model|\benum\b|\bstored\b|verbatim| vs |\[\d|"
                     r"\b[a-z]+_[a-z_]+\b|copy the opening|set ref", re.I)
    for b in ("CARS", "WEST", "TRS", "MEC", "ROAD", "CXT", "QUAD", "SRI", "UCTT"):
        for it in RV.payload(b, "x")["items"]:
            for t in [it["what"], it["reading"], it["level_why"]] + [c["plain"] for c in (it["gpt"] or {}).get("claims", [])]:
                assert not (t and bad.search(t)), (b, t)



def test_blind_question_locates_parts_sharing_clause_letters():
    """Clause (a) split into 'ratio (a)' (the debt) and '(a)' (minus cash): the question for '(a)' must say which piece
    it means (its opening words), or GPT answers for the start of clause (a). Opening words are a location, not an answer."""
    from covenant_engine.abstract.gpt_blind import questions
    ab = SO.load("CARS")[0]
    q = next(x for x in questions(ab) if x["id"] == "term:Senior Secured Net Leverage Ratio:0")["question"]
    assert 'it begins: "minus Unrestricted Cash' in q
    assert "Unrestricted Cash" not in q.split("it begins:")[0]          # the term itself isn't given away elsewhere


def test_explanations_read_as_plain_statements():
    """The one-line explanation is written for the reviewer to read: no 'you' / 'I', no mention of a model, a second
    reading or a person having to do something."""
    import re
    bad = re.compile(r"\b(you|your|I|we|second reading|person|automatically|GPT|Claude|model)\b")
    for b in ("CARS", "WEST", "TRS", "MEC", "ROAD", "CXT", "QUAD", "SRI", "UCTT"):
        for it in RV.payload(b, "x")["items"]:
            assert it["explain"] and not bad.search(it["explain"]), (b, it["explain"])


def test_highlight_positions_follow_the_pdf(tmp_path, monkeypatch):
    """Saved highlight positions are tied to the PDF file: a re-rendered agreement must be located again, or the page
    shows boxes from the old rendering over the wrong lines."""
    import hashlib
    from covenant_engine import review_view as RVm
    ab = SO.load("CARS")[0]
    rv = RVm.review_data("CARS")
    pdf = RVm.pdf_path("CARS", ab)
    if not pdf:
        import pytest
        pytest.skip("agreement PDF not on this machine")
    first = RVm.locate("CARS", ab, rv)
    fake = tmp_path / "copy.pdf"
    fake.write_bytes(pdf.read_bytes() + b"\n% re-rendered\n")
    monkeypatch.setattr(RVm, "pdf_path", lambda b, a: fake)
    second = RVm.locate("CARS", ab, rv)
    assert second["hash"] != first["hash"]


def test_defined_term_is_found_where_it_is_defined():
    # Covenants tab: clicking a definition item also marks the defined term, at its definition (not an earlier mention)
    import pymupdf
    from covenant_engine.review_view import _defined_at, _flat
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Total Indebtedness shall be tested quarterly.", fontsize=10)
    p = doc.new_page()
    p.insert_text((72, 72), "“Indebtedness” of any Person means, without duplication,", fontsize=10)
    p.insert_text((72, 86), "(a) all obligations of such Person for borrowed money", fontsize=10)
    flat = [_flat(pg.get_text()) for pg in doc]
    page, rects = _defined_at(doc, flat, "Indebtedness")
    assert page == 1 and len(rects) == 1 and rects[0][1] < 80
    assert _defined_at(doc, flat, "Consolidated EBITDA") is None


def test_evidence_drops_struck_through_text():
    # a redline's deleted words are dropped from the evidence the reviewer sees, as ingest drops them for the model
    import pymupdf
    from covenant_engine.review_view import _live_words
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 72), "shall", fontsize=10)
    p.insert_text((100, 72), "trigger", fontsize=10)              # deleted
    p.insert_text((136, 72), "not be resumed", fontsize=10)
    p.draw_line((99, 69), (133, 69), width=0.6)                    # strike line through the middle of 'trigger'
    assert [w[4] for w in _live_words(p)] == ["shall", "not", "be", "resumed"]


def test_flag_brings_the_passages_its_reading_rests_on_and_names_a_missing_document():
    import json
    from covenant_engine.abstract.related import passages, outside, numbers
    ab = json.load(open("data/abstract/v1/MEC.abstract.json"))
    mixes = next(f for f in ab["flags"] if "mixes standard and relief" in f["note"])
    labels = [lab for lab, _ in passages(ab, mixes)]
    assert "Definition of Covenant Adjustment Period" in labels and "How the Covenant Adjustment Period can end early" in labels
    why = outside(ab, mixes, "MEC")
    assert why and "before the Third Amendment" in why and "1st amendment 2025-06" in why
    cap = next(f for f in ab["flags"] if "4.00 ceiling" in f["note"])
    assert outside(ab, cap, "MEC") is None                          # answered on the page itself
    assert numbers("levels of 5.25 and 5.00 from 2026-03-31, §9.15") == {5.25, 5.0}


def test_gpt_agreement_only_demotes_a_flag_about_figures_it_read():
    from covenant_engine import brief as BR
    gpt = {"total": 2, "agree": 2, "claims": [{"verdict": "agree", "value_in_text": "4.00"}, {"verdict": "agree", "value_in_text": "5.25"}]}
    assert BR.level({"kind": "flag", "type": "other", "what": "unclear whether 4.00 caps the 5.25 level"}, gpt)["level"] == "confirm"
    assert BR.level({"kind": "flag", "type": "other", "what": "no cap on the number of uses"}, gpt)["level"] == "decide"
    assert BR.level({"kind": "flag", "type": "other", "what": "x", "outside": "an earlier version"}, gpt)["level"] == "decide"


def test_a_flag_question_is_one_decision():
    from covenant_engine.abstract.covenants import two_questions
    assert not two_questions("Does the relief period cover the March 31, 2027 test, or do the table's levels apply?")
    assert not two_questions("Is the 0.50x step-up a per-use increment bound by the 4.00x ceiling?")
    assert two_questions("Does the Accu-Fab period count toward the two-use limit, and does the 4.00x cap limit the relief levels?")
    assert two_questions("Must a second holiday be separated by two quarters, and what happens if they are closer together?")
    assert two_questions("Is this right? Is that right?")
