"""Light EDGAR intake: find a borrower's credit agreements on SEC EDGAR, and fetch one as a PDF for start_deal.

  find_agreements("TRS")             -> candidate exhibits, newest first, each typed by code from its cover
  fetch_pdf("TRS", url)              -> data/pdf/TRS/edgar_<accession>_<file>.pdf (+ .source.json provenance)

Public data only. SEC asks every automated client to identify itself (https://www.sec.gov/os/accessing-edgar-data):
set SEC_USER_AGENT to "Your Name your@email". Requests are spaced to stay well under SEC's 10 requests/second.
Code decides the document type (same cover patterns as ingest/locate.py); the model only picks from the list with
the person.
"""
import html as htmllib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import date

from ..config import DATA, ROOT
from ..ingest.locate import DOC_PATTERNS

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
# Only primary EDGAR archive documents can be fetched: https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/<file>
ARCHIVE = re.compile(r"^https://www\.sec\.gov/Archives/edgar/data/(\d{1,10})/(\d{18})/([A-Za-z0-9._-]{1,120}\.html?)$")
FULL_TEXT = re.compile(r"amended and restated|conformed|composite|restated in its entirety|as amended hereby|annex a\b|exhibit a\b", re.I)
ANCILLARY = re.compile(r"\b(GUARANT(Y|EE) AGREEMENT|SUBSIDIARY GUARANT|PROMISSORY NOTE|SECURITY AGREEMENT|PLEDGE AGREEMENT|"
                       r"JOINDER|INTERCREDITOR)\b|\bGUARANTY\b|FORM OF [A-Z ]*NOTE", re.I)
SPACING = 0.15                                  # seconds between SEC requests (< 10/s)

_lock = threading.Lock()
_last = [0.0]


class EdgarError(ValueError):
    pass


def _agent():
    a = os.environ.get("SEC_USER_AGENT", "").strip()
    if not a or "@" not in a:
        raise EdgarError("set SEC_USER_AGENT to 'Your Name your@email' on the server (SEC requires it)")
    return a


def _get(url, limit=None):
    """GET from SEC, rate-limited, identified. limit = max bytes to read (cover-page peeks)."""
    with _lock:
        wait = _last[0] + SPACING - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": _agent(), "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read(limit) if limit else r.read()
    except urllib.error.HTTPError as e:
        raise EdgarError(f"SEC returned {e.code} for {url}") from None
    except urllib.error.URLError as e:
        raise EdgarError(f"can't reach SEC ({e.reason})") from None


def company(ticker):
    """(cik, name) for a ticker, from SEC's ticker list (cached for a week under data/edgar/)."""
    f = DATA / "edgar" / "company_tickers.json"
    if not f.exists() or time.time() - f.stat().st_mtime > 7 * 86400:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(_get(TICKERS_URL))
    t = ticker.upper().replace(".", "-")
    for row in json.loads(f.read_text(encoding="utf-8")).values():
        if row["ticker"].upper() == t:
            return int(row["cik_str"]), row["title"]
    raise EdgarError(f"{ticker} is not in SEC's ticker list")


def _text(raw, n=4000):
    """Visible text of the start of an HTML document, whitespace collapsed."""
    s = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", htmllib.unescape(s)).strip()[:n]


def cover_type(head):
    """What the cover calls itself: first DOC_PATTERNS match in order of appearance (same rule as ingest).
    Side documents (guaranty, note, security/pledge agreement, joinder) mention the credit agreement but aren't it:
    if one names itself before any agreement match, it's 'ancillary'."""
    found = sorted((m.start(), name) for name, rx in DOC_PATTERNS for m in [rx.search(head)] if m)
    side = ANCILLARY.search(head)
    if side and (not found or side.start() < found[0][0]):
        return "ancillary"
    return found[0][1] if found else "other"


def _title(head):
    m = re.search(r"[^.]{0,80}\b(AMENDMENT|CREDIT AGREEMENT|LOAN AGREEMENT|FINANCING AGREEMENT)\b[^.]{0,80}", head, re.I)
    return (m.group(0) if m else head[:160]).strip()[:200]


