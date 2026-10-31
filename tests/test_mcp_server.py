"""The MCP server, through a real MCP client (in memory). The person's answers arrive through the elicitation
callback, as they would from a host's form; identity comes from the server's configured user."""
import asyncio
import re
import threading
import time

import pytest
from mcp import Client
from mcp.types import ElicitResult

from covenant_engine import config
from covenant_engine.mcp_server import UI_URI, create_server, view_uri

TOOLS = {"list_deals", "get_abstract", "list_review_items", "signoff_status", "run_status", "start_deal",
         "resolve_item", "verify", "sign_off", "open_review", "find_credit_agreements", "start_abstraction", "review_page", "review_decide", "review_verify", "review_sign"}
SCREEN_ONLY = {"review_page", "review_decide", "review_verify", "review_sign"}


def person(action="accept", decision="accept", confirm=True, seen=None):
    """A stand-in for the human answering forms in the host."""
    async def cb(ctx, params):
        if seen is not None:
            seen.append(params.message)
        if action != "accept":
            return ElicitResult(action=action)
        props = params.requested_schema["properties"]
        if "confirm" in props:
            return ElicitResult(action="accept", content={"confirm": confirm})
        return ElicitResult(action="accept", content={"decision": decision, "note": "checked", "correction": ""})
    return cb


def call(server, tool, args=None, answer=None):
    async def go():
        async with Client(server, elicitation_callback=answer or person()) as c:
            return await c.call_tool(tool, args or {})
    return asyncio.run(go())


def srv(abs_dir, tmp_path, user="Reviewer 1", runner=None):
    return create_server(abs_dir=abs_dir, user=user, job_dir=tmp_path / "jobs",
                         runner=runner or (lambda b, pdf, progress, **kw: {"status": "ready_for_review"}))


def test_tools_and_who_decides(abs_dir, tmp_path):
    async def go():
        async with Client(srv(abs_dir, tmp_path)) as c:
            return {t.name: t for t in (await c.list_tools()).tools}
    tools = asyncio.run(go())
    assert set(tools) == TOOLS
    for name in ("resolve_item", "verify", "sign_off"):            # the model can't pass a decision or an identity
        props = set(tools[name].input_schema.get("properties", {}))
        assert not props & {"answer", "by", "decision", "confirm"}, (name, props)
    assert tools["get_abstract"].annotations.read_only_hint
    for name, t in tools.items():                                  # decisions in the screen are app-only tools
        vis = ((t.meta or {}).get("ui") or {}).get("visibility", ["model", "app"])
        assert (vis == ["app"]) == (name in SCREEN_ONLY), (name, vis)
        if name in SCREEN_ONLY | {"open_review"}:
            assert re.fullmatch(r"ui://covenant-agent/review-[0-9a-f]{12}\.html", t.meta["ui"]["resourceUri"])
            assert t.meta["openai/outputTemplate"] == t.meta["ui"]["resourceUri"]
        assert not {"by", "user", "reviewer"} & set(t.input_schema.get("properties", {})), name


def test_view_uri_is_a_content_hash():
    assert view_uri("<p>a</p>") == view_uri("<p>a</p>") != view_uri("<p>b</p>")   # new build -> new URI


def test_review_screen(abs_dir, tmp_path):
    async def go():
        async with Client(srv(abs_dir, tmp_path)) as c:
            res = await c.list_resources()
            uris = sorted(str(r.uri) for r in res.resources if str(r.uri).startswith("ui://"))
            assert uris == sorted([UI_URI, "ui://covenant-agent/review.html"])        # hashed + legacy alias
            uri = UI_URI
            html = (await c.read_resource(uri)).contents[0]
            return html
    html = asyncio.run(go())
    assert html.mime_type == "text/html;profile=mcp-app" and "Verify abstract" in html.text
    assert html.meta["openai/ui"]["availableDisplayModes"] == ["fullscreen"]          # ChatGPT opens it full screen
    assert '["inline","fullscreen"]' in html.text   # the view declares both modes in ui/initialize
    s1, s2 = srv(abs_dir, tmp_path, user="Reviewer 1"), srv(abs_dir, tmp_path, user="Reviewer 2")
    p = call(s1, "open_review", {"borrower": "MEC"}).structured_content["screen"]
    assert p["viewer"] == "Reviewer 1" and p["status"] == "draft" and len(p["items"]) == p["open"] > 0
    assert p["covs"] and all("loc" in r for c in p["covs"] for r in c["rows"])
    for it in p["items"]:
        p = call(s1, "review_decide", {"borrower": "MEC", "key": it["key"], "decision": "accept", "note": "ok"}).structured_content
    assert p["open"] == 0 and all(i["resolution"]["by"] == "Reviewer 1" for i in p["items"])
    assert call(s1, "review_decide", {"borrower": "MEC", "key": "nope", "decision": "accept"}).is_error
    assert call(s1, "review_verify", {"borrower": "MEC"}).structured_content["status"] == "verified"
    r = call(s1, "review_sign", {"borrower": "MEC"})
    assert r.is_error and "different person" in r.content[0].text
    done = call(s2, "review_sign", {"borrower": "MEC"}).structured_content
    assert (done["status"], done["signed_by"]) == ("signed_off", "Reviewer 2")
    assert [t["what"] for t in done["trail"]][-2:] == ["draft → verified", "verified → signed_off"]


