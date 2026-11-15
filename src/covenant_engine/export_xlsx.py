"""Covenant abstract -> Excel covenant testing workbook. Code only: no model call.

The tool an analyst tests the covenants with once a borrower's financial statements arrive:
- Summary: the deal, sign-off status, this test date's results, and the inputs the formulas use.
- Calendar & Levels: relief periods and step-ups, then every test date to maturity with the level that applies and
  when the financials are due.
- Definitions: each defined term the covenants use, in plain words, with the agreement text beside it.
- Covenant Test: enter four quarters (flow items) or the test-date balance (debt, cash) for each line of the
  agreement's own definitions; caps apply by formula; the ratios, headroom, status and EBITDA cushion calculate, and the
  borrower's certificate figures can be entered beside the bank's to show the differences.
- Review Trail: who checked what in the abstract.
Every level, date, line and cap comes from the checked abstract (compliance_test.plan decides the lines); every result
is an Excel formula, so the workbook recalculates as numbers are entered.

    python -m covenant_engine.export_xlsx CARS [out.xlsx]
"""
import sys
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from . import config
from .abstract import signoff as SO
from .brief import GENERIC, cap_text, cat_label, title
from .compliance_test import NOTE as NOTE_CATS, _snip, plan

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


# ---------------------------------------------------------------------------------------------------------- helpers
CAL, TEST = "Calendar & Levels", "Covenant Test"
CALQ, TESTQ = "'Calendar & Levels'", "'Covenant Test'"
GREY = PatternFill("solid", fgColor="E5E7EB")
SOURCE = {"income_statement": "Income statement", "cash_flow_statement": "Cash flow statement",
          "balance_sheet": "Balance sheet", "footnote": "Notes to the financials", "borrower_schedule": "Borrower's schedule",
          "agreement_fixed": "Fixed by the agreement"}
SIGN = {"+": "+", "-": "−", "+/-": "±"}
USD = '$#,##0;($#,##0);"-"'
QFMT = "mmm yyyy"


def _f(**k):
    return Font(name=FONT, **k)


def _put(ws, r, c, v, fmt=None, **font):
    x = ws.cell(row=r, column=c, value=v)
    x.font = _f(**font)
    if fmt:
        x.number_format = fmt
    return x


def _n(v):
    return f"{v:.0f}" if float(v).is_integer() else f"{v:g}"


def cap_expr(x, caps, regime_cell):
    """Excel expression for amount x after the formula caps (see compliance_test.caps_for)."""
    e = x
    for c in caps:
        if c[0] == "excess":
            e = f"MAX(0,{e}-{_n(c[1])})"
    normal = [c[1] for c in caps if c[0] == "min" and not c[2]]
    out = f"MIN({e},{','.join(_n(v) for v in normal)})" if normal else e
    during = {}
    for c in caps:
        if c[0] == "min" and c[2]:
            during.setdefault(c[2], []).append(c[1])
    for rid, vs in during.items():
        if regime_cell.get(rid):
            out = f'IF({regime_cell[rid]}="Yes",MIN({e},{",".join(_n(v) for v in vs)}),{out})'
    return out


def caps_words(caps, regimes):
    names = {g["id"]: g["name"] for g in regimes}
    out = []
    for c in caps:
        if c[0] == "excess":
            out.append(f"only the amount above ${c[1]:,.0f}")
        elif c[2]:
            out.append(f"max ${c[1]:,.0f} during {names.get(c[2], c[2])}")
        else:
            out.append(f"max ${c[1]:,.0f}")
    return "; ".join(out)


def default_test_date(ab, today=None):
    today = today or date.today()
    rep = sorted({_d(x["period_end"]) for x in ab.get("reported", []) if _d(x.get("period_end"))})
    rep = [d for d in rep if d <= today]
    return rep[-1] if rep else _qend(today)


def _cov_name(c):
    return c.get("name") or c["metric"]


def _wrap(x):
    x.alignment = Alignment(wrap_text=True, vertical="top")


