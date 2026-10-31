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


# ---------------- the MCP tools, with a stand-in EDGAR module ----------------

def fake_edgar(fetched):
    m = types.SimpleNamespace(EdgarError=edgar.EdgarError, pdf_path=edgar.pdf_path)
    m.find_agreements = lambda b, years=5: {"ticker": b, "cik": 1, "name": b, "candidates": [{"url": TRS_URL}], "skipped": 0}

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


def test_edgar_tools(abs_dir, tmp_path):
    fetched, ran = [], []

    def runner(b, pdf, progress, **kw):
        ran.append((b, pdf, kw))
        return {"status": "ready_for_review"}
    s = create_server(abs_dir=abs_dir, user="Reviewer 1", job_dir=tmp_path / "jobs", runner=runner, edgar=fake_edgar(fetched))

    done = call(s, "find_credit_agreements", {"borrower": "CARS"}).structured_content
    assert done["already_abstracted"]["status"] == "draft"                       # ours are done: say so
    new = call(s, "find_credit_agreements", {"borrower": "TRS2"}).structured_content
    assert new["already_abstracted"] is None and "start_from_edgar" in new["next"]

    r = call(s, "start_from_edgar", {"borrower": "CARS", "url": TRS_URL})
    assert r.is_error and "already abstracted" in r.content[0].text              # never re-runs a finished deal
    assert call(s, "start_from_edgar", {"borrower": "ZZZ", "url": "https://evil.example/a.htm"}).is_error

    j = call(s, "start_from_edgar", {"borrower": "zzz", "url": TRS_URL}).structured_content
    assert j["pdf"] == "data/pdf/ZZZ/edgar_000084263326000029_trs_03312026xexh101.pdf"
    for _ in range(50):
        st = call(s, "run_status", {"job_id": j["job_id"]}).structured_content
        if st["state"] != "running":
            break
        time.sleep(0.05)
    assert st["state"] == "ready_for_review"
    assert fetched == [("ZZZ", TRS_URL)]                                          # fetched inside the job, then abstracted
    assert ran == [("ZZZ", j["pdf"], {"run_ingest": True, "gpt": False})]
