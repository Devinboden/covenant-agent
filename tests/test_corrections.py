"""Structured corrections: Correct changes the abstract's own fields (not just a note), only the fields the item points
at, re-validated, logged before -> after, and a verified abstract drops back to draft."""
import pytest

from covenant_engine.abstract import corrections as C
from covenant_engine.abstract import signoff as SO
from covenant_engine.abstract.build import ABSTRACT_SCHEMA
from covenant_engine.abstract.new_deal import review_rows
from covenant_engine.store import store


def rows(abs_dir, b):
    ab, rec, _ = SO.load(b, abs_dir)
    return ab, review_rows(ab, rec)


def test_fields_follow_the_item(abs_dir):
    ab, rs = rows(abs_dir, "CARS")
    step_up = next(r for r in rs if r["kind"] == "flag")
    labels = [f["label"] for f in C.fields(ab, step_up)]
    assert "Max number of uses (blank = no limit)" in labels and any(l.startswith("Level,") for l in labels)
    clause = next(r for r in rs if r.get("clause") == "(j)")
    f = {x["label"]: x for x in C.fields(ab, clause)}
    assert f["Effect"]["value"] == "+" and f["Category"]["value"] == "custom" and "custom" in f["Category"]["options"]
    calc = next(r for r in rs if r["kind"] == "review/calc")
    assert C.fields(ab, calc) == []                                     # nothing structured to correct


def test_correction_is_applied_and_logged(abs_dir):
    ab, rs = rows(abs_dir, "CARS")
    step_up = next(r for r in rs if r["kind"] == "flag")
    max_uses = next(f for f in C.fields(ab, step_up) if f["label"].startswith("Max number"))
    SO.resolve("CARS", step_up["key"], "correct", "Reviewer 1", "clause limits it to two", abs_dir=abs_dir,
               changes=[{"path": max_uses["path"], "value": "2"}])
    ab2, rec, _ = SO.load("CARS", abs_dir)
    assert ab2["regimes"][max_uses["path"][1]]["max_uses"] == "2"      # the abstract itself changed
    res = rec["resolutions"][step_up["key"]]
    assert res["applied"] and res["changes"][0]["before"] == "—" and res["changes"][0]["after"] == "2"
    assert rec["corrections"][0]["by"] == "Reviewer 1"


def test_refused_corrections_change_nothing(abs_dir):
    ab, rs = rows(abs_dir, "CARS")
    clause = next(r for r in rs if r.get("clause") == "(j)")
    sign = next(f for f in C.fields(ab, clause) if f["label"] == "Effect")
    before = store(abs_dir).load("CARS", "abstract")
    for bad in ([{"path": ["covenants", 0, "schedule", 0, "value"], "value": "9.99"}],   # not a field of this item
                [{"path": sign["path"], "value": "x"}],                                  # not an allowed option
                [{"path": sign["path"], "value": sign["value"]}]):                       # nothing changed
        with pytest.raises(SO.SignoffError):
            SO.resolve("CARS", clause["key"], "correct", "Reviewer 1", abs_dir=abs_dir, changes=bad)
    assert store(abs_dir).load("CARS", "abstract") == before
    ab2, _ = C.apply(ab, clause, [{"path": sign["path"], "value": "-"}], ABSTRACT_SCHEMA)
    assert ab2 != ab and ab["terms"] == before["terms"]                 # apply works on a copy


def test_correction_after_verify_drops_to_draft(abs_dir):
    ab, rs = rows(abs_dir, "CARS")
    for r in rs:
        SO.resolve("CARS", r["key"], "accept", "Reviewer 1", abs_dir=abs_dir)
    SO.verify("CARS", "Reviewer 1", abs_dir=abs_dir)
    step_up = next(r for r in rs if r["kind"] == "flag")
    ceiling = next(f for f in C.fields(ab, step_up) if f["label"].startswith("Ceiling"))
    SO.resolve("CARS", step_up["key"], "correct", "Reviewer 2", abs_dir=abs_dir, changes=[{"path": ceiling["path"], "value": "4.25"}])
    assert SO.load("CARS", abs_dir)[1]["status"] == "draft"             # content changed: needs verifying again


