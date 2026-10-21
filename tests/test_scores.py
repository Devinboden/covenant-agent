"""Regression guard: the stored abstracts must keep scoring at least the baseline against the answer keys."""
import json
import re
from pathlib import Path

import pytest

from covenant_engine.abstract.score import score_abstract
from covenant_engine.abstract.score_sections import score_all
from covenant_engine.store import store

BASE = json.loads((Path(__file__).parent / "baseline.json").read_text())


@pytest.mark.parametrize("deal", sorted(BASE["schedules"]))
def test_covenant_schedules_and_regimes(deal):
    s = score_abstract(deal, store().load(deal, "abstract"), terms=False)["summary"]
    covs, sched, regimes = (int(x) for x in re.findall(r"(\d+)/\d+", s)[:3])
    want = BASE["schedules"][deal]
    assert (covs, sched, regimes) >= tuple(want) and covs >= want[0] and sched >= want[1] and regimes >= want[2], s


def test_sections():
    got = score_all()
    for name, want in BASE["sections"].items():
        ok, n, misses = got[name]
        assert ok >= want, f"{name}: {ok}/{n} (baseline {want}); misses: {misses}"


def test_every_stored_abstract_is_schema_clean():
    for deal in store().deals():
        ab = store().load(deal, "abstract")
        bad = [r for r in ab["review"] if r.get("step") == "schema" or "model call failed" in str(r)]
        assert not bad, f"{deal}: {bad[:2]}"
