"""Clause enumeration and unnumbered-text rules. Each case is a pattern a real agreement broke (named in the test)."""
from covenant_engine.abstract.clauses import enumerate_clauses, unnumbered_spans


def ids(text):
    return [c for c, _, _ in enumerate_clauses(text)]


def test_flat_lettered_list():
    t = "“X” means the sum of (a) cash, plus (b) receivables, plus (c) inventory."
    assert ids(t) == ["(a)", "(b)", "(c)"]


def test_roman_items_under_a_letter():
    t = "“X” means (a) the sum of (i) interest, (ii) taxes and (iii) depreciation, minus (b) gains."
    assert ids(t) == ["(a)(i)", "(a)(ii)", "(a)(iii)", "(b)"]


def test_cross_reference_is_not_an_item():
    t = "“X” means (a) amounts described in clause (a)(vii) of the definition, plus (b) other amounts."
    assert ids(t) == ["(a)", "(b)"]


def test_condition_list_stays_with_its_item():
    # "money market funds that (i) comply ... (ii) are rated ..." is one item with conditions
    t = "“Cash Equivalents” means (a) deposits, (b) money market funds that (i) comply with Rule 2a-7 and (ii) are rated AAA."
    assert ids(t) == ["(a)", "(b)"]


def test_exclusion_list_keeps_the_parent_as_a_part():
    # TRS: "(c) ... Indebtedness ...; provided that ... shall not include (i) ... (ii) ..." = the amount + two exclusions
    t = ("“Indebtedness” means (a) borrowed money, (b) notes, (c) off-balance-sheet obligations; provided that such "
         "obligations shall not include (i) operating leases or (ii) earnouts, and (d) guarantees.")
    assert ids(t) == ["(a)", "(b)", "(c)", "(c)(i)", "(c)(ii)", "(d)"]


def test_definition_wide_list_after_the_last_letter():
    # WEST: "(d) ... but excluding, for the avoidance of doubt, (i) ... (ii) ..." applies to the whole definition
    t = ("“Consolidated Interest Expense” means (a) interest, plus (b) fees, plus (c) dividends on preferred stock, "
         "but excluding, for the avoidance of doubt, (i) amortization of financing costs and (ii) breakage costs.")
    assert ids(t) == ["(a)", "(b)", "(c)", "excl(i)", "excl(ii)"]


def test_less_than_is_not_a_subtraction_cue():
    # QUAD: "equal to or less than 3.00 to 1.00 ... provided, that (i) ..." must not become a definition-wide list
    t = ("“Amount” means (a) $200,000,000 when the ratio is equal to or less than 3.00 to 1.00, and (b) $100,000,000 "
         "otherwise, provided, that (i) prior incurrences remain permitted and (ii) clause (b) is reduced.")
    assert "excl(i)" not in ids(t)


def test_unnumbered_proviso_and_closing_sentence_are_listed():
    t = ("“Consolidated EBITDA” means (a) net income, plus (b) taxes; provided, that amounts under clause (b) shall not "
         "exceed $5,000,000. For purposes of this definition, pro forma effect shall be given to acquisitions.")
    labels = [lab.split("@")[0] for lab, _, _, _ in unnumbered_spans(t, enumerate_clauses(t))]
    assert "proviso" in labels and "sentence" in labels


def test_parenthetical_proviso_is_part_of_its_clause():
    t = "“Cash Equivalents” means (a) US government obligations (provided that the full faith of the US is pledged), and (b) deposits."
    assert not [s for s in unnumbered_spans(t, enumerate_clauses(t)) if s[0].startswith("proviso")]