def test_adding_a_cap_saves_and_validates():
    """The reviewer types a dollar limit into 'Limit: none recorded' (CARS Unrestricted Cash, $125M): it is applied and
    the abstract still validates (a blank cap 'basis' used to fail the schema, so the save was refused)."""
    import json
    from covenant_engine.abstract import corrections as C
    from covenant_engine.abstract.build import ABSTRACT_SCHEMA
    ab = json.load(open("data/abstract/v1/CARS.abstract.json"))
    item = {"kind": "review/gpt", "id": "term:Unrestricted Cash:0", "where": "term:Unrestricted Cash:0"}
    f = next(x for x in C.fields(ab, item) if x["path"][-1] == "+")
    new, log = C.apply(ab, item, [{"path": f["path"], "value": "125,000,000"}], ABSTRACT_SCHEMA)
    cap = next(t for t in new["terms"] if t["name"] == "Unrestricted Cash")["components"][0]["caps"][0]
    assert cap["value"] == "125000000" and log[0]["after"] == "125,000,000"


def test_value_must_be_in_its_quote():
    # code proves the stored number is in the quote (not just that the quote is in the agreement)
    from covenant_engine.abstract.value_check import value_in_quote
    assert value_in_quote("3.50", "The Borrower will not permit the Ratio to exceed 3.50 to 1.00") is None
    assert value_in_quote("0.20", "shall not exceed 20% of Consolidated EBITDA for such period") is None
    assert value_in_quote("125000000", "in an amount not to exceed $125,000,000 in the aggregate") is None
    assert value_in_quote("2500000", "not to exceed $2.5 million in any fiscal year") is None
    assert value_in_quote("6000000", "December 31, 2025 | 6.0") is None             # table row in millions
    assert value_in_quote("4.25", "The Borrower will not permit the Ratio to exceed 4.50 to 1.00")      # misread
    assert value_in_quote("9.99", "exceed")                                           # quote too short
    assert value_in_quote("lesser of x|y", "anything") is None                        # not a number: not this check


def test_blind_comparison_is_exact_except_stated_percentages():
    from covenant_engine.abstract.gpt_blind import _same_num, _cap_num, _months
    assert not _same_num("1000000", "100000000")        # $1M is not $100M
    assert _same_num("4.50", "4.5")
    assert _cap_num({"type": "pct_of_ebitda"}, 20) == 0.20 and _cap_num({"type": "fixed_amount"}, 20) == 20
    assert _months("12", "months") == _months("4", "fiscal quarters") == _months("1", "year")
    assert _months("12", "months") != _months("12", "quarters")


def test_a_correction_must_be_a_number_where_the_pipeline_requires_one():
    """A level typed as text used to be accepted; corrections now meet the same rule the pipeline holds the model to."""
    import json, pytest
    from covenant_engine.abstract import corrections as C
    from covenant_engine.abstract.build import ABSTRACT_SCHEMA
    ab = json.load(open("data/abstract/v1/CARS.abstract.json"))
    item = {"kind": "flag", "where": ab["covenants"][0]["ref"]}
    lvl = next(x for x in C.fields(ab, item) if x["path"][-1] == "value" and "schedule" in x["path"])
    with pytest.raises(C.CorrectionError, match="number"):
        C.apply(ab, item, [{"path": lvl["path"], "value": "not a number"}], ABSTRACT_SCHEMA)
    new, _ = C.apply(ab, item, [{"path": lvl["path"], "value": "3.75"}], ABSTRACT_SCHEMA)
    assert new["covenants"][0]["schedule"][0]["value"] == "3.75"


