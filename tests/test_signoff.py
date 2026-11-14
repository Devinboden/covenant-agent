"""Sign-off rules: nothing is signed off until every item is decided; any change drops back to draft."""
import pytest

from covenant_engine.abstract import signoff as S
from covenant_engine.store import store


def test_full_lifecycle(abs_dir):
    deal = "TRS"
    ab, rec, _ = S.load(deal, abs_dir)
    its = S.items(ab)
    assert its, "the stored TRS abstract has review items"
    with pytest.raises(S.SignoffError, match="unresolved"):
        S.sign(deal, "alice", abs_dir)
    for k in its:
        S.resolve(deal, k, "accept", "alice", "checked", abs_dir=abs_dir)
    S.sign(deal, "alice", abs_dir)                     # the reviewer signs off their own review: one step
    rec = S.load(deal, abs_dir)[1]
    assert (rec["status"], rec["signed_by"]) == ("signed_off", "alice")
    with pytest.raises(S.SignoffError, match="already signed off"):
        S.sign(deal, "alice", abs_dir)
    # any change to the content after sign-off drops it back to draft
    st = store(abs_dir)
    ab = st.load(deal, "abstract")
    ab["covenants"][0]["schedule"][0]["value"] = "9.99"
    st.save(deal, "abstract", ab)
    assert S.load(deal, abs_dir)[1]["status"] == "draft"


def test_correct_needs_a_value_and_defer_blocks_signoff(abs_dir):
    deal = "TRS"
    its = S.items(S.load(deal, abs_dir)[0])
    k = next(iter(its))
    with pytest.raises(S.SignoffError, match="corrected value"):
        S.resolve(deal, k, "correct", "alice", abs_dir=abs_dir)
    for key in its:
        S.resolve(deal, key, "accept", "alice", abs_dir=abs_dir)
    S.resolve(deal, k, "defer", "alice", abs_dir=abs_dir)
    with pytest.raises(S.SignoffError, match="deferred"):
        S.sign(deal, "alice", abs_dir)


def test_old_verified_record_reads_as_draft(abs_dir):
    """A record left half-way by the old two-step flow isn't signed off."""
    deal = "TRS"
    ab, rec, h = S.load(deal, abs_dir)
    rec.update(status="verified", hash=h, verified_by="alice")
    store(abs_dir).save(deal, "signoff", rec)
    rec = S.load(deal, abs_dir)[1]
    assert rec["status"] == "draft" and "verified_by" not in rec


def test_item_keys_are_stable_across_rebuilds():
    flag = {"type": "conflict", "ref": "6.13", "quote": "the ratio shall not exceed", "note": "x"}
    assert S.item_key("flag", flag) == S.item_key("flag", dict(flag, note="reworded note"))
