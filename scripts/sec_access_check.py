"""Why does SEC refuse this machine? Prints, without revealing the secret: whether SEC_USER_AGENT has the shape SEC asks
for ("Name email@domain"), then the HTTP status for the same EDGAR URLs with the secret UA and with a fixed UA.
Same status for both = SEC is refusing the machine's network; secret 403 but fixed 200 = the secret is the problem.
  SEC_USER_AGENT=... python scripts/sec_access_check.py
"""
import os
import re
import time
import urllib.error
import urllib.request

URLS = ["https://www.sec.gov/files/company_tickers.json",
        "https://www.sec.gov/Archives/edgar/data/1683606/000095017024056894/cars-ex10_1.htm",
        "https://efts.sec.gov/LATEST/search-index?q=%22credit%20agreement%22&ciks=0001683606"]
FIXED = "Covenant Agent demo devinboden25@gmail.com"

ua = os.environ.get("SEC_USER_AGENT", "")
shape = bool(re.fullmatch(r"\S+( \S+)* [^\s@]+@[^\s@]+\.[A-Za-z]{2,}", ua.strip()))
print(f"secret UA: length {len(ua)}, shape {'OK' if shape else 'NOT Name email@domain'}, "
      f"surrounding quotes {ua[:1] in '\"' + chr(39)}, leading/trailing space {ua != ua.strip()}, newline {chr(10) in ua}")
for label, agent in [("secret UA", ua), ("fixed UA ", FIXED)]:
    for url in URLS:
        req = urllib.request.Request(url, headers={"User-Agent": agent, "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print(f"{label} {r.status} {url}")
        except urllib.error.HTTPError as e:
            body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", e.read(3000).decode("utf-8", "replace")))[:200]
            print(f"{label} {e.code} {url} | {body.strip()}")
        time.sleep(0.5)