def test_gpt_check_goes_through_the_one_model_gate():
    # 'codex' routes to the Codex backend inside call_model's live backend; without a sign-in it fails loudly, not empty
    from covenant_engine import model as M
    live = M.Live()
    seen = []
    live.codex.complete_json = lambda *a, **k: seen.append("codex") or {"ok": True}
    live.claude.complete_json = lambda *a, **k: seen.append("claude") or {"ok": True}
    live.complete_json("", "q", {}, model="codex"); live.complete_json("", "q", {}, model="claude-sonnet-5-5")
    assert seen == ["codex", "claude"]
    import os
    old = os.environ.pop("CODEX_HOME", None)
    try:
        assert M.CodexCLI().complete_json("", "q", {})["ok"] is False
    finally:
        if old:
            os.environ["CODEX_HOME"] = old


def test_first_test_reading_is_a_setting_and_code_works_out_the_dates():
    """'Closing Date through ...': the reviewer picks the reading; code moves every covenant's first test date."""
    import json
    from datetime import date
    from covenant_engine.abstract import corrections as C
    from covenant_engine.abstract.build import ABSTRACT_SCHEMA
    from covenant_engine.abstract.covenants import first_test_date
    assert first_test_date(date(2023, 6, 28)) == date(2023, 6, 30)
    assert first_test_date(date(2023, 6, 28), "first_full_quarter") == date(2023, 9, 30)
    assert first_test_date(date(2023, 7, 1), "first_full_quarter") == date(2023, 9, 30)    # closed on a quarter's first day
    ab = json.load(open("data/abstract/v1/MEC.abstract.json"))
    item = {"kind": "flag", "where": "9.15(a)"}
    assert any(f["path"] == ["settings", "first_test"] for f in C.fields(ab, item))
    new, log = C.apply(ab, item, [{"path": ["settings", "first_test"], "value": "first_full_quarter"}], ABSTRACT_SCHEMA)
    assert new["settings"]["first_test"] == "first_full_quarter"
    assert {c["schedule"][0]["start"]["date"] for c in new["covenants"] if "Closing Date" in c["schedule"][0]["quote"]} == {"2023-09-30"}
    assert len([x for x in log if x["label"].startswith("From date")]) == 2


def test_flag_naming_a_regime_several_sections_or_a_ratio_gets_its_fields():
    import json
    from covenant_engine.abstract import corrections as C
    ab = json.load(open("data/abstract/v1/TRS.abstract.json"))
    paths = lambda where: {tuple(f["path"][:2]) for f in C.fields(ab, {"kind": "flag", "where": where})}
    assert ("regimes", 0) in paths("Covenant Holiday Period")
    assert {("covenants", 0), ("covenants", 1)} <= paths("6.12/6.13")
    assert ("terms", 1) in paths("Total Net Leverage Ratio")                       # the cash netting caps


def test_scorer_checks_what_applies_if_relief_ends_early():
    """MEC: if the relief period ends early, 3.50x leverage applies. An abstract that says 'back to the standard
    schedule' while its standard schedule still carries the 5.25x relief rows gets this wrong; the score must show it."""
    import copy, json
    from covenant_engine.abstract.score import score_abstract
    ab = json.load(open("data/abstract/v1/MEC.abstract.json"))
    assert "early exit 7/7" in score_abstract("MEC", ab, terms=False)["summary"]
    bad = copy.deepcopy(ab)
    g = next(g for g in bad["regimes"] if g["type"] == "relief_period")
    g["early_exit"]["after"], g["early_exit"]["after_levels"] = "revert_to_standard", []
    lev = next(c for c in bad["covenants"] if c["id"] == "total_leverage")
    dt = lambda x: {"kind": "date", "date": x, "count": "", "anchor": ""}
    lev["schedule"] = [lev["schedule"][0], {**lev["schedule"][1], "start": dt("2026-03-31"), "end": dt("2026-06-30"), "value": "5.25"},
                       {**lev["schedule"][1], "start": dt("2027-03-31")}]
    s = score_abstract("MEC", bad, terms=False)["summary"]
    assert "early exit 7/7" not in s, s