def _named(ws, r, c, name, quote, width, bold=False, cap=320):
    """A line as the review screen shows it: a plain name, the agreement's own words underneath in small grey."""
    quote = " ".join((quote or "").split())
    quote = quote if len(quote) <= cap else quote[:cap].rsplit(" ", 1)[0] + "…"
    x = ws.cell(row=r, column=c)
    if quote and quote.lower() != (name or "").lower():
        x.value = CellRichText(TextBlock(InlineFont(rFont=FONT, sz=10, b=bold), name),
                               TextBlock(InlineFont(rFont=FONT, sz=8, color="6B7280"), "\n" + quote))
    else:
        x.value = name
        x.font = _f(bold=bold, size=10)
        quote = ""
    x.alignment = Alignment(wrap_text=True, vertical="top")
    # row height from the text (Excel doesn't size rows itself here); errs high, since clipped text is worse than space
    name_lines = -(-len(name or "") // max(20, int(width * 0.9)))
    quote_lines = -(-len(quote) // max(20, int(width * 0.88))) if quote else 0
    h = 4 + 13.5 * name_lines + 11.5 * quote_lines
    ws.row_dimensions[r].height = max(ws.row_dimensions[r].height or 0, h)
    return x


# ---------------------------------------------------------------------------------------------------------- build
def build(b, abs_dir=config.ABSTRACT_DIR, today=None, review=True, test_date=None):
    """Return an openpyxl Workbook for deal b."""
    ab, rec, _ = SO.load(b, abs_dir)
    status = rec["status"]
    ln = ab["loan"]
    P = plan(ab)
    tdate = test_date or default_test_date(ab, today)
    reliefs = [g for g in ab.get("regimes", []) if g["type"] == "relief_period"]
    wb = Workbook()
    sm = wb.active
    sm.title = "Summary"
    cal = wb.create_sheet(CAL)
    dfs = wb.create_sheet("Definitions")
    ts = wb.create_sheet(TEST)

    # ------------------------------------------------------------ Summary: deal, results (filled later), inputs
    for col, w in zip("ABCDEFG", (58, 16, 16, 14, 22, 20, 60)):
        sm.column_dimensions[col].width = w
    _put(sm, 1, 1, f"{ln.get('borrower', b)} ({b}): covenant testing workbook", bold=True, size=14)
    banner = {"draft": "DRAFT: abstract not yet reviewed and signed off. Do not rely on it for compliance.",
              "signed_off": f"Abstract SIGNED OFF by {rec.get('signed_by')}."}.get(status, status)
    bc = _put(sm, 2, 1, banner, bold=True, color="991B1B" if status != "signed_off" else "166534")
    bc.fill = RED_FILL if status != "signed_off" else GREEN_FILL
    sm.merge_cells("A2:G2")
    fac = (ln.get("facilities") or [{}])[0]
    facts = [("Borrower", ln.get("borrower", "")), ("Administrative agent", ln.get("agent", "")),
             ("Facility", f"${_num(fac.get('commitment')) or 0:,.0f} {fac.get('type', '')}".strip()),
             ("Maturity", _d(fac.get("maturity"))), ("Agreement date", _d((ln.get("agreement_date") or {}).get("value"))),
             ("Latest amendment", _d((ln.get("latest_amendment_date") or {}).get("value"))),
             ("Source document", ab["header"].get("source_file", "")), ("Abstract status", status.replace("_", " ").title()),
             ("Workbook generated", today or date.today())]
    r = 4
    for k, v in facts:
        _put(sm, r, 1, k, bold=True)
        x = _put(sm, r, 2, v, DATE_FMT if isinstance(v, date) else None)
        x.alignment = Alignment(horizontal="left")
        r += 1
    r += 1
    _put(sm, r, 1, "Results for the test date", bold=True, size=12)
    res_title = r
    r += 1
    sum_res_head = r
    r += 1 + len(P["tests"]) + 1

    _put(sm, r, 1, "Inputs the formulas use", bold=True, size=12)
    r += 1
    rp = ln.get("reporting") or {}
    fy = (ln.get("fiscal_year_end") or {}).get("value", "12-31")
    fm, fd = (int(x) for x in (fy.split("-") if "-" in fy else ("12", "31")))
    inputs = [("quarterly_days", "Quarterly financials due (days after quarter end)", _num(rp.get("quarterly_days")),
               rp.get("quote_quarterly", "")),
              ("annual_days", "Annual financials due (days after fiscal year end)", _num(rp.get("annual_days")),
               rp.get("quote_annual", "")),
              ("fye_month", "Fiscal year end: month", fm, (ln.get("fiscal_year_end") or {}).get("quote", "")),
              ("fye_day", "Fiscal year end: day", fd, (ln.get("fiscal_year_end") or {}).get("quote", "")),
              ("watch", "Watch if cushion is below", WATCH, "Assumption: a common early-warning threshold; change it")]
    for g in reliefs:
        inputs.append((f"relief_{g['id']}", f"{g['name']} still in effect? (Yes / No)", "Yes",
                       "Set to No if it ended early: the calendar and the test then use the standard terms"))
    cell = {}
    yn = DataValidation(type="list", formula1='"Yes,No"', allow_blank=False)
    sm.add_data_validation(yn)
    for key, label, val, src in inputs:
        _put(sm, r, 1, label)
        x = _put(sm, r, 2, val, PCT_FMT if key == "watch" else None, color=BLUE)
        x.fill = INPUT
        x.alignment = Alignment(horizontal="left")
        if key.startswith("relief_"):
            yn.add(x)
        s = _put(sm, r, 3, ("Source: " + src[:300]) if src and not src.startswith(("Assumption", "Set to")) else src,
                 italic=True, color="6B7280")
        sm.merge_cells(start_row=r, start_column=3, end_row=r, end_column=7)
        _wrap(s)
        cell[key] = f"Summary!$B${r}"
        r += 1
    regime_cell = {g["id"]: cell[f"relief_{g['id']}"] for g in reliefs}
    r += 1
    howto = ["How to use: 1. On Covenant Test, set the test date and enter the borrower's numbers in the yellow cells "
             "(four quarters for income-statement items, the test-date balance for debt and cash). 2. Caps, the ratios, "
             "headroom and status calculate. 3. Optionally enter the borrower's compliance certificate figures in the "
             "Borrower column to see differences. 4. Record the result on Calendar & Levels.",
             "Blue text on yellow = inputs. Black = formulas. Green = links to another line or sheet. Every line, level, "
             "date and limit comes from the checked abstract; the agreement text is beside each line and on Definitions.",
             "Limits marked 'check' need facts the workbook doesn't have (per acquisition, lifetime use): confirm by hand. "
             "Step-ups are borrower elections: shown, not applied. Not legal advice; the agreement governs."]
    for line in howto:
        x = _put(sm, r, 1, line, color="374151")
        _wrap(x)
        sm.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
        sm.row_dimensions[r].height = 44
        r += 1

    # ------------------------------------------------------------ Calendar & Levels
    widths = [38, 10, 7, 13, 13, 11, 13, 14, 12, 22, 11, 9, 11, 34, 30]
    for i, w in enumerate(widths, 1):
        cal.column_dimensions[get_column_letter(i)].width = w
    _put(cal, 1, 1, "Relief periods and step-ups", bold=True, size=12)
    regs = ab.get("regimes", [])
    r = 2
    if regs:
        heads = [(1, 1, "Name"), (2, 3, "Type"), (4, 5, "Applies to"), (6, 7, "Effect"), (8, 8, "Level / increment"),
                 (9, 9, "Ceiling"), (10, 10, "Duration"), (11, 13, "Can end early"), (14, 15, "Uses / gap between uses")]
        names = {c["id"]: _cov_name(c) for c in ab["covenants"]}
        for c1, c2, h in heads:
            x = _put(cal, r, c1, h, bold=True, color="FFFFFF")
            for k in range(c1, c2 + 1):
                cal.cell(row=r, column=k).fill = HEAD
            if c2 > c1:
                cal.merge_cells(start_row=r, start_column=c1, end_row=r, end_column=c2)
        r += 1
        for g in regs:
            ex = g.get("early_exit") or {}
            dur = (g.get("duration") or {})
            gap = (g.get("min_gap") or {})
            vals = [g["name"], g["type"].replace("_", " "), ", ".join(names.get(x, x) for x in g.get("applies_to", [])),
                    g.get("effect", "").replace("_", " "), g.get("level") or (f"+{g['increment']}x" if g.get("increment") else ""),
                    g.get("ceiling") or "", f"{dur.get('count', '')} {dur.get('unit', '')}".strip(),
                    ("Yes: " + ex.get("how", "").replace("_", " ")) if ex.get("allowed") else "No",
                    f"{g.get('max_uses') or 'no limit stated'} uses; gap {gap.get('count', '') or '—'} {gap.get('unit', '')}".strip()]
            for (c1, c2, _), v in zip(heads, vals):
                x = _put(cal, r, c1, v)
                _wrap(x)
                if c2 > c1:
                    cal.merge_cells(start_row=r, start_column=c1, end_row=r, end_column=c2)
            cal.row_dimensions[r].height = 44
            r += 1
    else:
        _put(cal, r, 1, "None in this agreement.", italic=True, color="6B7280")
        r += 1
    r += 1
    _put(cal, r, 1, "Test calendar", bold=True, size=12)
    r += 1
    head_row = r
    cols = ["Covenant", "Section", "Test", "Test date", "Standard level", "Relief level", "Level that applies",
            "Financials due", "Recorded result", "Source of result", "Headroom", "Cushion", "Status", "Step-up available",
            "Waiver / remarks"]
    for i, h in enumerate(cols, 1):
        x = _put(cal, r, i, h, bold=True, color="FFFFFF")
        x.fill = HEAD
        _wrap(x)
    cal.row_dimensions[r].height = 30
    cal.freeze_panes = cal.cell(row=r + 1, column=1)
    start, end = window(ab, today)
    reported = {(x["covenant_id"], x["period_end"]): x for x in ab.get("reported", [])}
    steps = {}
    for g in regs:
        if g["type"] == "step_up":
            what = (f"+{g['increment']}x" if g.get("increment") else f"{g.get('level')}x") + \
                   (f" (max {g['ceiling']}x)" if g.get("ceiling") else "") + " if elected"
            for cid in g.get("applies_to", []):
                steps.setdefault(cid, []).append((g, f"{g['name']}: {what}"))
    row = r + 1
    for c in ab["covenants"]:
        relief_rows = [(g, s["rows"]) for g in reliefs for s in g["schedules"] if s["covenant_id"] == c["id"]]
        for q in test_dates(c, start, end, (fm, fd)):
            std = _num(_at(c["schedule"], q))
            rel = next(((g, _num(_at(rows, q))) for g, rows in relief_rows if _at(rows, q) is not None), (None, None))
            if std is None and rel[1] is None:
                continue
            if rel[1] is not None and std is not None and abs(rel[1] - std) < 1e-9:
                rel = (None, None)
            fmt = _fmt_level(c)
            R = row
            for k, v in {1: _cov_name(c), 2: c["ref"], 3: "Max" if c["direction"] == "max" else "Min", 4: q, 5: std,
                         6: rel[1]}.items():
                _put(cal, R, k, v)
            cal.cell(row=R, column=4).number_format = DATE_FMT
            for k in (5, 6, 7, 9, 11):
                cal.cell(row=R, column=k).number_format = fmt
            _put(cal, R, 7, f'=IF(AND(F{R}<>"",{regime_cell[rel[0]["id"]]}="Yes"),F{R},E{R})' if rel[0] is not None
                 else f"=E{R}", fmt)
            _put(cal, R, 8, (f'=IF({cell["quarterly_days"]}="","",D{R}+IF(AND(MONTH(D{R})={cell["fye_month"]},'
                             f'DAY(D{R})={cell["fye_day"]}),{cell["annual_days"]},{cell["quarterly_days"]}))'), DATE_FMT)
            rep = reported.get((c["id"], q.isoformat()))
            a = _put(cal, R, 9, _num(rep["value"]) if rep else None, fmt, color=BLUE)
            a.fill = INPUT
            if rep:
                _put(cal, R, 10, f"Borrower's {rep['doc']}")
                a.comment = Comment(f"From {rep['doc']}: \"{rep['quote'][:240]}\"", "covenant-agent")
            sign = f"G{R}-I{R}" if c["direction"] == "max" else f"I{R}-G{R}"
            _put(cal, R, 11, f'=IF(I{R}="","",{sign})', fmt)
            _put(cal, R, 12, f'=IF(OR(I{R}="",G{R}=0),"",K{R}/G{R})', PCT_FMT)
            _put(cal, R, 13, f'=IF(I{R}="","",IF(K{R}>=0,"Met","Breached"))')
            blocked = {x for g, _ in steps.get(c["id"], []) for x in g.get("not_during", [])}
            avail = [t for g, t in steps.get(c["id"], []) if not (rel[0] is not None and rel[0]["id"] in set(g.get("not_during", [])) | blocked)]
            _put(cal, R, 14, "; ".join(avail) or None)
            cal.cell(row=R, column=15).fill = INPUT
            for k in range(1, 16):
                x = cal.cell(row=R, column=k)
                x.border = BOX
                if k in (10, 14, 15):
                    x.alignment = Alignment(wrap_text=True, vertical="top")
            row += 1
    cal_first, cal_last = head_row + 1, max(row - 1, head_row + 1)
    cal.auto_filter.ref = f"A{head_row}:O{cal_last}"
    _status_colors(cal, f"M{cal_first}:M{cal_last}")
    cal.conditional_formatting.add(f"L{cal_first}:L{cal_last}",
                                   FormulaRule(formula=[f'AND(L{cal_first}<>"",L{cal_first}<{cell["watch"]})'], fill=AMBER_FILL))
    _put(cal, cal_last + 2, 1, ("Recorded result: the tested figure for each date (from Covenant Test, or the borrower's "
                                "reported figure where its filing states one). Test dates are calendar quarter / month ends"
                                + (" (this borrower's fiscal quarters may end on other days)"
                                   if (fm, fd) not in ((12, 31), (3, 31), (6, 30), (9, 30)) else "") + "."),
         italic=True, color="6B7280")

    def level_lookup(c, date_cell):
        rng = lambda col: f"{CALQ}!${col}${cal_first}:${col}${cal_last}"
        name = '"' + _cov_name(c).replace('"', '""') + '"'
        return (f'IF(COUNTIFS({rng("A")},{name},{rng("D")},{date_cell})=0,"",'
                f'SUMIFS({rng("G")},{rng("A")},{name},{rng("D")},{date_cell}))')

    # ------------------------------------------------------------ Covenant Test
    for i, w in enumerate([62, 13, 5, 13, 13, 13, 13, 15, 28, 15, 15, 13, 22], 1):
        ts.column_dimensions[get_column_letter(i)].width = w
    _put(ts, 1, 1, f"Covenant test: {ln.get('borrower', b)}", bold=True, size=14)
    _put(ts, 3, 1, "Test date (quarter end)", bold=True)
    td = _put(ts, 3, 4, tdate, DATE_FMT, color=BLUE, bold=True)
    td.fill = INPUT
    TD = "$D$3"
    _put(ts, 4, 1, ("Enter amounts in dollars, as positive numbers: the ± column applies the sign (lines marked ± take a "
                    "signed amount). Income-statement items: the four quarters ending on the test date. Debt and cash: the "
                    "balance at the test date. The Borrower column is optional: the borrower's compliance certificate "
                    "figure, same sign as Counts."), italic=True, color="374151")
    ts.merge_cells("A4:M4")
    _wrap(ts["A4"])
    ts.row_dimensions[4].height = 30
    _put(ts, 6, 1, "Results", bold=True, size=12)
    res_cols = ["Covenant", "Section", "", "Result", "Level that applies", "Headroom", "Status", "EBITDA could fall by",
                "As % of EBITDA", "Borrower's figure", "Difference"]
    for i, h in enumerate(res_cols, 1):
        x = _put(ts, 7, i, h, bold=True, color="FFFFFF")
        x.fill = HEAD
        _wrap(x)
    ts.row_dimensions[7].height = 30
    r = 7 + len(P["tests"]) + 3
    total = {}                                             # term name -> its total row on this sheet
    links = []                                             # (row, term name) cells that show another block's total

    def head(r, title, point_note=None):
        vals = {1: title, 2: "Clause", 3: "±", 4: f"=EOMONTH({TD},-9)", 5: f"=EOMONTH({TD},-6)", 6: f"=EOMONTH({TD},-3)",
                7: f"={TD}", 8: point_note or "4 quarters", 9: "Limit", 10: "Counts", 11: "Borrower", 12: "Difference",
                13: "Where to find it"}
        for k, v in vals.items():
            x = _put(ts, r, k, v, QFMT if k in (4, 5, 6, 7) else None, bold=True, color="FFFFFF")
            x.fill = HEAD
            x.alignment = Alignment(wrap_text=True, vertical="center", horizontal="left" if k in (1, 2, 3, 9, 13) else "right")
        ts.row_dimensions[r].height = 30

    def line_row(r, l, point):
        _named(ts, r, 1, l["label"], l["quote"], 62, cap=220)
        _put(ts, r, 2, l["clause"])
        _put(ts, r, 3, SIGN.get(l["sign"], l["sign"]))
        if l["kind"] == "link":                       # the target total may be further down: filled in once all exist
            _put(ts, r, 8, None, USD, color=GREEN)
            links.append((r, l["ref"]))
            for k in (4, 5, 6, 7):
                ts.cell(row=r, column=k).fill = GREY
        elif point:
            for k in (4, 5, 6, 7):
                ts.cell(row=r, column=k).fill = GREY
            x = _put(ts, r, 8, None, USD, color=BLUE)
            x.fill = INPUT
        else:
            for k in (4, 5, 6, 7):
                x = _put(ts, r, k, None, USD, color=BLUE)
                x.fill = INPUT
            _put(ts, r, 8, f"=SUM(D{r}:G{r})", USD)
        v = f"N(H{r})" + (f"*{_n(l['mult'])}" if l.get("mult") else "")
        e = cap_expr(v, l["caps"], regime_cell)
        _put(ts, r, 10, f"=-({e})" if l["sign"] == "-" else f"={e}", USD)
        lim = "; ".join([x for x in [caps_words(l["caps"], regs)] if x] + ["check: " + c for c in l["checks"]])
        if l.get("pool"):
            lim = "; ".join(x for x in [lim, f"in the {l['pool'][0]} pool (below)"] if x)
        x = _put(ts, r, 9, lim or None, size=9)
        _wrap(x)
        k = _put(ts, r, 11, None, USD, color=BLUE)
        k.fill = PatternFill("solid", fgColor="FFFBEB")
        _put(ts, r, 12, f'=IF(K{r}="","",J{r}-K{r})', USD)
        _put(ts, r, 13, SOURCE.get(l["source"], "") or None, size=9)
        for c_ in range(1, 14):
            x = ts.cell(row=r, column=c_)
            x.border = BOX
            x.alignment = Alignment(vertical="top", wrap_text=c_ in (1, 9))

    def total_row(r, label, first, last, extra_rows=()):
        _put(ts, r, 1, label, bold=True)
        _put(ts, r, 10, f"=SUM(J{first}:J{last})", USD, bold=True)
        _put(ts, r, 11, f'=IF(COUNT(K{first}:K{last})=0,"",SUM(K{first}:K{last}))', USD, bold=True)
        _put(ts, r, 12, f'=IF(K{r}="","",J{r}-K{r})', USD, bold=True)
        for c_ in range(1, 14):
            ts.cell(row=r, column=c_).fill = BAND
            ts.cell(row=r, column=c_).border = Border(top=THIN, bottom=THIN)

    _put(ts, r, 1, "Defined terms the covenants use", bold=True, size=12)
    r += 2
    for bl in P["blocks"]:
        head(r, bl["name"], "At test date" if bl["point"] else None)
        r += 1
        first = r
        rows = {}
        for i, l in enumerate(bl["lines"]):
            line_row(r, l, bl["point"])
            rows[i] = r
            r += 1
        pooled = [rows[i] for i, l in enumerate(bl["lines"]) if l.get("pool")]
        for pname, pool in bl["pools"].items():
            mine = [rows[i] for i, l in enumerate(bl["lines"]) if l.get("pool") and l["pool"][0] == pname]
            P_ = "+".join(f"J{x}" for x in mine)
            base = f"(SUM(J{first}:J{r - 1})-({'+'.join(f'J{x}' for x in pooled)}))"
            pct = pool["pct"]
            allowed = f"{_n(pct)}*{base}" if pool["basis"] != "after_capped_items" else f"{_n(pct / (1 - pct))}*{base}"
            _named(ts, r, 1, f"Limit: {', '.join(pool['clauses'])} together at most {pct:.0%} of EBITDA "
                             + ("(after these add-backs)" if pool["basis"] == "after_capped_items" else
                                "(before these add-backs)" if pool["basis"] == "before_capped_items" else
                                "(basis not stated: computed before these add-backs)"), pool["quote"], 62)
            _put(ts, r, 3, "−")
            _wrap(_put(ts, r, 9, f"{pct:.0%} of EBITDA, shared", size=9))
            _put(ts, r, 10, f"=-MAX(0,({P_})-MAX(0,{allowed}))", USD)
            for c_ in range(1, 14):
                ts.cell(row=r, column=c_).border = BOX
            r += 1
        total_row(r, f"{bl['name']}", first, r - 1)
        total[bl["name"]] = r
        r += 1
        for n_ in bl["notes"]:
            x = _put(ts, r, 1, "Rule " + n_, italic=True, color="6B7280", size=9)
            ts.merge_cells(start_row=r, start_column=1, end_row=r, end_column=13)
            _wrap(x)
            ts.row_dimensions[r].height = 26
            r += 1
        r += 1

    _put(ts, r, 1, "Covenant tests", bold=True, size=12)
    r += 2
    results = []
    T_ = {t["name"]: t for t in ab.get("terms", [])}
    ebitda_total = next((total[bl["name"]] for bl in P["blocks"] if bl["family"] == "ebitda"), None)
    for t in P["tests"]:
        c = t["covenant"]
        fmt = _fmt_level(c)
        title = f"{_cov_name(c)} · §{c['ref']} · {'Max' if c['direction'] == 'max' else 'Min'}"
        side_rows = {}
        if t["kind"] == "level":
            head(r, title)
            r += 1
            l = {"label": f"{t['term']}", "clause": "", "sign": "+", "kind": "link", "ref": t["term"], "caps": [],
                 "checks": [], "source": "", "quote": "", "mult": None}
            line_row(r, l, False)
            res_cell = r
            _put(ts, r, 1, f"{t['term']}" + (" (4 quarters; tested at fiscal year end)" if c.get("frequency") == "annual" else ""))
            r += 1
            result_expr = f"J{res_cell}"
            num_r = den_r = None
        else:
            point = any(x["point"] for x in t["num"]) and not any(not x["point"] for x in t["num"])
            head(r, title)
            r += 1
            for side, label in (("num", "Numerator"), ("den", "Denominator")):
                first = r
                idx = {}
                for i, l in enumerate(t[side]):
                    line_row(r, l, l["point"])
                    idx[i] = r
                    r += 1
                for g in [g for g in t["groups"] if g["side"] == side]:
                    hs = "+".join(f"N(H{idx[i]})" for i in g["lines"])
                    e = cap_expr(f"({hs})", g["caps"], regime_cell)
                    sg = "-" if g["sign"] == "-" else ""
                    _put(ts, r, 1, "Limit on the lines above combined: " + caps_words(g["caps"], regs), italic=True)
                    _wrap(ts.cell(row=r, column=1))
                    _wrap(_put(ts, r, 9, caps_words(g["caps"], regs), size=9))
                    _put(ts, r, 10, f"={sg}(({e})-({hs}))", USD)
                    for c_ in range(1, 14):
                        ts.cell(row=r, column=c_).border = BOX
                    r += 1
                total_row(r, label, first, r - 1)
                side_rows[side] = r
                r += 1
            num_r, den_r = side_rows["num"], side_rows["den"]
            result_expr = f'IF(N(J{den_r})=0,"",J{num_r}/J{den_r})'
        # result block
        rr = {}
        for key, label in (("res", "Result"), ("lvl", "Level that applies"), ("hd", "Headroom"), ("st", "Status")):
            _put(ts, r, 1, label, bold=key in ("res", "st"))
            rr[key] = r
            r += 1
        _put(ts, rr["res"], 10, f"={result_expr}", fmt, bold=True)
        rep = reported.get((c["id"], tdate.isoformat()))
        k = _put(ts, rr["res"], 11, _num(rep["value"]) if rep else None, fmt, color=BLUE)
        k.fill = PatternFill("solid", fgColor="FFFBEB")
        if rep:
            k.comment = Comment(f"Borrower's reported figure, {rep['doc']}: \"{rep['quote'][:240]}\"", "covenant-agent")
        _put(ts, rr["res"], 12, f'=IF(OR(K{rr["res"]}="",J{rr["res"]}=""),"",J{rr["res"]}-K{rr["res"]})', fmt)
        _put(ts, rr["lvl"], 10, f"={level_lookup(c, TD)}", fmt)
        J, L = f"J{rr['res']}", f"J{rr['lvl']}"
        sign = f"{L}-{J}" if c["direction"] == "max" else f"{J}-{L}"
        _put(ts, rr["hd"], 10, f'=IF(OR({J}="",{L}=""),"",{sign})', fmt)
        _put(ts, rr["st"], 10, f'=IF({L}="","Not tested on this date",IF({J}="","",IF(J{rr["hd"]}>=0,"Met","Breached")))', bold=True)
        _status_colors(ts, f"J{rr['st']}")
        cush = None
        if t["kind"] == "ratio" and t["ebitda_side"] == "den" and c["direction"] == "max":
            cush = f'IF(OR({J}="",N({L})=0),"",J{den_r}-J{num_r}/{L})'
        elif t["kind"] == "ratio" and t["ebitda_side"] == "num" and c["direction"] == "min":
            cush = f'IF(OR({J}="",{L}=""),"",J{num_r}-{L}*J{den_r})'
        if cush:
            _put(ts, r, 1, "EBITDA could fall by (negative = shortfall)")
            _put(ts, r, 10, f"={cush}", USD)
            rr["cu"] = r
            r += 1
            if ebitda_total:
                _put(ts, r, 1, "As % of EBITDA")
                _put(ts, r, 10, f'=IF(OR(J{rr["cu"]}="",N(J{ebitda_total})<=0),"",J{rr["cu"]}/J{ebitda_total})', PCT_FMT)
                rr["cp"] = r
                r += 1
        for key in rr:
            for c_ in range(1, 14):
                ts.cell(row=rr[key], column=c_).border = BOX
        results.append((c, fmt, rr))
        r += 1

    for row_, ref in links:
        if ref in total:
            ts.cell(row=row_, column=8).value = f"=J{total[ref]}"
    ts.freeze_panes = "B8"
    for i, (c, fmt, rr) in enumerate(results):
        R = 8 + i
        vals = {1: _cov_name(c), 2: c["ref"], 4: f"=J{rr['res']}", 5: f"=J{rr['lvl']}", 6: f"=J{rr['hd']}",
                7: f"=J{rr['st']}", 8: f"=J{rr['cu']}" if "cu" in rr else None, 9: f"=J{rr['cp']}" if "cp" in rr else None,
                10: f"=K{rr['res']}", 11: f"=L{rr['res']}"}
        for k, v in vals.items():
            f_ = {4: fmt, 5: fmt, 6: fmt, 8: USD, 9: PCT_FMT, 10: fmt, 11: fmt}.get(k)
            x = _put(ts, R, k, v, f_)
            x.border = BOX
        ts.cell(row=R, column=10).value = f'=IF(K{rr["res"]}="","",K{rr["res"]})'
        ts.cell(row=R, column=11).value = f'=IF(L{rr["res"]}="","",L{rr["res"]})'
    _status_colors(ts, f"G8:G{7 + len(results)}")

    # Summary results (links to the test sheet)
    cols = ["Covenant", "Result", "Level that applies", "Headroom", "Status", "EBITDA could fall by"]
    _put(sm, res_title, 2, f"={TESTQ}!$D$3", DATE_FMT, bold=True, color=GREEN).alignment = Alignment(horizontal="left")
    for i, h in enumerate(cols, 1):
        x = _put(sm, sum_res_head, i, h, bold=True, color="FFFFFF")
        x.fill = HEAD
        _wrap(x)
    for i, (c, fmt, rr) in enumerate(results):
        R = sum_res_head + 1 + i
        _put(sm, R, 1, f"{_cov_name(c)} (§{c['ref']})")
        for k, (col, f_) in enumerate((("D", fmt), ("E", fmt), ("F", fmt), ("G", None), ("H", USD)), 2):
            _put(sm, R, k, f"={TESTQ}!{col}{8 + i}", f_, color=GREEN)
        for k in range(1, 7):
            sm.cell(row=R, column=k).border = BOX
    _status_colors(sm, f"E{sum_res_head + 1}:E{sum_res_head + len(results)}")

    # ------------------------------------------------------------ Definitions (human-readable)
    _definitions(dfs, ab, P)

    # ------------------------------------------------------------ Review Trail
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
    for w in wb.worksheets:                               # prints landscape, one page wide
        w.page_setup.orientation = "landscape"
        w.page_setup.fitToWidth, w.page_setup.fitToHeight = 1, 0
        w.sheet_properties.pageSetUpPr.fitToPage = True
    return wb


def _status_colors(ws, rng):
    ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"Breached"'], fill=RED_FILL,
                                                  font=Font(name=FONT, bold=True, color="991B1B")))
    ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"Met"'], fill=GREEN_FILL))
    ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"Not tested on this date"'],
                                                  font=Font(name=FONT, color="6B7280")))


