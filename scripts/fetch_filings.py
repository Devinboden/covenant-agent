"""Rebuild the data the repo doesn't carry: download every filing in data/manifest.csv from SEC EDGAR, render it to
PDF the way a bank receives it, and ingest each deal's governing agreement.

  SEC_USER_AGENT="Your Name you@example.com" python scripts/fetch_filings.py [TICKER ...] [--no-render] [--no-ingest]

SEC asks every automated client to identify itself (https://www.sec.gov/os/accessing-edgar-data); set SEC_USER_AGENT.
Requests are spaced to stay well under SEC's 10 requests/second limit.
"""
import argparse
import asyncio
import csv
import os
import sys
import time
import urllib.request
from pathlib import Path

from covenant_engine.config import DATA, ROOT


def download(rows, agent):
    for r in rows:
        dst = DATA / r["local_path"]
        if dst.exists() and dst.stat().st_size > 1000:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(r["url"], headers={"User-Agent": agent, "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            dst.write_bytes(resp.read())
        print(f"downloaded {dst.relative_to(ROOT)}")
        time.sleep(0.3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--no-ingest", action="store_true")
    a = ap.parse_args()
    agent = os.environ.get("SEC_USER_AGENT")
    if not agent:
        sys.exit("set SEC_USER_AGENT to 'Your Name your@email' (SEC requires it)")
    rows = [r for r in csv.DictReader(open(DATA / "manifest.csv")) if not a.tickers or r["ticker"] in a.tickers]
    download(rows, agent)
    if not a.no_render:
        from covenant_engine.tools.html_to_pdf import main as render
        asyncio.run(render(set(a.tickers)))
    if not a.no_ingest:
        from covenant_engine.core.decompose_term import deal_pdf
        from covenant_engine.ingest.run import run as ingest
        tickers = a.tickers or sorted({r["ticker"] for r in rows})
        for t in tickers:
            try:
                pdf = deal_pdf(t)
            except KeyError:
                print(f"{t}: no governing agreement registered (data/deals.json or ingest/run.py CURRENT); skipped")
                continue
            out, rep = ingest(ROOT / pdf, t, use_model_fallback=True)
            print(f"{t}: ingested -> {out.relative_to(ROOT)} | covenants {rep['covenant_sections']['ids']}")


if __name__ == "__main__":
    main()
