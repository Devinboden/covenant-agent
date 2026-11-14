"""Step 2 (spec v1): maintenance covenants + the regimes that change them. Method 1: Sonnet fills a fixed structure
(enums for every structural choice); code checks it and re-asks with the problems (max 2 repairs), then review.

Structure choices that make the step-2 spike's errors impossible to express:
- a relief schedule lives INSIDE its relief regime (no 'relief' label on covenant rows to get wrong)
- a step-up lives only in its regime (effect, ceiling, duration, uses, gap), never as a covenant row
- durations and gaps carry a unit ("12 months" stays 12 months; code converts)
- dates can be relative ("first six full quarters after the Closing Date")
- covenants link to regimes by id; regimes can exclude each other (not_during)
- early exit has its own block; flags have types
- STATE (relief ended, step-ups used) is not here: it comes from filings as Events
"""
import json
import re
from datetime import date, datetime, timedelta

from ..ingest.exceptions import call_model, squash
from ..core.decompose_term import bundle_dir

DATEPOINT = {"type": "object", "additionalProperties": False, "required": ["kind", "date", "count", "anchor"],
             "properties": {"kind": {"type": "string", "enum": ["date", "quarters_after", "open"]},
                            "date": {"type": "string"}, "count": {"type": "string"}, "anchor": {"type": "string"}}}
ROW = {"type": "object", "additionalProperties": False, "required": ["start", "end", "value", "quote"],
       "properties": {"start": DATEPOINT, "end": DATEPOINT, "value": {"type": "string"}, "quote": {"type": "string"}}}
DURATION = {"type": "object", "additionalProperties": False, "required": ["count", "unit"],
            "properties": {"count": {"type": "string"},
                           "unit": {"type": "string", "enum": ["quarters", "months", "test_dates", "years", "none"]}}}
COVENANT = {"type": "object", "additionalProperties": False,
            "required": ["id", "ref", "name", "metric", "kind", "direction", "unit", "frequency", "event_tests",
                         "skip_when", "schedule", "applies_during", "quote"],
            "properties": {"id": {"type": "string"}, "ref": {"type": "string"}, "name": {"type": "string"},
                           "metric": {"type": "string"},
                           "kind": {"type": "string", "enum": ["ratio", "level"]},
                           "direction": {"type": "string", "enum": ["max", "min"]},
                           "unit": {"type": "string", "enum": ["x", "usd", "pct"]},
                           "frequency": {"type": "string", "enum": ["quarterly", "monthly", "annual", "other"]},
                           "event_tests": {"type": "array", "items": {"type": "string", "enum": [
                               "credit_event", "acquisition", "restricted_payment", "incurrence", "other"]}},
                           "skip_when": {"type": "string"},
                           "schedule": {"type": "array", "items": ROW},
                           "applies_during": {"type": "string"}, "quote": {"type": "string"}}}
