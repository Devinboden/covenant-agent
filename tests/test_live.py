"""End-to-end rebuild against the keys. Calls models (~$2) and needs fetched data: pytest -m live"""
import pytest

from covenant_engine.abstract import build as B
from covenant_engine.abstract.score import score_abstract


@pytest.mark.live
@pytest.mark.data
def test_rebuild_trs_matches_keys(tmp_path):
    r = B.build("TRS", reuse_step2=False, out=str(tmp_path))
    assert "error" not in r, r.get("error")
    s = score_abstract("TRS", r["abstract"])["summary"]
    assert "covenants 2/2" in s and "regime fields 5/5" in s, s
