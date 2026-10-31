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