REGIME = {"type": "object", "additionalProperties": False,
          "required": ["id", "type", "name", "applies_to", "trigger", "effect", "level", "increment", "ceiling", "schedules",
                       "duration", "max_uses", "min_gap", "start_event", "start_date", "end_event", "end_date",
                       "early_exit", "not_during", "conditions", "quote"],
          "properties": {
              "id": {"type": "string"},
              "type": {"type": "string", "enum": ["relief_period", "step_up", "springing_test", "other"]},
              "name": {"type": "string"},
              "applies_to": {"type": "array", "items": {"type": "string"}},
              "trigger": {"type": "string", "enum": ["borrower_election", "acquisition_automatic", "amendment", "date",
                                                     "usage_threshold", "other"]},
              "effect": {"type": "string", "enum": ["replace_schedule", "set_level", "add_increment", "suspend_test", "other"]},
              "level": {"type": "string"}, "increment": {"type": "string"}, "ceiling": {"type": "string"},
              "schedules": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False, "required": ["covenant_id", "rows"],
                  "properties": {"covenant_id": {"type": "string"}, "rows": {"type": "array", "items": ROW}}}},
              "duration": DURATION, "max_uses": {"type": "string"}, "min_gap": DURATION,
              "start_event": {"type": "string", "enum": ["amendment_effective", "closing", "election_notice",
                                                         "acquisition_close", "date", "other"]},
              "start_date": {"type": "string"},
              "end_event": {"type": "string", "enum": ["fixed_date", "certificate_delivery", "borrower_election",
                                                       "duration_elapsed", "other"]},
              "end_date": {"type": "string"},
              "early_exit": {"type": "object", "additionalProperties": False,
                             "required": ["allowed", "how", "after", "after_levels", "quote"],
                             "properties": {"allowed": {"type": "boolean"},
                                            "how": {"type": "string", "enum": ["borrower_election", "compliance_certificate",
                                                                               "none", "other"]},
                                            "after": {"type": "string", "enum": ["revert_to_standard", "fixed_levels",
                                                                                 "n/a", "other"]},
                                            "after_levels": {"type": "array", "items": {
                                                "type": "object", "additionalProperties": False,
                                                "required": ["covenant_id", "value"],
                                                "properties": {"covenant_id": {"type": "string"}, "value": {"type": "string"}}}},
                                            "quote": {"type": "string"}}},
              "not_during": {"type": "array", "items": {"type": "string"}},
              "conditions": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False, "required": ["type", "value", "quote"],
                  "properties": {"type": {"type": "string", "enum": ["notice", "min_acquisition_size", "no_default",
                                                                     "certificate", "other"]},
                                 "value": {"type": "string"}, "quote": {"type": "string"}}}},
              "quote": {"type": "string"}}}
FLAG = {"type": "object", "additionalProperties": False, "required": ["type", "ref", "quote", "note"],
        "properties": {"type": {"type": "string", "enum": ["dangling_reference", "conflict", "ambiguous_date",
                                                           "missing_schedule", "other"]},
                       "ref": {"type": "string"}, "quote": {"type": "string"}, "note": {"type": "string"},
                       "also": {"type": "array", "items": {"type": "string"}}}}
# A flag says exactly what it's about and what's uncertain, in fixed fields, so code (not text matching) decides which
# fields a reviewer can edit, which evidence to show, whether GPT's agreement settles it, and how to word the card.
TARGET = {"type": "object", "additionalProperties": False, "required": ["kind", "id", "clause"],
          "properties": {"kind": {"type": "string", "enum": ["covenant", "regime", "term"]},
                         "id": {"type": "string"},          # covenant / regime id from this extraction, or a defined term
                         "clause": {"type": "string"}}}     # a term's clause ("(a)(ii)"), else ""
NEEDS = {"type": "object", "additionalProperties": False, "required": ["needed", "what"],
         "properties": {"needed": {"type": "boolean"}, "what": {"type": "string"}}}
STRUCT = {"also": {"type": "array", "items": {"type": "string"}},
          "targets": {"type": "array", "items": TARGET},
          "question": {"type": "string"},                  # the decision a reviewer makes, one plain sentence
          "readings": {"type": "array", "items": {"type": "string"}},   # the plausible readings of the text
          "abstract_reads": {"type": "string"},            # which reading this extraction recorded
          "needs_text": NEEDS}                             # does the answer depend on text not in this filing?
FLAG["properties"].update(STRUCT)                      # stored abstracts: optional (older ones don't have them)
FLAG_OUT = {**FLAG, "required": FLAG["required"] + list(STRUCT)}
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["covenants", "regimes", "flags", "needs_context", "search_for"],
          "properties": {"covenants": {"type": "array", "items": COVENANT}, "regimes": {"type": "array", "items": REGIME},
                         "flags": {"type": "array", "items": FLAG_OUT},
                         "needs_context": {"type": "array", "items": {"type": "string"}},
                         "search_for": {"type": "array", "items": {"type": "string"}}}}

SYSTEM = ("You extract financial maintenance covenants from commercial credit agreements into a fixed structure. "
          "Work only from the text given. Every quote must be copied exactly from the text. Copy numbers as written "
          "(no arithmetic, no unit conversion); leave a field \"\" when the text doesn't say.")

