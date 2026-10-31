"""Fetch and render only the governing agreement behind each stored abstract (for the hosted demo's page images).

  SEC_USER_AGENT="Your Name you@example.com" python scripts/fetch_demo_pdfs.py

For every data/abstract/v1/<B>.abstract.json, finds the manifest row whose file matches the abstract's source_file,
downloads that one filing from SEC EDGAR and renders it to data/pdf/<B>/<source_file> with the same renderer the
engine used (Chromium via Playwright). Skips files already present.
"""
import asyncio
import csv
import os
import sys
from pathlib import Path

from covenant_engine.config import DATA
from covenant_engine.store import store

sys.path.insert(0, str(Path(__file__).parent))
from fetch_filings import download  # noqa: E402


def wanted():
    st = store()
    rows = list(csv.DictReader(open(DATA / "manifest.csv")))
    out = []
    for b in st.deals():
        src = st.load(b, "abstract")["header"]["source_file"]
        stem = Path(src).stem
        row = next((r for r in rows if r["ticker"] == b and Path(r["local_path"]).stem == stem), None)
        if row is None:
            print(f"{b}: {src} not in data/manifest.csv; skipped")
            continue
        out.append((b, row, DATA / "pdf" / b / src))
    return out


async def render_all(items):
    from playwright.async_api import async_playwright
    from covenant_engine.tools.html_to_pdf import render     # same page size and margins as the engine's PDFs
    sem = asyncio.Semaphore(3)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        await asyncio.gather(*(render(browser, DATA / r["local_path"], dst, sem) for _, r, dst in items))
        await browser.close()


def main():
    agent = os.environ.get("SEC_USER_AGENT")
    if not agent:
        sys.exit("set SEC_USER_AGENT to 'Your Name your@email' (SEC requires it)")
    items = [(b, r, d) for b, r, d in wanted() if not d.exists()]
    download([r for _, r, _ in items], agent)
    asyncio.run(render_all(items))


if __name__ == "__main__":
    main()