def test_reads(abs_dir, tmp_path):
    s = srv(abs_dir, tmp_path)
    deals = call(s, "list_deals").structured_content["deals"]
    assert {"MEC", "CXT", "WEST"} <= {d["borrower"] for d in deals}
    summ = call(s, "get_abstract", {"borrower": "MEC"}).structured_content
    assert summ["covenants"] and all(c["schedule"] or c["applies_during"] for c in summ["covenants"])
    assert summ["status"] == "draft"
    items = call(s, "list_review_items", {"borrower": "CXT"}).structured_content
    assert items["open"] == len(items["items"]) > 0 and all(i["key"] for i in items["items"])
    assert call(s, "get_abstract", {"borrower": "NOPE"}).is_error
    assert call(s, "get_abstract", {"borrower": "../etc"}).is_error


def test_declined_form_changes_nothing(abs_dir, tmp_path):
    s = srv(abs_dir, tmp_path)
    key = call(s, "list_review_items", {"borrower": "CXT"}).structured_content["items"][0]["key"]
    for action in ("decline", "cancel"):
        r = call(s, "resolve_item", {"borrower": "CXT", "key": key}, answer=person(action))
        assert r.structured_content["recorded"] is False
    assert call(s, "signoff_status", {"borrower": "CXT"}).structured_content["open"] == 6


def test_four_eyes_through_mcp(abs_dir, tmp_path):
    r1, r2 = srv(abs_dir, tmp_path, user="Reviewer 1"), srv(abs_dir, tmp_path, user="Reviewer 2")
    assert call(r1, "verify", {"borrower": "CXT"}).is_error                         # open items block verify
    seen = []
    for it in call(r1, "list_review_items", {"borrower": "CXT"}).structured_content["items"]:
        r = call(r1, "resolve_item", {"borrower": "CXT", "key": it["key"], "proposed_decision": "reject"},
                 answer=person(decision="accept", seen=seen))
        assert r.structured_content["decision"] == "accept"                          # the person's answer wins
    assert len(seen) == 6 and all("CXT" in m for m in seen)
    assert call(r1, "verify", {"borrower": "CXT"}, answer=person(confirm=False)).structured_content["verified"] is False
    assert call(r1, "verify", {"borrower": "CXT"}).structured_content["verified"]
    same = call(r1, "sign_off", {"borrower": "CXT"})
    assert same.is_error and "different person" in same.content[0].text
    ok = call(r2, "sign_off", {"borrower": "CXT"}).structured_content
    assert ok["signed_off"] and ok["by"] == "Reviewer 2"
    st = call(r2, "signoff_status", {"borrower": "CXT"}).structured_content
    assert (st["status"], st["verified_by"], st["signed_by"]) == ("signed_off", "Reviewer 1", "Reviewer 2")


def test_no_identity_no_decisions(abs_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("COVENANT_USER", raising=False)
    s = srv(abs_dir, tmp_path, user="")
    key = call(s, "list_review_items", {"borrower": "CXT"}).structured_content["items"][0]["key"]
    r = call(s, "resolve_item", {"borrower": "CXT", "key": key})
    assert r.is_error and "identity" in r.content[0].text


def test_start_deal_job(abs_dir, tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "data" / "pdf" / "ZZZ").mkdir(parents=True)
    (root / "data" / "pdf" / "ZZZ" / "credit.pdf").write_bytes(b"%PDF-1.4")
    (root / "secret.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(config, "ROOT", root)
    calls, release = [], threading.Event()

    def runner(b, pdf, progress, **kw):
        calls.append((b, pdf, kw))
        progress("ingest: ok")
        release.wait(10)          # held until the duplicate start has been refused (no timing race under load)
        return {"status": "ready_for_review", "deal": b}
    s = srv(abs_dir, tmp_path, runner=runner)
    for bad in ("../secret.pdf", "data/pdf/ZZZ/../../secret.pdf", "data/pdf/ZZZ/missing.pdf", "data/pdf/ZZZ"):
        assert call(s, "start_deal", {"borrower": "ZZZ", "pdf": bad}).is_error, bad

    async def go():
        async with Client(s) as c:
            j = (await c.call_tool("start_deal", {"borrower": "zzz", "pdf": "data/pdf/ZZZ/credit.pdf"})).structured_content
            again = await c.call_tool("start_deal", {"borrower": "ZZZ", "pdf": "data/pdf/ZZZ/credit.pdf"})
            release.set()
            for _ in range(100):
                st = (await c.call_tool("run_status", {"job_id": j["job_id"]})).structured_content
                if st["state"] != "running":
                    break
                await asyncio.sleep(0.1)
            return j, again, st
    j, again, st = asyncio.run(go())
    assert again.is_error and "already has a running job" in again.content[0].text
    assert st["state"] == "ready_for_review" and st["progress"] == ["ingest: ok"]
    assert calls == [("ZZZ", "data/pdf/ZZZ/credit.pdf", {"run_ingest": True, "gpt": False})]
    assert call(s, "run_status", {"job_id": "nope"}).is_error
