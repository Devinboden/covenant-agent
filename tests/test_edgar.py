"""EDGAR intake: the search/typing code against canned SEC responses (no network), and the two MCP tools with a
stand-in EDGAR module. A live check: SEC_USER_AGENT=... python -c "from covenant_engine.tools import edgar; print(edgar.find_agreements('TRS'))"."""
import asyncio
import json
import time
import types

import pytest
from mcp import Client

from covenant_engine.mcp_server import create_server
from covenant_engine.tools import edgar

TRS_URL = "https://www.sec.gov/Archives/edgar/data/842633/000084263326000029/trs_03312026xexh101.htm"
COVERS = {
    "trs_03312026xexh101.htm": "<p>Execution Version</p><p>TENTH AMENDMENT</p><p>This TENTH AMENDMENT, dated as of March 20, "
                               "2026, to the CREDIT AGREEMENT ... attached as Annex A, conformed</p>",
    "ex102-guaranty.htm": "<p>SUBSIDIARY GUARANTY</p><p>in favor of the Administrative Agent under the Credit Agreement</p>",
    "ex101-credit.htm": "<p>CREDIT AGREEMENT</p><p>dated as of May 1, 2025 among TRIMAS CORP and the Lenders</p>",
}


def fts(*rows):
    return json.dumps({"hits": {"total": {"value": len(rows)}, "hits": [
        {"_id": f"{adsh}:{f}", "_source": {"file_type": t, "file_date": d, "form": form}} for adsh, f, t, d, form in rows]}}).encode()


@pytest.fixture
def sec(monkeypatch, tmp_path):
    """Canned SEC: ticker list, one full-text search page, exhibit covers. Records every URL asked for."""
    asked = []
    tickers = json.dumps({"0": {"cik_str": 842633, "ticker": "TRS", "title": "TRIMAS CORP"}}).encode()
    page = fts(("0000842633-26-000029", "trs_03312026xexh101.htm", "EX-10.1", "2026-04-30", "10-Q"),
               ("0000842633-25-000010", "ex102-guaranty.htm", "EX-10.2", "2025-05-02", "8-K"),
               ("0000842633-25-000010", "ex101-credit.htm", "EX-10.1", "2025-05-02", "8-K"),
               ("0000842633-26-000029", "trs-20260331.htm", "10-Q", "2026-04-30", "10-Q"))   # the filing itself: not an exhibit

    def get(url, limit=None):
        asked.append(url)
        if url == edgar.TICKERS_URL:
            return tickers
        if url.startswith(edgar.FTS_URL):
            return page
        return COVERS[url.rsplit("/", 1)[1]].encode()
    monkeypatch.setattr(edgar, "_get", get)
    monkeypatch.setattr(edgar, "DATA", tmp_path / "data")
    return asked


def test_find_agreements_types_from_the_cover(sec):
    r = edgar.find_agreements("trs")
    assert (r["cik"], r["name"]) == (842633, "TRIMAS CORP")
    got = [(c["filed"], c["doc_type"], c["full_text_likely"]) for c in r["candidates"]]
    assert got == [("2026-04-30", "amendment", True), ("2025-05-02", "credit_agreement", False)]   # newest first
    assert r["skipped"] == 1                                                     # the guaranty is a side document
    assert r["candidates"][0]["url"] == TRS_URL
    assert not any(u.endswith("trs-20260331.htm") for u in sec)                  # non-exhibits are never fetched


def test_check_url_only_this_borrowers_edgar_filings(sec):
    assert edgar.check_url("TRS", TRS_URL)[0] == 842633
    for bad in ["https://evil.example/a.htm", TRS_URL.replace("https://", "http://"), TRS_URL.replace(".htm", ".exe"),
                "https://www.sec.gov/Archives/edgar/data/842633/../../x.htm",
                "https://www.sec.gov/Archives/edgar/data/1683606/000095017024056894/cars-ex10_1.htm"]:   # CARS, not TRS
        with pytest.raises(edgar.EdgarError):
            edgar.check_url("TRS", bad)
    assert edgar.pdf_path("trs", TRS_URL) == "data/pdf/TRS/edgar_000084263326000029_trs_03312026xexh101.pdf"


