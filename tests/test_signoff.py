"""Sign-off rules: nothing is verified until every item is decided; four eyes; any change drops back to draft."""
import pytest

from covenant_engine.abstract import signoff as S
from covenant_engine.store import store


def test_full_lifecycle(abs_dir):
    deal = "TRS"
    ab, rec, _ = S.load(deal, abs_dir)
    its = S.items(ab)
    assert its, "the stored TRS abstract has review items"
    with pytest.raises(S.SignoffError, match="unresolved"):
        S.verify(deal, "alice", abs_dir)
    for k in its:
        S.resolve(deal, k, "accept", "alice", "checked", abs_dir=abs_dir)
    S.verify(deal, "alice", abs_dir)
    with pytest.raises(S.SignoffError, match="different person"):
        S.sign(deal, "alice", abs_dir)
    S.sign(deal, "bob", abs_dir)
    assert S.load(deal, abs_dir)[1]["status"] == "signed_off"
    # any change to the content after sign-off drops it back to draft
    st = store(abs_dir)
    ab = st.load(deal, "abstract")
    ab["covenants"][0]["schedule"][0]["value"] = "9.99"
    st.save(deal, "abstract", ab)
    assert S.load(deal, abs_dir)[1]["status"] == "draft"


def test_correct_needs_a_value_and_defer_blocks_verification(abs_dir):
    deal = "TRS"
    its = S.items(S.load(deal, abs_dir)[0])
    k = next(iter(its))
    with pytest.raises(S.SignoffError, match="corrected value"):
        S.resolve(deal, k, "correct", "alice", abs_dir=abs_dir)
    for key in its:
        S.resolve(deal, key, "accept", "alice", abs_dir=abs_dir)
    S.resolve(deal, k, "defer", "alice", abs_dir=abs_dir)
    with pytest.raises(S.SignoffError, match="deferred"):
        S.verify(deal, "alice", abs_dir)


def test_item_keys_are_stable_across_rebuilds():
    flag = {"type": "conflict", "ref": "6.13", "quote": "the ratio shall not exceed", "note": "x"}
    assert S.item_key("flag", flag) == S.item_key("flag", dict(flag, note="reworded note"))
