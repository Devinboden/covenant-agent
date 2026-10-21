"""Code-decided dates: first test date after the Closing Date and 'n full quarters after' resolution."""
from datetime import date

from covenant_engine.abstract.covenants import first_quarter_end, nth_quarter_end


def test_first_quarter_end_on_or_after():
    assert first_quarter_end(date(2023, 6, 28)) == date(2023, 6, 30)      # MEC: Closing Date through ... starts Jun 30 2023
    assert first_quarter_end(date(2023, 6, 30)) == date(2023, 6, 30)


def test_nth_full_quarter_after_closing():
    closing = date(2022, 8, 29)                                           # WEST
    assert nth_quarter_end(closing, 1, full=True) == date(2022, 12, 31)   # Q3 2022 is partial: first FULL quarter is Q4
    assert nth_quarter_end(closing, 6, full=True) == date(2024, 3, 31)
    assert nth_quarter_end(closing, 7, full=True) == date(2024, 6, 30)
    assert nth_quarter_end(closing, 1, full=False) == date(2022, 9, 30)
