"""Verify a covenant abstract against its source filings, then run the replay test.

Checks:
  1. Every `quote` / `*_quote` string (and every string in a `quotes:` map) exists
     verbatim in its source document. Source = nearest `src:` above it, else "ca".
  2. Standard threshold schedules ({from, to} rows) are in date order and don't overlap.
  3. Replay: for each disclosed covenant calc, sum numerator and denominator lines,
     compare to the borrower's reported ratio (±0.01), test pass/fail against the cap,
     foot any `build:` breakdowns, run cap checks and "wrong reading" variants.

All arithmetic lives here, never in the LLM.

Usage:
  python engine/verify_abstract.py data/abstracts/WEST.yaml
  python engine/verify_abstract.py --all            # every abstract in data/abstracts
"""
import html
import re
import sys
from datetime import date
from pathlib import Path

import warnings

import yaml

from .config import ROOT
TOL = 0.01 + 1e-9   # reported ratios are rounded to 2dp


def normalize(s: str) -> str:
    s = s.replace("​", " ").replace("\xa0", " ").replace(" ", " ")
    s = s.replace("’", "'").replace("‘", "'")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"“\s*", "“", s)
    s = re.sub(r"\s*”", "”", s)
    s = re.sub(r"\s+([,.;:)%])", r"\1", s)
    s = re.sub(r"([($])\s+", r"\1", s)
    return s.strip()


_cache = {}
RAW_KEYS = ("blackline", "superseded", "struck")   # quote fields that deliberately cite deleted text


def _is_struck(tag) -> bool:
    if tag.name in ("strike", "s", "del"):
        return True
    style = (tag.get("style") or "").lower().replace(" ", "")
    return "line-through" in style


def load_source(path: Path, live: bool = True) -> str:
    """Text of a filing. live=True drops struck-through (deleted) text, so redline
    'conformed copies' read as the current agreement. live=False keeps everything."""
    key = (path, live)
    if key not in _cache:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        if live:
            from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
            warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(raw, "lxml")
            for t in soup.find_all(_is_struck):
                t.decompose()
            text = soup.get_text(" ")
        else:
            text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        _cache[key] = normalize(text)
    return _cache[key]


def walk_quotes(node, src, trail):
    """Yield (trail, source_id, quote) for every quote-like string."""
    if isinstance(node, dict):
        src = node.get("src", src)
        for k, v in node.items():
            here = trail + [str(k)]
            if isinstance(v, str) and (k == "quote" or k.endswith("_quote")):
                yield here, src, v
            elif k == "quotes" and isinstance(v, dict):
                for qk, qv in v.items():
                    if isinstance(qv, str):
                        yield here + [qk], src, qv
                    else:
                        yield from walk_quotes(qv, src, here + [qk])
            elif isinstance(v, (dict, list)):
                yield from walk_quotes(v, src, here)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            label = item.get("id", item.get("covenant", i)) if isinstance(item, dict) else i
            yield from walk_quotes(item, src, trail + [str(label)])


def check_quotes(ab, files):
    """Normal quotes must be in the LIVE text (deleted language fails).
    Fields named blackline_/superseded_/struck_ are checked against the raw redline."""
    fails, n = [], 0
    for trail, src, q in walk_quotes(ab, "ca", []):
        n += 1
        if src not in files:
            fails.append((".".join(trail), src, "UNKNOWN SOURCE ID"))
            continue
        raw_ok = any(k in trail[-1] for k in RAW_KEYS)
        text = load_source(files[src], live=not raw_ok)
        if normalize(q) not in text:
            fails.append((".".join(trail), src, q[:100]))
    return n, fails


def as_date(d):
    if d is None or isinstance(d, date):
        return d
    return date.fromisoformat(str(d))


def check_schedules(ab):
    problems = []
    for cov in ab.get("covenants", []):
        rows = [r for r in cov.get("thresholds", []) or []
                if isinstance(r, dict) and "from" in r and r.get("regime", "standard") == "standard"]
        rows.sort(key=lambda r: as_date(r["from"]))
        for a, b in zip(rows, rows[1:]):
            if a.get("to") is None or as_date(a["to"]) >= as_date(b["from"]):
                problems.append(f"{cov.get('id')}: rows overlap: {a.get('from')}..{a.get('to')} and {b.get('from')}..")
    return problems


def r2(x):
    return round(x + 1e-12, 2)


