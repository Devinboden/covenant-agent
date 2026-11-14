"""Covenant abstract -> Excel compliance workbook. Code only: no model call.

The abstract holds what the agreement says; the workbook turns it into what a portfolio manager works from each
quarter (a covenant tickler): every test date to maturity, the level that applies, when the financials are due, the
reported actual, and the headroom and Met / Breached status, computed by Excel formulas so they recalculate when an
actual is entered. Levels, dates and deadlines come from the checked abstract (each with its agreement quote); the
review trail shows who checked what. Layout follows common practice: a tickler with Required / Actual / Complies
columns, headroom and cushion, waiver and remarks columns, and an agreement companion (definitions, step-ups).

    python -m covenant_engine.export_xlsx CARS [out.xlsx]
"""
import sys
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import config
from .abstract import signoff as SO
from .brief import cap_text, cat_label

FONT = "Arial"
HEAD = PatternFill("solid", fgColor="1F2937")
BAND = PatternFill("solid", fgColor="F3F4F6")
INPUT = PatternFill("solid", fgColor="FFF7CC")         # cells a person fills in
RED_FILL = PatternFill("solid", fgColor="FDE2E1")
AMBER_FILL = PatternFill("solid", fgColor="FEF3C7")
GREEN_FILL = PatternFill("solid", fgColor="DCFCE7")
THIN = Side(style="thin", color="D1D5DB")
BOX = Border(bottom=THIN)
BLUE = "0000FF"                                       # hardcoded input (financial-model convention)
GREEN = "008000"                                      # link to another sheet
RATIO_FMT, USD_FMT, PCT_FMT, DATE_FMT = '0.00"x"', '$#,##0', '0.0%', "mmm d, yyyy"
WATCH = 0.10                                          # cushion below this is flagged for attention (an assumption)


def _d(s):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _qend(d):
    """The quarter end on or before d."""
    q = (d.month - 1) // 3
    end = date(d.year, q * 3 + 3, 1)
    end = date(end.year + (end.month == 12), end.month % 12 + 1, 1) - timedelta(days=1)
    if end > d:
        end = _qend(date(d.year, q * 3 + 1, 1) - timedelta(days=1))
    return end


def _next_q(d):
    m = d.month + 3
    y = d.year + (m > 12)
    m = (m - 1) % 12 + 1
    return date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def _month_ends(start, end):
    d, out = start, []
    while d <= end:
        out.append(d)
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        d = date(nxt.year + (nxt.month == 12), nxt.month % 12 + 1, 1) - timedelta(days=1)
    return out


def _at(rows, q):
    """Value a schedule gives at test date q; later rows win (amended text is listed last)."""
    v = None
    for r in rows:
        s, e = _d(r["start"].get("date")), _d(r["end"].get("date"))
        if (s is None or s <= q) and (e is None or q <= e):
            v = r["value"]
    return v


def test_dates(c, start, end, fye):
    if c["frequency"] == "monthly":
        return _month_ends(start, end)
    qs, q = [], start
    while q <= end:
        qs.append(q)
        q = _next_q(q)
    if c["frequency"] == "annual":
        qs = [q for q in qs if (q.month, q.day) == fye] or qs[-1:]
    return qs


def window(ab, today=None):
    """Test dates shown: three quarters back from the last completed quarter (so recent reported actuals line up),
    forward to maturity, at most 20 quarters."""
    today = today or date.today()
    last = _qend(today)
    start = last
    for _ in range(3):
        start = _qend(start - timedelta(days=1))
    mats = [_d(f.get("maturity")) for f in (ab["loan"].get("facilities") or [])]
    end = max([m for m in mats if m] or [last + timedelta(days=5 * 365)])
    cap = start
    for _ in range(20):
        cap = _next_q(cap)
    return start, min(end, cap)


# ---------------------------------------------------------------------------------------------------------- sheets
def _style_header(ws, row, cols):
    for i, h in enumerate(cols, 1):
        c = ws.cell(row=row, column=i, value=h)
        c.font = Font(name=FONT, bold=True, color="FFFFFF")
        c.fill = HEAD
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 30
    ws.freeze_panes = ws.cell(row=row + 1, column=1)