def test_user_agent_required(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(edgar.EdgarError, match="SEC_USER_AGENT"):
        edgar._agent()


def test_resolve_and_pick(sec):
    assert edgar.resolve("trs")[:2] == ("TRS", 842633)
    assert edgar.resolve("TriMas Corp")[:2] == ("TRS", 842633)                   # by name
    with pytest.raises(edgar.EdgarError, match="no SEC registrant"):
        edgar.resolve("Nonexistent Widgets")
    c = edgar.find_agreements("TRS")["candidates"]
    chosen, why = edgar.pick(c)
    assert chosen["url"] == TRS_URL and "newest full-text" in why               # the conformed 10th amendment
    assert edgar.pick([x | {"full_text_likely": False} for x in c if x["doc_type"] == "amendment"])[0] is None


# ---------------- the MCP tools, with a stand-in EDGAR module ----------------

CANDS = [{"url": TRS_URL, "doc_type": "amendment", "full_text_likely": True, "title": "TENTH AMENDMENT", "exhibit": "EX-10.1",
          "form": "10-Q", "filed": "2026-04-30"}]


def fake_edgar(fetched, cands=CANDS):
    m = types.SimpleNamespace(EdgarError=edgar.EdgarError, pdf_path=edgar.pdf_path, pick=edgar.pick)
    m.resolve = lambda q: (q.upper(), 1, q.upper() + " INC")
    m.find_agreements = lambda b, years=5: {"ticker": b, "cik": 1, "name": b, "candidates": cands, "skipped": 0}

    def check(b, url):
        if not edgar.ARCHIVE.match(url or ""):
            raise edgar.EdgarError("url must be an EDGAR archive document")
    m.check_url = check
    m.fetch_pdf = lambda b, url, progress: fetched.append((b, url))
    return m


def call(server, tool, args):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args)
    return asyncio.run(go())


def wait(s, jid):
    for _ in range(200):
        st = call(s, "run_status", {"job_id": jid}).structured_content
        if st["state"] != "running":
            return st
        time.sleep(0.05)
    return st


def test_start_abstraction(abs_dir, tmp_path):
    fetched, ran = [], []

    def runner(b, pdf, progress, **kw):
        ran.append((b, pdf, kw))
        return {"status": "ready_for_review"}
    s = create_server(abs_dir=abs_dir, user="Reviewer 1", job_dir=tmp_path / "jobs", runner=runner, edgar=fake_edgar(fetched))

    assert call(s, "find_credit_agreements", {"company": "CARS"}).structured_content["already_abstracted"]["status"] == "draft"
    r = call(s, "start_abstraction", {"company": "cars"}).structured_content    # on file: flow continues, no re-run
    assert r["started"] and r["on_file"] and "already on file" in r["message"]
    st = wait(s, r["job_id"])
    assert st["state"] == "ready_for_review" and st["result"]["on_file"] and "not re-run" in st["progress"][0]
    assert not fetched and not ran                                               # nothing downloaded, engine untouched
    assert call(s, "start_abstraction", {"company": "zzz", "url": "https://evil.example/a.htm"}).is_error

    j = call(s, "start_abstraction", {"company": "zzz"}).structured_content      # code picks the document
    assert j["started"] and j["document"]["url"] == TRS_URL and "newest full-text" in j["why_this_one"]
    assert j["pdf"] == "data/pdf/ZZZ/edgar_000084263326000029_trs_03312026xexh101.pdf"
    assert wait(s, j["job_id"])["state"] == "ready_for_review"
    assert fetched == [("ZZZ", TRS_URL)]                                          # fetched inside the job, then abstracted
    assert ran == [("ZZZ", j["pdf"], {"run_ingest": True, "gpt": False})]


def test_start_abstraction_without_a_clear_pick(abs_dir, tmp_path):
    weak = [CANDS[0] | {"full_text_likely": False}]
    s = create_server(abs_dir=abs_dir, user="Reviewer 1", job_dir=tmp_path / "jobs",
                      runner=lambda b, pdf, progress, **kw: {"status": "ready_for_review"}, edgar=fake_edgar([], weak))
    r = call(s, "start_abstraction", {"company": "yyy"}).structured_content
    assert r["started"] is False and r["candidates"] == weak                     # nothing started; the person chooses
