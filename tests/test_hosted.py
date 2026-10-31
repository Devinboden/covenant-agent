"""Hosted demo app: one URL per reviewer, unknown secrets get 404, foreign Host headers are refused."""
import json

import pytest
from starlette.testclient import TestClient

from covenant_engine import config, hosted

S1, S2 = "a" * 40, "b" * 40
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def test_reviewer_config_rules(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("COVENANT_REVIEWERS", raising=False)
    made = hosted.reviewers()                                   # first boot: two random secrets in a 0600 file
    assert sorted(made.values()) == ["Reviewer 1", "Reviewer 2"] and all(len(k) >= 32 for k in made)
    assert (tmp_path / "reviewers.json").stat().st_mode & 0o077 == 0
    assert hosted.reviewers() == made                           # stable across restarts
    for bad in ({"short": "R1"}, {S1: "Same", S2: "Same"}, {}):
        monkeypatch.setenv("COVENANT_REVIEWERS", json.dumps(bad))
        with pytest.raises(SystemExit):
            hosted.reviewers()


def test_routes_and_hosts():
    app = hosted.build({S1: "Reviewer 1", S2: "Reviewer 2"}, allowed_hosts=["demo.example.com"])
    with TestClient(app, base_url="https://demo.example.com") as c:
        assert c.get("/health").json() == {"ok": True, "reviewers": 2}
        assert c.post("/r/" + "c" * 40 + "/mcp", json=INIT, headers=HDR).status_code == 404
        assert c.post(f"/r/{S1}/mcp", json=INIT, headers=HDR).status_code == 200
        assert c.post(f"/r/{S1}/mcp", json=INIT, headers={**HDR, "Origin": "https://evil.example"}).status_code == 403
    again = hosted.build({S1: "Reviewer 1"}, allowed_hosts=["demo.example.com"])   # a session manager runs once
    with TestClient(again, base_url="https://evil.example.com") as c:
        assert c.post(f"/r/{S1}/mcp", json=INIT, headers=HDR).status_code == 421
