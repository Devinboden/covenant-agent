"""The covenant testing workbook is code only: lines, caps and levels from the abstract; every result by formula."""
from datetime import date

from covenant_engine.abstract import signoff as SO
from covenant_engine.compliance_test import plan
from covenant_engine.export_xlsx import build, cap_expr

TODAY = date(2026, 11, 14)


def _calendar(ws):
    head = next(r for r in range(1, ws.max_row + 1) if ws.cell(r, 4).value == "Test date")
    return {(r[0].value, r[3].value.date() if hasattr(r[3].value, "date") else r[3].value): r
            for r in ws.iter_rows(min_row=head + 1) if r[0].value and r[3].value}


def _test_rows(ws):
    """{(block title, clause, label): row} on the Covenant Test sheet."""
    out, block = {}, None
    for r in range(1, ws.max_row + 1):
        if ws.cell(r, 2).value == "Clause":
            block = ws.cell(r, 1).value
        elif block and ws.cell(r, 1).value:
            out.setdefault((block, ws.cell(r, 2).value or "", str(ws.cell(r, 1).value)), r)
    return out


def _find(rows, block, clause="", label=""):
    return next(r for (b, c, l), r in rows.items() if b.startswith(block) and c == clause and str(l).startswith(label))


# ------------------------------------------------------------------------------------------------ what gets tested
def test_mec_plan_follows_the_definitions():
    P = plan(SO.load("MEC")[0])
    blocks = {b["name"]: b for b in P["blocks"]}
    eb = blocks["Consolidated EBITDA"]
    pool = eb["pools"]["cash add-backs"]                       # one 15% cap shared by six add-backs
    assert pool["pct"] == 0.15 and pool["clauses"] == ["(b)(v)", "(b)(vii)", "(b)(viii)", "(b)(xi)", "(b)(xii)", "(b)(xiii)"]
    assert eb["lines"][0]["label"] == "Consolidated Net Income"
    interest = next(l for l in eb["lines"] if l["clause"] == "(b)(i)")
    assert interest["kind"] == "link" and interest["ref"] == "Consolidated Interest Expense"   # not entered twice
    assert blocks["Indebtedness"]["point"] and not eb["point"]   # balance at the test date vs four quarters
    tests = {t["covenant"]["id"]: t for t in P["tests"]}
    cash = tests["total_leverage"]["num"][1]
    assert cash["caps"] == [("min", 25000000.0, None), ("min", 10000000.0, "covenant_adjustment_period")]
    capex = tests["interest_coverage"]["num"][1]
    assert capex["kind"] == "link" and capex["mult"] == 0.5    # "minus (ii) 50% of Capital Expenditures"
    assert tests["capex"]["kind"] == "level"


def test_trs_shared_netting_limit_and_cars_pool():
    P = plan(SO.load("TRS")[0])
    lev = next(t for t in P["tests"] if t["covenant"]["id"] == "net_leverage")
    (g,) = lev["groups"]                                       # cash + permitted investments: one limit on the sum
    assert g["caps"] == [("excess", 10000000.0), ("min", 240000000.0, None)]   # §6.13's $240M replaces the general $150M
    P = plan(SO.load("CARS")[0])
    eb = next(b for b in P["blocks"] if b["name"] == "Consolidated EBITDA")
    assert eb["pools"]["(f)/(r) cap"]["clauses"] == ["(f)", "(r)"]


def test_cap_expression():
    caps = [("excess", 10000000.0), ("min", 240000000.0, None)]
    assert cap_expr("N(H5)", caps, {}) == "MIN(MAX(0,N(H5)-10000000),240000000)"
    caps = [("min", 25000000.0, None), ("min", 10000000.0, "cap")]
    assert cap_expr("N(H5)", caps, {"cap": "Summary!$B$20"}) == \
        'IF(Summary!$B$20="Yes",MIN(N(H5),10000000),MIN(N(H5),25000000))'


# ------------------------------------------------------------------------------------------------ the workbook
def test_mec_workbook_tests_from_the_lines():
    wb = build("MEC", today=TODAY, review=False)
    assert wb.sheetnames == ["Summary", "Calendar & Levels", "Covenant Test", "Definitions"]
    assert "DRAFT" in wb["Summary"]["B5"].value
    ts = wb["Covenant Test"]                                   # the covenant in words, at the top of the test sheet
    box = {ts.cell(r, 1).value.split("  §")[0]: ts.cell(r, 2).value for r in range(7, 10)}
    assert box["Consolidated Total Leverage Ratio"].startswith("Consolidated Total Leverage Ratio = (Consolidated Total "
                                                                "Indebtedness − Unrestricted Cash")
    assert "÷ Consolidated EBITDA, which must not exceed" in box["Consolidated Total Leverage Ratio"]
    assert "× 50%" in box["Consolidated Interest Coverage Ratio"]
    cal = _calendar(wb["Calendar & Levels"])
    r = cal[("Consolidated Total Leverage Ratio", date(2026, 6, 30))]
    assert r[4].value == 3.5 and r[5].value == 5.25 and "Summary!" in r[6].value   # relief level on a switch
    ts = wb["Covenant Test"]
    assert ts["D3"].value == date(2026, 6, 30)                 # latest quarter the borrower reported
    rows = _test_rows(ts)
    cash = _find(rows, "Consolidated Total Leverage", "(a)(ii)")
    assert ts.cell(cash, 10).value.startswith('=-(IF(Summary!') and "10000000" in ts.cell(cash, 10).value
    ni = _find(rows, "Consolidated EBITDA", "", "Consolidated Net Income")
    assert ts.cell(ni, 8).value == f"=SUM(D{ni}:G{ni})"         # four quarters summed
    pool = next(r for r in range(1, ts.max_row + 1) if str(ts.cell(r, 1).value or "").startswith("Limit: (b)(v)"))
    assert ts.cell(pool, 10).value.startswith("=-MAX(0,") and "0.15*" in ts.cell(pool, 10).value
    res = next(r for r in range(cash, ts.max_row + 1) if ts.cell(r, 1).value == "Result")
    assert ts.cell(res, 10).value.startswith('=IF(N(J') and ts.cell(res, 11).value == 2.91   # borrower's 10-Q figure
    lvl = ts.cell(res + 1, 10).value
    assert "COUNTIFS('Calendar & Levels'!" in lvl and "$D$3" in lvl


def test_files_open_in_excel_without_repair(tmp_path):
    """Excel repairs (and may discard) a sheet with an empty container element that LibreOffice accepts: an empty
    <dataValidations/> (a deal with no relief period has no Yes/No switch), <conditionalFormatting/> or <mergeCells/>."""
    import re
    import zipfile
    for deal in ("CARS", "MEC", "TRS"):
        out = tmp_path / f"{deal}.xlsx"
        build(deal, today=TODAY).save(out)
        with zipfile.ZipFile(out) as z:
            for name in z.namelist():
                if name.startswith("xl/worksheets/sheet"):
                    xml = z.read(name).decode()
                    empty = re.findall(r'<(dataValidations|conditionalFormatting|mergeCells|hyperlinks)\b[^>]*count="0"[^>]*/>', xml)
                    assert not empty, f"{deal} {name}: empty {empty}"