RULES = """1. covenants: every MAINTENANCE financial covenant (tested periodically: leverage, coverage, liquidity, capex, cash caps).
   NOT incurrence gates (tested only when taking an action), NOT pricing grids.
   - id: short slug (e.g. "net_leverage"). ref: section as cited ("6.09(a)"). metric: ONLY the defined term name of the ratio/amount tested (e.g. "Consolidated Net Leverage Ratio"; no formula).
   - schedule: the STANDARD schedule only, one row per value, all rows including past dates. Dates: kind "date" with
     YYYY-MM-DD (inclusive test dates; a single test date has start = end), kind "quarters_after" with count + anchor
     (e.g. 6, "Closing Date") for relative dates, kind "open" for open-ended. value: decimal ("4.50", "15000000").
   - Row start = the FIRST TEST DATE the row applies to (a fiscal quarter end / the month's test day), never the day
     after the previous row or an effective date: "from the Amendment No. 3 Effective Date (Feb 15, 2024), tested on
     the last Business Day of each calendar month commencing with the first full month" -> start 2024-03-29.
   - event_tests: tests on top of the periodic ones ("each day on which a Credit Event is made" -> credit_event; on
     an acquisition, a restricted payment, an incurrence); [] if none. skip_when: when a periodic test is skipped
     (e.g. "no Revolving Loans outstanding on the test date"), else "".
   - applies_during: the id of the regime this covenant exists in, if it only applies during one; else "".
2. regimes: every mechanism that changes which threshold applies.
   - relief_period: effect "replace_schedule"; its rows go in schedules (per covenant_id), NOT in the covenant's schedule.
     early_exit: can it end early, how, and what applies after ("fixed_levels" + after_levels only if the text states them).
   - step_up (incl. "covenant holiday", "leverage increase period"): effect set_level (level) or add_increment
     (increment, ceiling); duration with its unit as written; max_uses; min_gap = time required between uses.
   - not_during: ids of regimes it cannot overlap with. conditions: notice, minimum acquisition size, etc.
3. flags: things a reviewer must see, e.g. a reference to a mechanism that no longer exists (dangling_reference),
   two provisions that conflict, a date that can't be pinned down.
   - quote = the passage in question; also = every other passage your reading relies on (a proviso, a definition,
     the clause that ends a period), each copied exactly.
   - targets = exactly what in YOUR extraction the flag is about: {"kind": "covenant"|"regime", "id": <the id you gave
     it>, "clause": ""} or {"kind": "term", "id": <defined term as written, e.g. "Total Net Leverage Ratio">,
     "clause": <its clause, e.g. "(a)(ii)", or "">}. At least one.
   - question = the decision the reviewer makes, one plain sentence, no jargon ("Does the 4.00x cap also limit the
     relief-period levels?"). readings = the plausible readings (at least two for an ambiguity; one if the flag only
     reports something). abstract_reads = which reading your extraction records. note = your reasoning.
   - needs_text = {"needed": true, "what": <what text>} if the answer depends on text you were not given (an earlier
     version of the agreement, an annex, a schedule); else {"needed": false, "what": ""}.
4. needs_context: sections ("Section 9.3(i)") or defined terms you need that are NOT in the packet; search_for: short
   phrases to look up. Ask here instead of flagging text as missing; leave both empty if the packet is enough."""

PROMPT = """From the reading packet below, extract the covenants, regimes and flags.

{rules}

READING PACKET:
<<<
{packet}
>>>"""

REPAIR = """Your previous extraction (below) failed these code checks or asked for more text. Return the complete
corrected extraction (drop any flag the new text resolves).

PROBLEMS:
{problems}
{extra}
PREVIOUS EXTRACTION:
{previous}

{rules}

READING PACKET:
<<<
{packet}
>>>"""