EFFECT = {"+": "Adds", "-": "Deducts", "+/-": "Adds or deducts"}
WHAT_W = 92


def _definitions(ws, ab, P):
    """Each defined term as the review screen shows it: clause, effect, a plain name with the agreement's own words
    underneath, and the limit."""
    for i, w in enumerate([20, 16, WHAT_W, 40], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    _put(ws, 1, 1, "Definitions: how this agreement defines the numbers the covenants use", bold=True, size=14)
    _put(ws, 2, 1, "Each line: a plain name, with the agreement's own words underneath. The agreement governs.",
         italic=True, color="6B7280")
    names = {c["id"]: _cov_name(c) for c in ab["covenants"]}
    order = [bl["name"] for bl in P["blocks"]]
    terms = sorted(ab.get("terms", []), key=lambda t: (t["name"] not in order,
                                                       order.index(t["name"]) if t["name"] in order else 0))
    r = 4

    def section(text):
        nonlocal r
        _put(ws, r, 1, text, bold=True, size=12, color="FFFFFF")
        for k in range(1, 5):
            ws.cell(row=r, column=k).fill = HEAD
        r += 1

    def header(cols):
        nonlocal r
        for i, h in enumerate(cols, 1):
            _put(ws, r, i, h, bold=True)
            ws.cell(row=r, column=i).border = Border(bottom=THIN)
        r += 1

    def row(a, b_, name, quote, d="", fmt=None):
        nonlocal r
        for i, v in ((1, a), (2, b_), (4, d)):
            x = _put(ws, r, i, v, fmt if i == 2 else None)
            _wrap(x)
        _named(ws, r, 3, name, quote, WHAT_W, cap=600)
        for i in range(1, 5):
            ws.cell(row=r, column=i).border = BOX
        r += 1

    section("Defined terms")
    r += 1
    for t in terms:
        if t.get("family") == "ratio":
            what = "The covenant ratio: " + " over ".join(
                x for x in ([", ".join(p.get("ref_term") or cat_label(p["category"]) for p in t["components"]
                                       if p["category"].startswith("numerator") and p["sign"] == "+")] +
                            [", ".join(p.get("ref_term") or cat_label(p["category"]) for p in t["components"]
                                       if p["category"].startswith("denominator"))]) if x)
        else:
            what = ("Starts from " + t["starts_from"] + "." if t.get("starts_from") and len(t["starts_from"]) <= 60 else "")
        used = ", ".join(names.get(x, x) for x in t.get("used_by", []))
        n_add = sum(p["sign"] == "+" for p in t["components"] if p["category"] not in NOTE_CATS)
        n_sub = sum(p["sign"] == "-" for p in t["components"] if p["category"] not in NOTE_CATS)
        n_cap = sum(bool(p.get("caps")) for p in t["components"])
        counts = f"{n_add} added, {n_sub} subtracted" + (f", {n_cap} with a limit" if n_cap else "")
        _put(ws, r, 1, f"{t['name']}: {counts}", bold=True, size=11)
        for k in range(1, 5):
            ws.cell(row=r, column=k).fill = BAND
        r += 1
        line = " ".join(s for s in [what, f"Used by: {used}." if used else ""] if s)
        if line:
            _put(ws, r, 1, line, italic=True, color="374151")
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
            r += 1
        if t["components"]:
            header(["Clause", "Effect", "What it is", "Limit"])
        for p in t["components"]:
            structural = p["category"] == "term_reference" or p["category"].startswith(("numerator", "denominator"))
            name = (f"{p['ref_term']} (defined term)" if structural and p.get("ref_term") else
                    _snip(p.get("quote")) if p["category"] in GENERIC and p.get("quote") else title(p["category"]))
            row(p.get("clause", ""), "Rule" if p["category"] in NOTE_CATS else EFFECT.get(p["sign"], p["sign"]), name,
                p.get("quote", ""), "; ".join(cap_text(k) for k in p.get("caps", [])))
        r += 1
    if ab.get("terms_pending"):
        for name in ab["terms_pending"]:
            _put(ws, r, 1, name, bold=True)
            _put(ws, r, 3, "Feeds a covenant but isn't broken down in the abstract: read it in the agreement", italic=True)
            r += 1
        r += 1
    section("Covenant levels as written")
    header(["Section", "Level", "Covenant and period", ""])
    for c in ab["covenants"]:
        for s_ in c["schedule"]:
            frm = _d(s_["start"].get("date")) or s_["start"].get("kind")
            to = _d(s_["end"].get("date")) or ("thereafter" if s_["end"].get("kind") == "open" else s_["end"].get("kind"))
            when = (f"{frm:%b %d, %Y}" if isinstance(frm, date) else str(frm)) + " to " + \
                   (f"{to:%b %d, %Y}" if isinstance(to, date) else str(to))
            row(c["ref"], _num(s_["value"]), f"{_cov_name(c)} ({'max' if c['direction'] == 'max' else 'min'}), {when}",
                s_.get("quote", ""), fmt=_fmt_level(c))
    r += 1
    if ab.get("regimes"):
        section("Relief periods and step-ups as written")
        header(["", "Type", "What it is", ""])
        for g in ab["regimes"]:
            for lab, q in (("", g.get("quote", "")), ("how it can end early", (g.get("early_exit") or {}).get("quote", ""))):
                if q:
                    row("", g["type"].replace("_", " "), g["name"] + (f": {lab}" if lab else ""), q)
        r += 1
    if ab.get("calculation_rules"):
        section("Rules for the whole calculation")
        header(["Section", "Rule", "What it means", ""])
        for k in ab["calculation_rules"]:
            row(k.get("section", ""), k.get("type", "").replace("_", " ").capitalize(), k.get("summary", ""), k.get("quote", ""))
        r += 1
    if ab.get("deemed_values"):
        section("Values the agreement fixes")
        header(["Period end", "Value", "What it is", ""])
        for k in ab["deemed_values"]:
            row(_d(k.get("period_end")), _num(k.get("value")), f"{k.get('term', '')} of {k.get('entity', '')}",
                k.get("quote", ""), fmt=USD)
        for x in ws.iter_rows(min_row=4, max_row=r, min_col=1, max_col=1):
            if isinstance(x[0].value, date):
                x[0].number_format = DATE_FMT


def export(b, out=None, abs_dir=config.ABSTRACT_DIR, today=None, test_date=None):
    out = Path(out or f"{b}_covenant_testing.xlsx")
    build(b, abs_dir, today, test_date=test_date).save(out)
    return out


if __name__ == "__main__":
    print(export(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
