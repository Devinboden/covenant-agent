"""Render every EDGAR filing in data/filings to a born-digital PDF in data/pdf.

Banks receive executed agreements and financials as PDFs, not EDGAR HTML, so the
pipeline is built and tested on PDFs. Rendering keeps formatting (struck-through
deleted text, colors, tables, page breaks) the way a printed copy would show it.

Usage: python -m covenant_engine.tools.html_to_pdf [TICKER ...]
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from ..config import ROOT
SRC = ROOT / "data" / "filings"
OUT = ROOT / "data" / "pdf"


async def render(browser, src: Path, dst: Path, sem):
    async with sem:
        dst.parent.mkdir(parents=True, exist_ok=True)
        page = await browser.new_page()
        await page.goto(src.as_uri(), wait_until="load", timeout=120_000)
        await page.pdf(path=str(dst), format="Letter", print_background=True,
                       margin={"top": "0.6in", "bottom": "0.6in", "left": "0.7in", "right": "0.7in"})
        await page.close()
        print(f"{dst.relative_to(ROOT)}  {dst.stat().st_size // 1024} KB")


async def main(tickers):
    srcs = sorted(p for p in SRC.glob("*/*.htm") if not tickers or p.parent.name in tickers)
    sem = asyncio.Semaphore(4)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        await asyncio.gather(*(render(browser, s, OUT / s.parent.name / (s.stem + ".pdf"), sem) for s in srcs))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main(set(sys.argv[1:])))