def _num(x):
    try:
        return float(str(x).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _d(p):
    if p["kind"] != "date":
        return None
    try:
        return date.fromisoformat(p["date"][:10])
    except ValueError:
        return "bad"


MONTHS_RX = "January|February|March|April|May|June|July|August|September|October|November|December"


def closing_date(B):
    """Code: the Closing Date when the text fixes it ('which date occurred on August 29, 2022'), only. Else None."""
    t = B.defs["defined"].get("Closing Date", {}).get("text", "")
    m = re.search(r"(" + MONTHS_RX + r")\s+(\d{1,2}),\s+(\d{4})", t)
    # 'means the date of this Agreement': the cover of a conformed copy carries the AMENDMENT's date; build.py fixes
    # these rows after the loan step has the real closing date
    if not m:
        return None
    return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date()


FIRST_TEST = {"on_or_after_closing": "the first quarter end on or after the Closing Date",
              "first_full_quarter": "the end of the first full quarter after the Closing Date"}


def first_test_date(closing, rule="on_or_after_closing"):
    """The first test date of a period that runs 'from the Closing Date'. The agreement often doesn't say; the reading is
    a setting a reviewer can change (settings.first_test), and code works out the date from it."""
    q = first_quarter_end(closing)
    if rule == "first_full_quarter" and not (closing.day == 1 and closing.month in (1, 4, 7, 10)):
        q = first_quarter_end(q + timedelta(days=1))
    return q


def first_quarter_end(d):
    for mo, dy in ((3, 31), (6, 30), (9, 30), (12, 31)):
        q = date(d.year, mo, dy)
        if q >= d:
            return q


def nth_quarter_end(anchor, n, full):
    """The n-th quarter end after anchor. full=True ('n-th full fiscal quarter ending after the Closing Date'): only
    quarters that START after the anchor count; else the first quarter end on/after the anchor is number 1."""
    q = first_quarter_end(anchor)
    if full:                                      # the quarter containing the anchor is partial: skip it
        start = date(q.year, q.month - 2, 1)
        if start <= anchor:
            q = first_quarter_end(date(q.year + (q.month == 12), (q.month % 12) + 1, 1))
    for _ in range(int(n) - 1):
        q = first_quarter_end(date(q.year + (q.month == 12), (q.month % 12) + 1, 1))
    return q


def check(out, live_sq, closing=None):
    """Code checks. Returns a list of problems, each tagged with the item it concerns."""
    P = []
    cids = {c["id"] for c in out["covenants"]}
    rids = {g["id"] for g in out["regimes"]}

    def q(where, s, required=False):
        if required and len(squash(s or "")) < 15:            # a level, covenant or regime must say where it came from
            P.append(f"{where}: quote missing or too short to locate in the agreement: {s!r}")
            return
        if s and squash(s)[:80] not in live_sq:
            P.append(f"{where}: quote not found verbatim in the agreement: {s[:80]!r}")

    def rows(where, rs):
        last_end = None
        for r in rs:
            if _num(r["value"]) is None:
                P.append(f"{where}: value {r['value']!r} is not a number")
            for side in ("start", "end"):
                p = r[side]
                if p["kind"] == "date" and _d(p) == "bad":
                    P.append(f"{where}: {side} date {p['date']!r} is not YYYY-MM-DD")
                if p["kind"] == "quarters_after" and (_num(p["count"]) is None or not p["anchor"]):
                    P.append(f"{where}: relative {side} needs a count and an anchor")
            q(where, r["quote"], required=True)
        dated = [(r, _d(r["start"]), _d(r["end"])) for r in rs if _d(r["start"]) not in (None, "bad")]
        dated.sort(key=lambda x: x[1])
        for (r1, s1, e1), (r2, s2, e2) in zip(dated, dated[1:]):
            if e1 in (None, "bad") and r1["end"]["kind"] == "open":
                P.append(f"{where}: an open-ended row is followed by a later row (rows overlap)")
            elif e1 not in (None, "bad") and s2 <= e1:
                P.append(f"{where}: rows overlap ({r1['value']} to {e1} vs {r2['value']} from {s2})")

    for c in out["covenants"]:
        w = f"covenant {c['id']}"
        q(w, c["quote"], required=True)
        # spec v1.1: a row starts on its first test date (month end / quarter end), not a period start or effective date
        for r in c["schedule"] + [r for g in out["regimes"] for s in g["schedules"] if s["covenant_id"] == c["id"] for r in s["rows"]]:
            d = _d(r["start"])
            if d not in (None, "bad") and c["frequency"] in ("monthly", "quarterly") and d.day < 25:
                P.append(f"{w}: row starting {d} ({r['value']}): start must be the first TEST DATE it applies to "
                         f"(a {c['frequency']} test date), not a period start or effective date")
            # a row "from the Closing Date" starts at the first quarter end on/after the Closing Date (code knows it)
            if closing and d not in (None, "bad") and c["frequency"] == "quarterly" and "Closing Date" in r["quote"] \
                    and d != first_quarter_end(closing):
                P.append(f"{w}: row '{r['quote'][:60]}' runs from the Closing Date ({closing}): its first test date is "
                         f"{first_quarter_end(closing)}, not {d}")
        if not c["schedule"] and not c["applies_during"]:
            P.append(f"{w}: no schedule and not tied to a regime")
        if c["applies_during"] and c["applies_during"] not in rids:
            P.append(f"{w}: applies_during {c['applies_during']!r} is not a regime id")
        rows(w, c["schedule"])
    for g in out["regimes"]:
        w = f"regime {g['id']}"
        q(w, g["quote"], required=True)
        for cid in g["applies_to"] + [s["covenant_id"] for s in g["schedules"]] + [a["covenant_id"] for a in g["early_exit"]["after_levels"]]:
            if cid not in cids:
                P.append(f"{w}: covenant id {cid!r} doesn't exist")
        for rid in g["not_during"]:
            if rid not in rids:
                P.append(f"{w}: not_during {rid!r} is not a regime id")
        if g["type"] == "relief_period" and g["effect"] == "replace_schedule":
            if not any(s["rows"] for s in g["schedules"]):
                P.append(f"{w}: replace_schedule but no schedule rows")
            for s in g["schedules"]:
                rows(f"{w} schedule for {s['covenant_id']}", s["rows"])
        if g["type"] == "step_up":
            if g["effect"] == "set_level" and _num(g["level"]) is None:
                P.append(f"{w}: set_level needs a numeric level")
            if g["effect"] == "add_increment" and _num(g["increment"]) is None:
                P.append(f"{w}: add_increment needs a numeric increment")
            if _num(g["duration"]["count"]) is None or g["duration"]["unit"] == "none":
                P.append(f"{w}: step_up needs a duration with a unit")
        ex = g["early_exit"]
        if ex["after"] == "fixed_levels" and not ex["after_levels"]:
            P.append(f"{w}: early_exit after=fixed_levels but no after_levels")
        if ex["allowed"]:
            q(w + " early_exit", ex["quote"])
        for cnd in g["conditions"]:
            q(w + " condition", cnd["quote"])
    cov_ids = {c["id"] for c in out["covenants"]}
    reg_ids = {g["id"] for g in out["regimes"]}
    for f in out["flags"]:
        q(f"flag {f['type']}", f["quote"])
        for a in f.get("also", []):
            q(f"flag {f['type']} (also)", a)
        if "targets" in f:                      # structured flags (model output from this version on)
            w = f"flag {f['type']} {f['ref']}"
            if not f["targets"]:
                P.append(f"{w}: targets is empty; name the covenant, regime or defined term the flag is about")
            for t in f["targets"]:
                if t["kind"] == "covenant" and t["id"] not in cov_ids:
                    P.append(f"{w}: target covenant '{t['id']}' is not one of your covenant ids {sorted(cov_ids)}")
                elif t["kind"] == "regime" and t["id"] not in reg_ids:
                    P.append(f"{w}: target regime '{t['id']}' is not one of your regime ids {sorted(reg_ids)}")
                elif t["kind"] == "term" and squash(t["id"]) not in live_sq:
                    P.append(f"{w}: target term '{t['id']}' is not a term in the agreement text")
            if not f.get("question", "").strip():
                P.append(f"{w}: question is empty")
            if f.get("needs_text", {}).get("needed") and not f["needs_text"].get("what", "").strip():
                P.append(f"{w}: needs_text.needed is true but what is empty")
    return P


MISSING = re.compile(r"Section\s+(\d+\.\d+[\d.]*(?:\([a-z0-9]+\))*)[^.]{0,80}?\b(not (?:been )?(?:included|in the packet|provided|"
                     r"available|shown)|missing)", re.I)


def _section_clause(B, ref, width=6000):
    """A section's text, or from the cited clause onward ('Section 9.3(i)' -> from '(i)' at a line start)."""
    sid = re.sub(r"^(Section|SECTION)\s+", "", ref.strip())
    s = next((x for x in B.sections if x["id"] == sid.split("(")[0] or x.get("printed_id") == sid.split("(")[0]), None)
    if not s:
        return None
    full = "\n".join(B.blocks[k]["text_live"] for k in range(s["start"], s["end"]) if B.blocks[k]["text_live"])
    cl = re.findall(r"\(([a-z0-9]+)\)", sid)
    if cl:
        m = re.search(r"(?:^|\n|;\s)\(" + re.escape(cl[0]) + r"\)", full)
        if m:
            return full[:300] + "\n...\n" + full[m.start():m.start() + width]
    return full[:width]


def run(borrower, model="sonnet", max_repairs=2):
    """#26: the step can ask for text (needs_context / search_for), and code also fetches any section a flag says
    is missing from the packet. One extra round is allowed for context alone."""
    from .decompose import Bundle
    b = bundle_dir(borrower)
    B = Bundle(borrower)
    packet = (b / "packet.md").read_text(encoding="utf-8")
    live_sq = squash((b / "text_live.txt").read_text(encoding="utf-8"))
    trace, have, searched, extra = [], set(), set(), []
    closing = closing_date(B)
    r = call_model(SYSTEM, PROMPT.format(rules=RULES, packet=packet), SCHEMA, model=model, timeout=1200)
    trace.append({k: r.get(k) for k in ("model", "cost_usd", "seconds", "ok")})
    if not r["ok"]:
        return {"error": r.get("error"), "trace": trace}
    out = r["output"]
    for rnd in range(max_repairs + 1):
        probs = check(out, live_sq, closing)
        asks = list(out.get("needs_context", []))
        asks += [f"Section {m.group(1)}" for f in out["flags"] for m in MISSING.finditer(f["ref"] + " " + f["note"])]
        new = []
        for n in asks:
            key = n.strip().lower()
            if key in have:
                continue
            have.add(key)
            t = _section_clause(B, n) if re.match(r"(Section|SECTION)\s+\d", n) else B.definition(n)
            new.append(f"[{n}]\n{t}" if t else f"[{n}] not found in this agreement")
        for ph in out.get("search_for", []):
            if ph.lower() in searched:
                continue
            searched.add(ph.lower())
            hits = B.search(ph)
            new.append(f"[search: {ph}]\n" + ("\n---\n".join(hits) if hits else "no matches"))
        trace[-1].update({"problems": probs, "context_fetched": [x.split("]")[0][1:] for x in new]})
        if not probs and not new:
            break
        if rnd == max_repairs and not new:
            break
        if rnd > max_repairs:
            break
        extra += new
        extra_txt = ("\nADDITIONAL TEXT FROM THE AGREEMENT (asked for, or a flag said it was missing):\n<<<\n"
                     + "\n\n".join(extra) + "\n>>>\n") if extra else ""
        r = call_model(SYSTEM, REPAIR.format(problems="\n".join(f"- {p}" for p in probs) or "- (none: revise for the new text)",
                                             extra=extra_txt, previous=json.dumps(out), rules=RULES, packet=packet),
                       SCHEMA, model=model, timeout=1200)
        trace.append({k: r.get(k) for k in ("model", "cost_usd", "seconds", "ok")})
        if not r["ok"]:
            break
        out = r["output"]
    probs = check(out, live_sq, closing)
    trace[-1]["problems"] = probs
    return {"output": out, "review": probs, "trace": trace, "context_fetched": [x.split("]")[0][1:] for x in extra],
            "cost": round(sum(t["cost_usd"] or 0 for t in trace), 4),
            "seconds": round(sum(t["seconds"] or 0 for t in trace), 1)}