def _table(ws, row, cols, rows, widths, wrap=()):
    _style_header(ws, row, cols)
    for r, vals in enumerate(rows, row + 1):
        for i, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=i, value=v)
            c.font = Font(name=FONT)
            c.alignment = Alignment(vertical="top", wrap_text=i in wrap)
            c.border = BOX
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.auto_filter.ref = f"A{row}:{get_column_letter(len(cols))}{max(row, row + len(rows))}"


def _fmt_level(c):
    return USD_FMT if c.get("unit") == "usd" else RATIO_FMT


def build(b, abs_dir=config.ABSTRACT_DIR, today=None, review=True):
    """Return an openpyxl Workbook for deal b."""
    ab, rec, _ = SO.load(b, abs_dir)
    status = rec["status"]
    ln = ab["loan"]
    wb = Workbook()

    # ---------------------------------------------------------------- Summary (with the inputs the formulas use)
    ws = wb.active
    ws.title = "Summary"
    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 58
    ws.column_dimensions["C"].width = 60
    t = ws.cell(row=1, column=1, value=f"{ln.get('borrower', b)} ({b}) covenant compliance workbook")
    t.font = Font(name=FONT, bold=True, size=14)
    banner = {"draft": "DRAFT: not yet verified and signed off. Do not rely on it for compliance.",
              "verified": "VERIFIED by one reviewer; awaiting a second reviewer's sign-off.",
              "signed_off": "SIGNED OFF by two reviewers."}.get(status, status)
    bc = ws.cell(row=2, column=1, value=banner)
    bc.font = Font(name=FONT, bold=True, color="991B1B" if status != "signed_off" else "166534")
    bc.fill = RED_FILL if status != "signed_off" else GREEN_FILL
    ws.merge_cells("A2:C2")
    fac = (ln.get("facilities") or [{}])[0]
    rp = ln.get("reporting") or {}
    facts = [("Borrower", ln.get("borrower", "")), ("Administrative agent", ln.get("agent", "")),
             ("Facility", f"${_num(fac.get('commitment')) or 0:,.0f} {fac.get('type', '')}".strip()),
             ("Maturity", _d(fac.get("maturity"))), ("Agreement date", _d((ln.get("agreement_date") or {}).get("value"))),
             ("Latest amendment", _d((ln.get("latest_amendment_date") or {}).get("value"))),
             ("Test period", (ln.get("test_period") or {}).get("basis", "").replace("_", " ")),
             ("Source document", ab["header"].get("source_file", "")), ("Source SHA-256", ab["header"].get("sha256", "")),
             ("Abstract built", _d(ab["header"].get("built"))), ("Status", status.replace("_", " ").title()),
             ("Verified by", rec.get("verified_by") or "—"), ("Signed off by", rec.get("signed_by") or "—"),
             ("Workbook generated", today or date.today())]
    r = 4
    for k, v in facts:
        ws.cell(row=r, column=1, value=k).font = Font(name=FONT, bold=True)
        c = ws.cell(row=r, column=2, value=v)
        c.font = Font(name=FONT)
        if isinstance(v, date):
            c.number_format = DATE_FMT
            c.alignment = Alignment(horizontal="left")
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="Inputs the Test Calendar uses").font = Font(name=FONT, bold=True, size=12)
    r += 1
    fy = (ln.get("fiscal_year_end") or {}).get("value", "12-31")
    fm, fd = (int(x) for x in (fy.split("-") if "-" in fy else ("12", "31")))
    inputs = [("quarterly_days", "Quarterly financials due (days after quarter end)", _num(rp.get("quarterly_days")),
               rp.get("quote_quarterly", "")),
              ("annual_days", "Annual financials due (days after fiscal year end)", _num(rp.get("annual_days")),
               rp.get("quote_annual", "")),
              ("fye_month", "Fiscal year end: month", fm, (ln.get("fiscal_year_end") or {}).get("quote", "")),
              ("fye_day", "Fiscal year end: day", fd, (ln.get("fiscal_year_end") or {}).get("quote", "")),
              ("watch", "Watch if cushion is below", WATCH, "Assumption: a common early-warning threshold; change it")]
    reliefs = [g for g in ab.get("regimes", []) if g["type"] == "relief_period"]
    for g in reliefs:
        inputs.append((f"relief_{g['id']}", f"{g['name']} still in effect? (Yes / No)", "Yes",
                       "Set to No if it ended early; the Test Calendar then applies the standard levels"))
    cell = {}
    for key, label, val, src in inputs:
        ws.cell(row=r, column=1, value=label).font = Font(name=FONT)
        c = ws.cell(row=r, column=2, value=val)
        c.font = Font(name=FONT, color=BLUE)
        c.fill = INPUT
        c.alignment = Alignment(horizontal="left")
        if key == "watch":
            c.number_format = PCT_FMT
        s = ws.cell(row=r, column=3, value=("Source: " + src[:300]) if src and not src.startswith("Assumption") else src)
        s.font = Font(name=FONT, italic=True, color="6B7280")
        s.alignment = Alignment(wrap_text=True, vertical="top")
        cell[key] = f"Summary!$B${r}"
        r += 1
    r += 1
    legend = ["How to use: blue text on yellow = inputs (edit them). Black = formulas. Levels and dates come from the "
              "checked abstract, each with its agreement quote on the Covenants tab.",
              "Test Calendar: type the borrower's reported ratio or amount in Actual; Headroom, Cushion and Status update. "
              "Actuals from the borrower's 10-Q/10-K are prefilled where the filing states them.",
              "Not legal advice; the agreement governs. Step-ups are elections: shown, not applied."]
    for line in legend:
        c = ws.cell(row=r, column=1, value=line)
        c.font = Font(name=FONT, color="374151")
        c.alignment = Alignment(wrap_text=True)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        ws.row_dimensions[r].height = 30
        r += 1

    # ---------------------------------------------------------------- Test Calendar (the tickler)
    tc = wb.create_sheet("Test Calendar")
    cols = ["Covenant", "Section", "Test", "Test date", "Standard level", "Relief level", "Level that applies",
            "Financials due", "Actual", "Source of actual", "Headroom", "Cushion", "Status", "Step-up available",
            "Waiver / remarks"]
    _style_header(tc, 1, cols)
    for i, w in enumerate([38, 10, 7, 13, 13, 11, 13, 14, 11, 22, 11, 9, 11, 34, 30], 1):
        tc.column_dimensions[get_column_letter(i)].width = w
    start, end = window(ab, today)
    reported = {(x["covenant_id"], x["period_end"]): x for x in ab.get("reported", [])}
    steps = {}                                     # elective step-ups: shown where they could apply, never applied
    for g in ab.get("regimes", []):
        if g["type"] == "step_up":
            what = (f"+{g['increment']}x" if g.get("increment") else f"{g.get('level')}x") + \
                   (f" (max {g['ceiling']}x)" if g.get("ceiling") else "") + " if elected"
            for cid in g.get("applies_to", []):
                steps.setdefault(cid, []).append((g, f"{g['name']}: {what}"))
    row = 2
    for c in ab["covenants"]:
        relief_rows = [(g, s["rows"]) for g in reliefs for s in g["schedules"] if s["covenant_id"] == c["id"]]
        for q in test_dates(c, start, end, (fm, fd)):
            std = _num(_at(c["schedule"], q))
            rel = next(((g, _num(_at(rows, q))) for g, rows in relief_rows if _at(rows, q) is not None), (None, None))
            if std is None and rel[1] is None:
                continue
            if rel[1] is not None and std is not None and abs(rel[1] - std) < 1e-9:
                rel = (None, None)                 # the relief schedule repeats the standard level: nothing to switch
            fmt = _fmt_level(c)
            vals = {1: c.get("name") or c["metric"], 2: c["ref"], 3: "Max" if c["direction"] == "max" else "Min", 4: q,
                    5: std, 6: rel[1]}
            for k, v in vals.items():
                x = tc.cell(row=row, column=k, value=v)
                x.font = Font(name=FONT)
            tc.cell(row=row, column=4).number_format = DATE_FMT
            for k in (5, 6, 7, 9, 11):
                tc.cell(row=row, column=k).number_format = fmt
            R = row
            if rel[0] is not None:
                tc.cell(row=R, column=7, value=f'=IF(AND(F{R}<>"",{cell["relief_" + rel[0]["id"]]}="Yes"),F{R},E{R})')
            else:
                tc.cell(row=R, column=7, value=f"=E{R}")
            tc.cell(row=R, column=8, value=(f'=IF({cell["quarterly_days"]}="","",D{R}+IF(AND(MONTH(D{R})={cell["fye_month"]},'
                                            f'DAY(D{R})={cell["fye_day"]}),{cell["annual_days"]},{cell["quarterly_days"]}))'))
            tc.cell(row=R, column=8).number_format = DATE_FMT
            rep = reported.get((c["id"], q.isoformat()))
            a = tc.cell(row=R, column=9, value=_num(rep["value"]) if rep else None)
            a.font = Font(name=FONT, color=BLUE)
            a.fill = INPUT
            if rep:
                tc.cell(row=R, column=10, value=f"{rep['doc']}")
                a.comment = Comment(f"From {rep['doc']}: \"{rep['quote'][:240]}\"", "covenant-agent")
            sign = f"G{R}-I{R}" if c["direction"] == "max" else f"I{R}-G{R}"
            tc.cell(row=R, column=11, value=f'=IF(I{R}="","",{sign})')
            tc.cell(row=R, column=12, value=f'=IF(OR(I{R}="",G{R}=0),"",K{R}/G{R})').number_format = PCT_FMT
            tc.cell(row=R, column=13, value=f'=IF(I{R}="","",IF(K{R}>=0,"Met","Breached"))')
            blocked = {x for g, _ in steps.get(c["id"], []) for x in g.get("not_during", [])}
            avail = [t for g, t in steps.get(c["id"], []) if not (rel[0] is not None and rel[0]["id"] in set(g.get("not_during", [])) | blocked)]
            tc.cell(row=R, column=14, value="; ".join(avail) or None)
            w = tc.cell(row=R, column=15)
            w.fill = INPUT
            for k in range(1, 16):
                x = tc.cell(row=R, column=k)
                x.border = BOX
                if not x.font or x.font.name != FONT:
                    x.font = Font(name=FONT, color=x.font.color.rgb if x.font and x.font.color and x.font.color.type == "rgb" else None)
                if k in (10, 14, 15):
                    x.alignment = Alignment(wrap_text=True, vertical="top")
            row += 1
    last = max(row - 1, 2)
    tc.auto_filter.ref = f"A1:O{last}"
    tc.conditional_formatting.add(f"M2:M{last}", CellIsRule(operator="equal", formula=['"Breached"'], fill=RED_FILL,
                                                             font=Font(name=FONT, bold=True, color="991B1B")))
    tc.conditional_formatting.add(f"M2:M{last}", CellIsRule(operator="equal", formula=['"Met"'], fill=GREEN_FILL))
    tc.conditional_formatting.add(f"L2:L{last}", FormulaRule(formula=[f'AND(L2<>"",L2<{cell["watch"]})'], fill=AMBER_FILL))
    tc.cell(row=last + 2, column=1, value=("Levels from the abstract's covenant schedules and relief periods. Test dates "
                                           "are calendar quarter / month ends" + (" (this borrower's fiscal quarters may "
                                           "end on other days)" if (fm, fd) not in ((12, 31), (3, 31), (6, 30), (9, 30)) else "")
                                           + ". Headroom = level − actual for a maximum, actual − level for a minimum.")
            ).font = Font(name=FONT, italic=True, color="6B7280")

    # ---------------------------------------------------------------- Covenants (schedules as written)
    cv = wb.create_sheet("Covenants")
    rows = []
    for c in ab["covenants"]:
        for s in c["schedule"]:
            rows.append([c.get("name") or c["metric"], c["ref"], "Max" if c["direction"] == "max" else "Min",
                         c["frequency"], _d(s["start"].get("date")) or s["start"].get("kind"),
                         _d(s["end"].get("date")) or ("thereafter" if s["end"].get("kind") == "open" else s["end"].get("kind")),
                         _num(s["value"]), s.get("quote", "")])
    _table(cv, 1, ["Covenant", "Section", "Test", "Frequency", "From", "To", "Level", "Agreement text"], rows,
           [30, 10, 7, 11, 13, 13, 12, 90], wrap=(8,))
    for i, c in enumerate([c for c in ab["covenants"] for _ in c["schedule"]], 2):
        cv.cell(row=i, column=7).number_format = _fmt_level(c)
        for k in (5, 6):
            if isinstance(cv.cell(row=i, column=k).value, date):
                cv.cell(row=i, column=k).number_format = DATE_FMT

    # ---------------------------------------------------------------- Step-ups & Relief
    rg = wb.create_sheet("Step-ups & Relief")
    rows = []
    for g in ab.get("regimes", []):
        ex = g.get("early_exit") or {}
        rows.append([g["name"], g["type"].replace("_", " "), ", ".join(g.get("applies_to", [])), g["effect"].replace("_", " "),
                     g.get("level") or g.get("increment") or "", g.get("ceiling") or "",
                     f"{(g.get('duration') or {}).get('count', '')} {(g.get('duration') or {}).get('unit', '')}".strip(),
                     g.get("max_uses") or "no limit stated",
                     f"{(g.get('min_gap') or {}).get('count', '')} {(g.get('min_gap') or {}).get('unit', '')}".strip(),
                     ("Yes: " + ex.get("how", "").replace("_", " ") + (", then " + ", ".join(f"{x['covenant_id']} {x['value']}" for x in ex.get("after_levels", []))
                                                                      if ex.get("after_levels") else "")) if ex.get("allowed") else "No",
                     g.get("quote", "")])
    _table(rg, 1, ["Name", "Type", "Applies to", "Effect", "Level / increment", "Ceiling", "Duration", "Max uses",
                   "Gap between uses", "Can end early", "Agreement text"], rows, [26, 12, 22, 16, 12, 9, 12, 12, 12, 28, 80],
           wrap=(10, 11))

    # ---------------------------------------------------------------- Definitions (the agreement companion)
    df = wb.create_sheet("Definitions")
    rows = []
    for t in ab.get("terms", []):
        for p in t["components"]:
            rows.append([t["name"], p.get("clause", ""), {"+": "added", "-": "subtracted"}.get(p["sign"], p["sign"]),
                         cat_label(p["category"]), "; ".join(cap_text(k) for k in p.get("caps", [])) or "",
                         p.get("ref_term", ""), p.get("quote", "")])
    for name in ab.get("terms_pending", []):
        rows.append([name, "", "", "not broken down", "", "", "Feeds a covenant; read the definition in the agreement"])
    _table(df, 1, ["Defined term", "Clause", "Effect", "Category", "Limit", "Refers to", "Agreement text"], rows,
           [30, 14, 11, 26, 28, 24, 80], wrap=(5, 7))

    # ---------------------------------------------------------------- Review Trail (who checked what)
    if review:
        from .review_view import payload
        p = payload(b, "export", abs_dir)
        word = {"accept": "Confirmed", "correct": "Changed", "defer": "Later", "reject": "Doesn't belong"}
        rows = []
        for it in p["items"]:
            r_ = it.get("resolution") or {}
            changes = "; ".join(f"{c['label']}: {c['before']} -> {c['after']}" for c in r_.get("changes", []))
            rows.append([it.get("where_label", ""), {"decide": "High", "confirm": "Low", "ack": "Acknowledgment"}.get(it["level"], it["level"]),
                         it.get("explain", ""), word.get(r_.get("decision"), "Open"), r_.get("by", ""), _d(r_.get("date")) or "",
                         r_.get("note", ""), changes])
        _table(wb.create_sheet("Review Trail"), 1, ["Item", "Priority", "What was checked", "Decision", "By", "Date", "Note",
                                                     "Changes (before -> after)"], rows,
               [34, 12, 60, 12, 14, 12, 34, 40], wrap=(1, 3, 7, 8))
    return wb


def export(b, out=None, abs_dir=config.ABSTRACT_DIR, today=None):
    out = Path(out or f"{b}_covenant_compliance.xlsx")
    build(b, abs_dir, today).save(out)
    return out


if __name__ == "__main__":
    print(export(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
