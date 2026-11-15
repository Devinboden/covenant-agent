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
    assert sorted(made.values()) == ["Credit Analyst", "Portfolio Manager"] and all(len(k) >= 32 for k in made)
    assert (tmp_path / "reviewers.json").stat().st_mode & 0o077 == 0
    assert hosted.reviewers() == made                           # stable across restarts
    (tmp_path / "reviewers.json").write_text(json.dumps({S1: "Reviewer 1", S2: "Reviewer 2"}))
    assert hosted.reviewers() == {S1: "Credit Analyst", S2: "Portfolio Manager"}   # older files: same URLs, role names
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


def test_files_route_serves_made_workbooks(abs_dir, monkeypatch, tmp_path):
    """/files/<token>/<name> serves a workbook export_workbook made; anything else is 404."""
    from covenant_engine import exports
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("COVENANT_PUBLIC_URL", "https://demo.example.com")
    x = exports.make("MEC", abs_dir)
    app = hosted.build({S1: "Credit Analyst"}, allowed_hosts=["demo.example.com"])
    with TestClient(app, base_url="https://demo.example.com") as c:
        r = c.get(f"/files/{x['token']}/{x['file_name']}")
        assert r.status_code == 200 and r.content == x["bytes"] and "spreadsheetml" in r.headers["content-type"]
        assert c.get(f"/files/{'0' * 32}/{x['file_name']}").status_code == 404
        assert c.get(f"/files/{x['token']}/..%2Freviewers.json").status_code == 404
