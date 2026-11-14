"""The Excel compliance workbook is code only: levels, dates and actuals from the abstract, headroom and status by formula."""
from datetime import date

from covenant_engine.export_xlsx import build


def _rows(ws):
    return {(r[0].value, r[3].value.date() if hasattr(r[3].value, "date") else r[3].value): r for r in ws.iter_rows(min_row=2) if r[0].value and r[3].value}


def test_cars_tickler_has_levels_actuals_and_formulas():
    wb = build("CARS", today=date(2026, 11, 14), review=False)
    assert wb.sheetnames == ["Summary", "Test Calendar", "Covenants", "Step-ups & Relief", "Definitions"]
    rows = _rows(wb["Test Calendar"])
    lev = rows[("Senior Secured Net Leverage Ratio", date(2026, 6, 30))]
    assert lev[4].value == 3.5 and lev[8].value == 0.08                 # level from the schedule, actual from the 10-Q
    assert lev[10].value == '=IF(I{0}="","",G{0}-I{0})'.format(lev[0].row)     # max covenant: level - actual
    cov = rows[("Consolidated Interest Coverage Ratio", date(2026, 6, 30))]
    assert cov[10].value == '=IF(I{0}="","",I{0}-G{0})'.format(cov[0].row)     # min covenant: actual - level
    assert "Met" in cov[12].value and "Breached" in cov[12].value
    assert "DRAFT" in wb["Summary"]["A2"].value                            # not signed off: says so


def test_mec_relief_levels_switch_on_an_input():
    wb = build("MEC", today=date(2026, 11, 14), review=False)
    r = _rows(wb["Test Calendar"])[("Consolidated Total Leverage Ratio", date(2026, 6, 30))]
    assert r[4].value == 3.5 and r[5].value == 5.25                       # standard and relief levels side by side
    assert r[6].value.startswith('=IF(AND(F') and "Summary!" in r[6].value    # which applies: the relief input on Summary
    assert r[13].value is None                                              # no step-up during the relief period