def find_agreements(ticker, years=5, limit=12):
    """Credit agreements and amendments filed as EX-10 exhibits in the last `years`, newest first."""
    cik, name = company(ticker)
    start = date(date.today().year - years, 1, 1).isoformat()
    hits, seen = [], set()
    for page in range(3):                                   # up to 300 full-text hits
        q = urllib.parse.urlencode({"q": '"credit agreement" "administrative agent"', "ciks": f"{cik:010d}",
                                    "forms": "8-K,10-Q,10-K", "dateRange": "custom", "startdt": start,
                                    "enddt": date.today().isoformat(), "from": page * 100})
        res = json.loads(_get(f"{FTS_URL}?{q}"))["hits"]
        for h in res["hits"]:
            s = h["_source"]
            if not str(s.get("file_type", "")).upper().startswith("EX-10"):
                continue
            adsh, fname = h["_id"].split(":", 1)
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{fname}"
            if url in seen or not ARCHIVE.match(url):
                continue
            seen.add(url)
            hits.append({"filed": s.get("file_date", ""), "form": s.get("form", ""), "exhibit": s.get("file_type", ""),
                         "accession": adsh, "url": url})
        if (page + 1) * 100 >= res["total"]["value"]:
            break
    hits.sort(key=lambda h: h["filed"], reverse=True)
    out = []
    for h in hits[:limit]:                                  # peek at each cover (first 64 KB) so code can type it
        head = _text(_get(h["url"], limit=65536))
        out.append(h | {"doc_type": cover_type(head), "title": _title(head),
                        "full_text_likely": bool(FULL_TEXT.search(head))})
    keep = [h for h in out if h["doc_type"] in ("credit_agreement", "amendment", "consent_or_waiver")]
    return {"ticker": ticker.upper(), "cik": cik, "name": name, "since": start, "candidates": keep,
            "skipped": len(out) - len(keep)}


def check_url(ticker, url):
    """The url must be an EDGAR archive document filed by this ticker's company. Returns (cik, accession, file)."""
    m = ARCHIVE.match(url or "")
    if not m:
        raise EdgarError("url must be an EDGAR archive document: https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/<file>.htm")
    cik, _ = company(ticker)
    if int(m.group(1)) != cik:
        raise EdgarError(f"that filing belongs to CIK {int(m.group(1))}, not {ticker.upper()} (CIK {cik})")
    return cik, m.group(2), m.group(3)


def pdf_path(ticker, url):
    """Where fetch_pdf puts the PDF, relative to ROOT (deterministic, so start_deal can be told up front)."""
    m = ARCHIVE.match(url)
    stem = re.sub(r"\.html?$", "", m.group(3))
    return f"data/pdf/{ticker.upper()}/edgar_{m.group(2)}_{stem}.pdf"


def fetch_pdf(ticker, url, progress=print):
    """Download the exhibit and render it to PDF the way a bank receives it (same renderer as the engine's PDFs)."""
    import asyncio
    cik, acc, fname = check_url(ticker, url)
    rel = pdf_path(ticker, url)
    dst = ROOT / rel
    if dst.exists() and dst.stat().st_size > 1000:
        progress(f"edgar: {rel} already fetched")
        return rel
    src = DATA / "filings" / ticker.upper() / f"edgar_{acc}_{fname}"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(_get(url))
    progress(f"edgar: downloaded {fname} ({src.stat().st_size // 1024} KB)")
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise EdgarError("this server can't render PDFs (pip install '.[render]' and playwright's Chromium)") from None
    from .html_to_pdf import render

    async def go():
        async with async_playwright() as p:
            b = await p.chromium.launch()
            await render(b, src, dst, asyncio.Semaphore(1))
            await b.close()
    asyncio.run(go())
    dst.with_suffix(".source.json").write_text(json.dumps(
        {"url": url, "cik": cik, "accession": acc, "fetched": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=1), encoding="utf-8")
    progress(f"edgar: rendered {rel} ({dst.stat().st_size // 1024} KB)")
    return rel
