"""Write <B>.incurrence.json next to each abstract: other ratio tests in the agreement, found by a code scan.
A side file, not part of the abstract, so it never changes the abstract's hash or sign-off status.
  python scripts/scan_incurrence.py [BORROWER ...]
"""
import glob
import json
import re
import sys
from pathlib import Path

from covenant_engine import config
from covenant_engine.tools import incurrence as I

d = Path(config.ROOT) / config.ABSTRACT_DIR
for f in sorted(d.glob("*.abstract.json")):
    b = f.name.split(".")[0]
    if sys.argv[1:] and b not in sys.argv[1:]:
        continue
    texts = sorted(glob.glob(str(Path(config.ROOT) / f"data/intake/{b}/*/text_live.txt")))
    if not texts:
        print(f"{b}: no intake text, skipped")
        continue
    ab = json.loads(f.read_text(encoding="utf-8"))
    secs = {re.match(r"[\d.]+", c["ref"]).group(0).rstrip(".") for c in ab["covenants"] if re.match(r"\d", c.get("ref") or "")}
    rows = I.scan(Path(texts[-1]).read_text(encoding="utf-8"), [c.get("quote") for c in ab["covenants"]], secs)
    (d / f"{b}.incurrence.json").write_text(json.dumps({"source": Path(texts[-1]).parent.name, "tests": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{b}: {len(rows)} other ratio tests")