def run_replay(rp):
    """Return (lines of output, ok?) for one replay entry."""
    out, ok = [], True
    cov = rp.get("covenant")
    rep = rp.get("reported") or (rp.get("expected") if rp.get("status") == "reported_only" else None)
    if rep and "cap" in rep and not rp.get("numerator"):
        # Borrower discloses the ratio but not its components: test the reported ratio against the cap.
        exp = rep
        r, cap, d = exp["ratio"], exp["cap"], exp.get("direction", "max")
        passes = r <= cap if d == "max" else r >= cap
        head = r2(((cap - r) / cap if d == "max" else (r - cap) / cap) * 100)
        return [f"  [{cov}] {rp.get('test_date')}  reported only: {r}x vs {d} {cap}x  pass={passes}  headroom {head}%"], True
    if rp.get("status") in ("not_disclosed", "not_possible"):
        return [f"  [{cov}] skipped: {rp.get('status')}"], True

    num_lines = rp.get("numerator", [])
    den_lines = rp.get("denominator", [])
    ids = {l["id"]: l for l in num_lines + den_lines}

    for l in num_lines + den_lines:
        if "build" in l:
            s = sum(l["build"].values())
            if s != l["value"]:
                out.append(f"  [{cov}] BUILD DOES NOT FOOT: {l['id']} {s} != {l['value']}")
                ok = False

    def ratio(drop=(), override=None):
        override = override or {}
        num = sum(override.get(l["id"], l["value"]) for l in num_lines if l["id"] not in drop)
        den = sum(override.get(l["id"], l["value"]) for l in den_lines if l["id"] not in drop)
        return num, den, (num / den if den else float("inf"))

    num, den, r = ratio()
    exp = rp["expected"]
    cap = exp.get("cap")
    direction = exp.get("direction", "max")
    match = abs(r2(r) - exp["ratio"]) <= TOL
    passes = None if cap is None else (r <= cap if direction == "max" else r >= cap)
    head = None if cap is None else r2(((cap - r) / cap if direction == "max" else (r - cap) / cap) * 100)
    ok &= match
    out.append(f"  [{cov}] {rp.get('test_date')}  numerator {num:,}  denominator {den:,}")
    out.append(f"      ratio {r2(r)}x  reported {exp['ratio']}x  match={'YES' if match else 'NO'}"
               + ("" if cap is None else f"  {direction} {cap}x  pass={passes}  headroom {head}%"))

    for c in rp.get("checks", []) or []:
        lhs = sum(ids[i]["value"] for i in c["lines"])
        if c.get("absolute"):
            lhs = abs(lhs)
        base = den if c.get("of") == "denominator" else num
        rhs = c["pct"] * base
        good = lhs <= rhs if c["op"] == "<=" else lhs >= rhs
        ok &= good
        out.append(f"      check {c['id']}: {c['what']}: {lhs:,} vs {r2(rhs):,} ({r2(lhs / base * 100)}%) -> {'OK' if good else 'FAIL'}")

    for v in rp.get("variants", []) or []:
        _, _, vr = ratio(v.get("drop", ()), v.get("override"))
        vcap = v.get("cap", cap)
        txt = f"{r2(vr)}x"
        if vcap is not None:
            vh = r2(((vcap - vr) / vcap if direction == "max" else (vr - vcap) / vcap) * 100)
            vp = vr <= vcap if direction == "max" else vr >= vcap
            txt += f" vs {vcap}x (pass={vp}, headroom {vh}%)"
        out.append(f"      variant {v['id']}: {v['reading']}: {txt}")
    return out, ok


def verify(path):
    ab = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    files = {k: ROOT / s["file"] for k, s in ab["sources"].items()}
    print(f"== {Path(path).name}  ({ab.get('abstract_id')})")

    n, fails = check_quotes(ab, files)
    print(f"Quotes checked: {n}  missing: {len(fails)}")
    for f in fails:
        print("  MISSING", f)

    probs = check_schedules(ab)
    print(f"Schedule problems: {len(probs)}")
    for p in probs:
        print("  ", p)

    rps = ab.get("replay", [])
    if isinstance(rps, dict):
        rps = [rps]
    print("Replay:")
    rok = True
    if not rps:
        print("  none")
    for rp in rps:
        lines, ok = run_replay(rp)
        rok &= ok
        print("\n".join(lines))

    ok = not fails and not probs and rok
    print("RESULT:", "PASS" if ok else "FAIL")
    print()
    return ok


def main(args):
    if args and args[0] == "--all":
        paths = sorted((ROOT / "data" / "abstracts").glob("*.yaml"))
    else:
        paths = [Path(a) for a in args] or [ROOT / "data/abstracts/WEST.yaml"]
    results = [verify(p) for p in paths]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
