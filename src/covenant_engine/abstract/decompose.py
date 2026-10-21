"""Step 3 (term decomposition) with a CODE-DRIVEN loop. Method 1: the model only assembles library parts; code
checks every part, fetches any context the model asks for, and re-asks for the failing parts only.

  round 1   model: all components for the term + needs_context / search_for requests
  check     code:  shape (schema), quotes in the agreement, cap values/schedules, clause coverage vs code's own
                   enumeration, duplicates; "custom" parts go to review (never retried)
  fetch     code:  requested definitions / sections from the intake bundle, text search over the agreement,
                   plus defined terms the model mentioned that weren't in its context
  rounds 2-3 model: ONLY the failing / missing clauses, with the errors and the fetched context
  escalate  one round on a stronger model for anything still failing
  review    whatever is left, with the reason

Usage: python -m covenant_engine.abstract.decompose ROAD "Consolidated Adjusted EBITDA" --model haiku [--strip-context]
"""
import argparse
import json
import re
import time
from datetime import date

from ..ingest.exceptions import call_model, squash
from ..ingest.packet import NO_EXPAND
from ..core.decompose_term import CAP_TYPES, CATEGORIES, bundle_dir
from .clauses import coverage, enumerate_clauses, norm, segments_unclaused, unnumbered_spans

# ---- parts libraries by term family (Method 1: the model picks from these; "custom" goes to review) ----------------
FAMILIES = {
    "ebitda": [c for c in CATEGORIES if c != "custom"],
    "debt": ["borrowed_money", "notes_bonds", "capital_finance_leases", "synthetic_leases", "letters_of_credit_drawn",
             "letters_of_credit_undrawn", "bankers_acceptances_guaranties", "guarantees", "deferred_purchase_price",
             "earnouts", "conditional_sale", "others_debt_secured_by_own_assets", "hedge_obligations",
             "receivables_financing", "disqualified_equity", "off_balance_sheet", "convertible_notes",
             "secured_only_filter", "scope_entities", "excluded_debt", "less_cash",
             "interest_bearing_catch_all", "partnership_jv_debt", "purchase_money_debt", "sale_leaseback",
             "equity_redemption_obligations"],
    "cash": ["unrestricted_cash", "cash_equivalents", "permitted_investments", "excluded_restricted_cash",
             "excluded_foreign_cash", "excluded_cash_subject_to_liens", "excluded_earmarked_proceeds",
             "location_or_account_requirement", "minimum_retained_amount", "cap_on_total"],
    "interest": ["interest_expense_gaap", "cash_interest_expense", "capitalized_interest", "capital_lease_interest",
                 "letter_of_credit_fees", "commitment_fees", "hedge_net_costs", "oid_pik_interest",
                 "receivables_financing_costs", "preferred_dividends", "interest_income", "excluded_financing_cost_amortization",
                 "excluded_hedge_mark_to_market", "excluded_non_cash_interest", "prior_period_cash_payments",
                 "excluded_hedge_breakage_and_costs", "excluded_other"],
    "ratio": ["numerator_term", "numerator_less_term", "denominator_term", "denominator_less_term",
              "numerator_amount", "numerator_less_amount", "denominator_amount", "denominator_less_amount",
              "measurement_period", "scope_entities"],
    "capex": ["ppe_additions", "capital_lease_additions", "capitalized_software", "excluded_insurance_funded",
              "excluded_acquisitions", "excluded_reinvested_proceeds", "excluded_equity_funded", "carry_forward"],
}
COMMON = ["term_reference", "calculation_rule", "custom"]   # calculation_rule: an instruction, not an amount (floor at zero, annualization)
ALL_CATEGORIES = sorted({c for v in FAMILIES.values() for c in v} | set(COMMON))
FAMILY_RULES = {
    "ebitda": "- starts_from: ONLY the name of the base amount, e.g. \"Consolidated Net Income\" (no other words).",
    "debt": ("- Debt: one component per included item (sign \"+\") and per exclusion (sign \"-\", category excluded_debt). "
             "If the term only counts SECURED debt or a subset of entities, add a secured_only_filter / scope_entities component. "
             "If cash is netted, add less_cash (or a term_reference to the cash term). starts_from: \"\" unless the term starts from another defined amount."),
    "cash": ("- Cash netting: base items \"+\", exclusions \"-\", account/location rules as location_or_account_requirement, "
             "and the cap on the whole amount as ONE cap_on_total component with the cap fields (greater_of / lesser_of allowed). starts_from: \"\"."),
    "interest": ("- Interest: items added \"+\", items netted or deducted \"-\", items the text explicitly excludes as excluded_* with sign \"-\". "
                 "starts_from: \"\" unless the term starts from another defined amount."),
    "ratio": ("- Ratio: one component per TERM named in each side (a side like \"Funded Debt minus Qualifying Cash\" is two components with the same clause): numerator_term / denominator_term (term_reference-style: put the "
              "defined term in ref_term), numerator_less_term for amounts netted (e.g. cash), measurement_period for the period "
              "or date each side is measured (note), scope_entities if limited. An amount that is NOT a defined term (e.g. \"income tax "
              "expense\") uses the *_amount categories with ref_term \"\". starts_from: \"\"."),
    "capex": "- Capex: included items \"+\", carve-outs excluded_* \"-\", carry-forward rules as carry_forward. starts_from: \"\".",
}
FAMILY_OF = [("ratio", r"Ratio$"), ("ebitda", r"EBITDA"), ("interest", r"Interest (Expense|Charges)"),
             ("capex", r"Capital Expenditure"), ("cash", r"^(?!.*(Charges|Expenses?|Costs)$).*(Unrestricted Cash|Qualifying Cash|Cash Amount|Cash and Cash Equivalents|"
                     r"^Cash\b|Liquidity|Permitted Investments)"),
             ("debt", r"Debt|Indebtedness")]


REF_CATS = {"term_reference", "numerator_term", "numerator_less_term", "denominator_term", "denominator_less_term"}


def family_of(term):
    for fam, rx in FAMILY_OF:
        if re.search(rx, term):
            return fam
    return None


CAP_TYPES_V1 = ["pct_of_ebitda", "pct_of_total_assets", "pct_of_other", "fixed_amount", "per_transaction", "lifetime_amount",
                "greater_of", "lesser_of", "relative", "step_down", "other"]
CAP = {"type": "object", "additionalProperties": False,
       "required": ["type", "value", "from", "to", "during", "covenant", "pool", "basis", "quote"],
       "properties": {"type": {"type": "string", "enum": CAP_TYPES_V1},
                      "value": {"type": "string"},
                      "from": {"type": "string"}, "to": {"type": "string"},
                      "during": {"type": "string"},
                      "covenant": {"type": "string"},
                      "pool": {"type": "string"},
                      "basis": {"type": "string", "enum": ["after_capped_items", "before_capped_items", "not_stated", "n/a"]},
                      "quote": {"type": "string"}}}
SOURCES = ["income_statement", "balance_sheet", "cash_flow_statement", "footnote", "borrower_schedule", "agreement_fixed",
           "not_disclosed", "n/a"]
COMPONENT = {
    "type": "object", "additionalProperties": False,
    "required": ["clause", "category", "ref_term", "ref_clauses", "sign", "quote", "caps", "source", "needs_judgment", "note"],
    "properties": {
        "clause": {"type": "string"},
        "category": {"type": "string", "enum": ALL_CATEGORIES},
        "ref_term": {"type": "string"}, "ref_clauses": {"type": "string"},
        "sign": {"type": "string", "enum": ["+", "-", "+/-"]},
        "quote": {"type": "string"},
        "caps": {"type": "array", "items": CAP},
        "source": {"type": "string", "enum": SOURCES},
        "needs_judgment": {"type": "boolean"},
        "note": {"type": "string"}}}
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["starts_from", "components", "needs_context", "search_for"],
          "properties": {"starts_from": {"type": "string"},
                         "components": {"type": "array", "items": COMPONENT},
                         "needs_context": {"type": "array", "items": {"type": "string"}},
                         "search_for": {"type": "array", "items": {"type": "string"}}}}

def schema_for(family):
    import copy
    sc = copy.deepcopy(SCHEMA)
    sc["properties"]["components"]["items"]["properties"]["category"]["enum"] = FAMILIES[family] + COMMON
    return sc


SYSTEM = ("You decompose defined terms in commercial credit agreements into components from a fixed parts library. "
          "Work only from the text given. Every quote must be copied exactly, character for character, from the text. "
          "Never fill a gap from general knowledge: if you need a definition or section you were not given, ask for it.")

RULES = """Rules:
- One component per lettered/numbered clause that adds, deducts or includes/excludes an amount (e.g. (a)(xii), (b), (c)(ii)). Never merge clauses; split only into separately capped parts (e.g. (xv)(x), (xv)(y)). Skip "[Reserved]" clauses.
- clause: the clause reference as written, e.g. "(a)(xii)". category: closest part from the enum, or "custom" if none fits without bending its meaning.
- sign: "+" adds, "-" deducts, "+/-" when the same clause adds expenses and deducts the matching income.
- quote: the opening words of that clause (8-30 words), exact copy.
- caps: a LIST with one entry per limit on this part ([] if none). A part can have several: limbs that change over time, a value that applies only to one covenant, a per-item limit plus a shared pool.
  type: pct_of_ebitda (value "0.15"), fixed_amount (per test period, "2500000"), per_transaction, lifetime_amount, greater_of / lesser_of (value lists every limb separated by "|", e.g. "0.10|7500000"; a limb meaning "the amount above $20M" is "excess_over:20000000"), other.
  from / to: when the cap applies only for a time window: YYYY-MM-DD if the text gives a date, or "<n> months after <defined date>" / "<n> quarters after <defined date>" if it counts from an event (e.g. to "24 months after Closing Date"); "" = open. Never compute dates. during: the period it applies in when that period ends on an event (e.g. "Covenant Adjustment Period"), else "".
  covenant: "all"; or, when the text gives that value only for some covenants, the section(s) (e.g. "Section 6.13") or the ratio name(s) the text uses, separated by ";".
  pool: ONLY when one limit is shared by several DIFFERENT clauses (e.g. add-backs (v), (vii) and (xii) together capped at 15%): the same short name on every one of them, same type and value. Never for caps on a single clause. Else "".
  An amount that only counts above a threshold ("in excess of $10,000,000") is a cap of type other with value "excess_over:10000000".
  basis: for % caps, measured before or after the capped items. quote: exact words stating that cap. Approval requirements go in note.
- source: where the number for this part would come from for a public borrower: income_statement, balance_sheet, cash_flow_statement, footnote (notes to the financials or MD&A), borrower_schedule (only the borrower knows it: cost savings, deal costs, pro forma figures), agreement_fixed (a number fixed in the agreement), not_disclosed. Use "n/a" for parts that are not amounts: term references, ratio sides that name a defined term, qualifiers (scope, secured-only, account rules, measurement period), calculation rules and cap_on_total.
- needs_judgment: true if the clause depends on judgment words ("reasonably", "factually supportable", "good faith", "acceptable to the Agent").
- ref_term: when a clause adds or deducts another defined term (category term_reference, or the ratio categories), its exact name (e.g. "Qualifying Cash"); else "". ref_clauses: if only some clauses of that term count, list them as written, e.g. "(a),(b),(c),(d)"; "" = the whole term.
{family_rules}
- Labels (use exactly these conventions):
  * A list of eligible items inside a definition (e.g. the investments listed in "Permitted Investments" or "Cash Equivalents"): every item takes the category named after that definition (permitted_investments / cash_equivalents).
  * An amount the text excludes or deducts: use the library category for the item itself with sign "-" when one exists (interest income -> interest_income, OID / PIK -> oid_pik_interest, receivables facility fees -> receivables_financing_costs); otherwise the most specific excluded_* category; excluded_other only when none fits.
  * A definition with NO lettered or numbered clauses: one component per amount it counts and one per condition, exclusion or proviso it applies; label them "base", "condition 1", "proviso", ...
  * A rule about how to measure or value an amount ("deemed to be the lesser of ...", "calculated without giving effect to ...", "valued at ...") is a calculation_rule part, not a cap.
  * A limit equal to another amount is a cap of type relative: value = the clause as written ("(a)(v)"), the defined
    term, or a short description ("earnings the proceeds replace"); never a number.
  * An add-back that shrinks each period ("reduced by 25% of the original amount for each subsequent Test Period") is a
    cap of type step_down with value the fraction lost per period ("0.25").
  * A cap on only some clauses of a referenced term (e.g. "Transaction Charges pursuant to clause (ii) shall not exceed
    $10,000,000"): split the part by ref_clauses, one capped part for those clauses and one uncapped for the rest.
  * Percentage limits: of EBITDA -> pct_of_ebitda; of total assets -> pct_of_total_assets; of anything else ->
    pct_of_other (say of what in note). Value as a fraction ("0.005").
  * Labels: text before the first lettered clause is "base" / "lead-in", not "(a)". ref_term only for a defined term the
    clause's own words name; related terms go in note.
  * Cap types: a dollar limit per test period -> fixed_amount; any other dollar limit (per certificate, per bank, per item, outstanding at any time) -> other. Every cap value is a bare number ("0.005", "5000000") or limbs "a|b"; descriptions go in note.
- needs_context: names of definitions or "Section x.y" you need but were not given. search_for: short phrases to search the agreement for when you can't name what you need. Leave both empty if you have what you need."""

FIRST = """Decompose the defined term “{term}” into its components.
{used_by}
{rules}

DEFINITION OF “{term}”:
<<<
{definition}
>>>

REFERENCED DEFINITIONS (context):
<<<
{context}
>>>

CALCULATION RULES THAT APPLY TO EVERY RATIO (context; not components of the term — mention them in a note only if a clause depends on them):
<<<
{rules_text}
>>>"""

REPAIR = """You are fixing part of a decomposition of “{term}”. Return ONLY the components for the clauses listed below
(corrected or newly added), using the same rules. Do not return clauses that are not listed.

{rules}

CLAUSES TO RETURN AND WHAT WAS WRONG:
{problems}

{extra}DEFINITION OF “{term}”:
<<<
{definition}
>>>"""


class Bundle:
    def __init__(self, borrower):
        d = self.dir = bundle_dir(borrower)
        self.defs = json.loads((d / "definitions.json").read_text(encoding="utf-8"))
        self.sections = json.loads((d / "sections.json").read_text(encoding="utf-8"))
        self.blocks = [json.loads(l) for l in (d / "blocks.jsonl").read_text(encoding="utf-8").splitlines()]
        self.live = (d / "text_live.txt").read_text(encoding="utf-8")
        self.live_sq = squash(self.live)

    def definition(self, name):
        d = self.defs["defined"].get(name)
        return d["text"] if d else None

    def section(self, ref):
        sid = re.sub(r"^(Section|SECTION)\s+", "", ref.strip()).split("(")[0]
        s = next((s for s in self.sections if s["id"] == sid or s.get("printed_id") == sid), None)
        if not s:
            return None
        return "\n".join(self.blocks[k]["text_live"] for k in range(s["start"], s["end"]) if self.blocks[k]["text_live"])[:8000]

    def search(self, phrase, limit=4):
        hits = [b["text_live"] for b in self.blocks if phrase.lower() in b["text_live"].lower()]
        return [h[:1500] for h in hits[:limit]]


def calc_rules(bundle):
    """Global calculation rules (pro forma, ratio calculations, accounting terms) from the packet."""
    p = json.loads((bundle.dir / "packet.json").read_text(encoding="utf-8"))
    return "\n\n".join(f"[Section {c['id']} {c.get('title', '')}]\n{c['text']}" for c in p if c["kind"] == "calc_rules")


def pointer_text(bundle, term):
    """A definition that only points elsewhere ('“Total Leverage Ratio” has the meaning assigned to such term in
    Section 6.11(a)') is decomposed from the cited clause, not from the pointer (QUAD, found by the GPT check)."""
    d = bundle.defs["defined"][term]["text"]
    m = re.search(r"has the meaning (?:assigned|given|set forth|specified)[^.]{0,60}?Section\s+(\d+\.\d+[\d.]*)((?:\([a-z0-9]+\))*)", d)
    if not m or len(d) > 400:
        return None
    sid, cl = m.group(1), re.findall(r"\(([a-z0-9]+)\)", m.group(2))
    s = next((x for x in bundle.sections if x["id"] == sid or x.get("printed_id") == sid), None)
    if not s:
        return None
    full = "\n".join(bundle.blocks[k]["text_live"] for k in range(s["start"], s["end"]) if bundle.blocks[k]["text_live"])
    if cl:
        a = re.search(r"(?:^|\n)\(" + re.escape(cl[0]) + r"\)", full)
        if not a:
            return None
        z = re.search(r"\n\([a-z0-9]+\)", full[a.end():])
        full = full[a.start():a.end() + (z.start() if z else len(full))].strip()
        full = re.sub(r"^\([a-z0-9]+\)\s+[A-Z][^.]{0,80}\.\s+", "", full)   # drop "(a) Maximum Total Leverage Ratio. "
    return full[:20000]


def context_for(bundle, term, max_chars=60000, strip=False):
    d = bundle.defs["defined"][term]
    pt = pointer_text(bundle, term)
    if pt:                                   # decompose the cited clause; its defined terms are the context
        d = dict(d, text=pt, refs=[t for t in bundle.defs["defined"] if len(t) > 6 and t != term
                                   and re.search(r"(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])", pt)])
    if strip:
        return d["text"], "", []
    ctx, names, used = [], [], 0
    for r in d.get("refs", []):
        if r in NO_EXPAND or r not in bundle.defs["defined"]:
            continue
        t = bundle.defs["defined"][r]["text"][:20000]          # long definitions were cut mid-clause at 6000
        if used + len(t) > max_chars:
            break
        ctx.append(t)
        names.append(r)
        used += len(t)
    return d["text"], "\n\n".join(ctx), names


QUALIFIERS = {"scope_entities", "secured_only_filter", "location_or_account_requirement", "calculation_rule",
              "measurement_period"}
VARIANT = re.compile(r"(solely|only) (with respect to|for (the )?purposes of)[^.;]{0,80}?Sections?\s+(\d+\.\d+[\d.]*(\([a-z]+\))?)", re.I)


LIST_TERMS = {"permitted investments": "permitted_investments", "cash equivalents": "cash_equivalents",
              "cash equivalent": "cash_equivalents"}


def relabel_list_items(term, comps):
    """Code, not model: inside a list-of-eligible-items definition every item takes the definition's own category."""
    cat = LIST_TERMS.get(term.strip().lower())
    if cat:
        for c in comps.values():
            if c["category"] in ("permitted_investments", "cash_equivalents"):
                c["category"] = cat
    return comps


def dedupe_relabelled(comps, code_ids):
    """Code: two parts quoting the same words with the same sign are one part under two labels (WEST: 'excluding (iv)'
    and 'excl(iv)' after the clause list changed) - keeping both would subtract it twice. Keep the one whose label is a
    code clause id; return the dropped labels for the trace."""
    groups, dropped = {}, []
    for k, c in comps.items():
        q = re.sub(r"^\s*(?:\([a-z0-9]{1,6}\)\s*)+", "", c["quote"])          # "(iv) commissions" = "commissions"
        groups.setdefault((c["category"], c["sign"], squash(q)[:60]), []).append(k)   # same role only
    out = dict(comps)
    for keys in groups.values():
        if len(keys) < 2:
            continue
        coded = [k for k in keys if norm(comps[k]["clause"]) in code_ids]
        if not coded or len(coded) == len(keys):
            continue
        for k in keys:
            if k not in coded:
                keep = out[coded[0]]
                if not keep["ref_term"] and comps[k]["ref_term"]:      # keep the reference the duplicate carried
                    keep["ref_term"] = comps[k]["ref_term"]
                dropped.append(comps[k]["clause"])
                out.pop(k)
    return out, dropped


def dedupe(comps):
    """A part the model re-sends under a different clause label (MEC: blank -> 'main (liens)') is the same part: same
    category, same referenced term, same opening words. Keep the latest version."""
    seen, out = {}, {}
    for k, c in comps.items():
        sig = (c["category"], c["ref_term"], squash(c["quote"])[:60])
        if sig in seen:
            out.pop(seen[sig])
        seen[sig] = k
        out[k] = c
    return out


POINTER = re.compile(r"has the meaning (assigned|given|set forth|specified)", re.I)
LEAD = (r"^(?:\([a-z0-9]{1,6}\)\s*)*(?:all\s+|any\s+|the\s+)?(?:aggregate\s+)?(?:outstanding\s+)?(?:principal\s+)?"
        r"(?:amount\s+of\s+)?(?:(?:all|any)\s+)?(?:other\s+)?")


def amount_term(quote, defined, self_name, family=None):
    """The known-family defined term a clause IS (it opens with it: 'Indebtedness for borrowed money', 'the aggregate
    principal amount of Indebtedness', 'all Permitted Investments'), else None. Descriptive mentions don't count."""
    for t in sorted(defined, key=len, reverse=True):
        if t != self_name and family_of(t) in ("debt", "cash", "interest", "capex") and (family is None or family_of(t) == family) and \
                re.match(LEAD + re.escape(t) + r"(?![A-Za-z])", quote.strip(), re.I):
            return t
    return None


# ---- source: structural parts are n/a; gross contradictions are rejected; SOURCE_OK is for plausibility scoring
NA_CATS = {"term_reference", "numerator_term", "numerator_less_term", "denominator_term", "denominator_less_term",
           "measurement_period", "scope_entities", "secured_only_filter", "location_or_account_requirement",
           "calculation_rule", "cap_on_total"}
FLOW = {"income_statement", "cash_flow_statement"}
NOT_BALANCE = {"interest", "taxes", "depreciation_amortization", "interest_expense_gaap", "interest_income",
               "cash_interest_expense", "non_cash_compensation", "ppe_additions"}
NOT_FLOW = {"unrestricted_cash", "cash_equivalents", "permitted_investments", "borrowed_money", "notes_bonds",
            "letters_of_credit_undrawn", "guarantees"}
IS, BS, CF, FN, BSCH, FIX, ND = ("income_statement", "balance_sheet", "cash_flow_statement", "footnote",
                                 "borrower_schedule", "agreement_fixed", "not_disclosed")
SOURCE_OK = {
    **{c: {IS, FN, CF} for c in ["interest", "taxes", "depreciation_amortization", "other_non_cash_charges", "non_cash_compensation",
                                 "equity_plan_costs", "pension_amortization", "fx_losses", "fx_gains", "impairment_writedowns",
                                 "debt_extinguishment", "hedging_mark_to_market", "asset_sale_gains_losses", "non_cash_gains",
                                 "discontinued_operations", "nci_income", "nci_losses", "interest_expense_gaap", "interest_income",
                                 "oid_pik_interest", "capitalized_interest", "capital_lease_interest", "hedge_net_costs"]},
    **{c: {FN, BSCH, IS} for c in ["restructuring_business_optimization", "transaction_costs", "litigation_regulatory",
                                   "transition_closure_costs", "public_company_costs", "sponsor_management_fees",
                                   "extraordinary_unusual_items", "earnouts_contingent_consideration", "insurance_recoveries",
                                   "third_party_reimbursed_expenses", "receivables_facility_losses",
                                   "cash_receipts_from_prior_non_cash_gains", "letter_of_credit_fees", "commitment_fees",
                                   "receivables_financing_costs", "prior_period_cash_payments"]},
    **{c: {BSCH, ND, FN} for c in ["cost_savings_implementation", "projected_savings", "quality_of_earnings_adjustments",
                                   "acquired_ebitda", "disposed_ebitda", "equity_cure"]},
    **{c: {BS, FN} for c in ["borrowed_money", "notes_bonds", "capital_finance_leases", "convertible_notes", "unrestricted_cash",
                             "cash_equivalents", "permitted_investments", "less_cash"]},
    **{c: {FN, BS, BSCH, ND} for c in ["letters_of_credit_drawn", "letters_of_credit_undrawn", "bankers_acceptances_guaranties",
                                       "guarantees", "off_balance_sheet", "synthetic_leases", "conditional_sale",
                                       "others_debt_secured_by_own_assets", "hedge_obligations", "receivables_financing",
                                       "deferred_purchase_price", "earnouts", "purchase_money_debt", "sale_leaseback",
                                       "partnership_jv_debt", "interest_bearing_catch_all", "equity_redemption_obligations",
                                       "disqualified_equity"]},
    "cash_interest_expense": {CF, FN}, "preferred_dividends": {CF, FN}, "ppe_additions": {CF},
    "capital_lease_additions": {FN, CF}, "carry_forward": {BSCH, FN, FIX},
}


def source_problems(c):
    cat, src = c["category"], c["source"]
    if cat in NA_CATS and src != "n/a":
        return [f"source must be 'n/a' for a {cat} part (it is not an amount)"]
    if cat not in NA_CATS and not cat.startswith(("numerator_", "denominator_")) and src == "n/a" and cat != "custom":
        return ["this part is an amount: give its source (where the number would come from)"]
    if cat in NOT_BALANCE and src == "balance_sheet":
        return [f"a {cat} amount is a flow for the period; it can't come from the balance sheet"]
    if cat in NOT_FLOW and src in FLOW:
        return [f"a {cat} amount is a balance at a date; it can't come from the {src}"]
    return []


def ckey(c):
    """A clause can hold several parts (a ratio side 'Funded Debt minus Qualifying Cash'), so key by clause+part."""
    return (norm(c["clause"]), c["category"], c["ref_term"])


def _overlaps(dsq, qsq, span):
    """Does the quote overlap the clause's own text? Quotes may start before the clause (a ratio quoted from 'means')
    or run past a short clause, but must touch it. Sibling clauses can share long openings ('to the extent included in
    such consolidated interest expense ...'), so ANY occurrence of the quote that touches the clause counts."""
    if qsq not in dsq:
        return True                    # not inside this definition (e.g. from a referenced one): not this check's job
    i = dsq.find(qsq)
    while i >= 0:
        if i < span[1] and i + len(qsq) > span[0]:
            return True
        i = dsq.find(qsq, i + 1)
    return False


def _limbs(v):
    out = []
    for x in str(v).split("|"):
        x = x.strip().replace(",", "").replace("$", "")
        m = re.fullmatch(r"(excess_over:)?(-?\d+(\.\d+)?)", x)
        out.append(((m.group(1) or "") + str(round(float(m.group(2)), 9))) if m else x.lower())
    return tuple(sorted(out))


REL = re.compile(r"(\d+) (months|quarters|years|days) after (?:the )?(.+)", re.I)


def _sec(s):
    m = re.search(r"\d+\.\d+[\d.]*(\([a-z]+\))?", s or "")
    return m.group(0) if m else "all"


def _secs(s):
    found = re.findall(r"\d+\.\d+[\d.]*(?:\([a-z]+\))?", s or "")
    return tuple(sorted(set(found))) or ("all",)


def _when(v):
    m = REL.fullmatch((v or "").strip())
    return f"{m.group(1)} {m.group(2).lower()} after {m.group(3).strip().lower()}" if m else (v or "").strip()


def cap_sig(cap):
    return (cap["type"], _limbs(cap["value"]), _when(cap["from"]), _when(cap["to"]), cap["during"].strip().lower(),
            _secs(cap["covenant"]))


def cap_signatures(comps):
    """Distinct cap SETS on the cash term's cap_on_total parts. The same cap set anchored to several clauses (the cap
    sentence and its limbs (i), (ii)) is one."""
    return {tuple(sorted(cap_sig(c) for c in comp["caps"])) for comp in comps if comp["category"] == "cap_on_total"}


def check(comps, starts_from, leaves, bundle, family=None, definition="", term=""):
    """Code checks. Returns {clause_key: [problems]}, missing leaves, review items."""
    probs, review, seen = {}, [], {}
    # each code-enumerated clause's own text (to the next clause marker), squashed
    leaf_span, dsq = {}, squash(definition)
    for i, (cid, pos, _) in enumerate(leaves):        # clause spans in squashed coordinates
        end = leaves[i + 1][1] if i + 1 < len(leaves) else len(definition)
        leaf_span[cid] = (len(squash(definition[:pos])), len(squash(definition[:end])))
    if family == "ratio":       # a ratio part whose clause IS a defined term ("minus (iii) Discretionary Repurchase
        for c in comps:         # Obligation Payments paid in cash") must reference it, whatever its family
            if c["category"] in QUALIFIERS or c["ref_term"]:
                continue
            q = re.sub(r"^\s*(?:(?:minus|plus|less|and|to|over|divided by)\s+|\([a-z0-9]{1,6}\)\s*|\d+(?:\.\d+)?%\s+of\s+)*", "",
                       c["quote"], flags=re.I)
            t = next((t for t in sorted(bundle.defs["defined"], key=len, reverse=True) if t != term and len(t) > 6
                      and re.match(LEAD + re.escape(t) + r"(?![A-Za-z])", q.strip())), None)
            if t:
                probs.setdefault(norm(c["clause"]), []).append(
                    f"this clause is the defined term {t!r}: use the *_term category and set ref_term to it")
    if family == "ratio":       # every calculation term the ratio names must be referenced by some component
        named = [t for t in bundle.defs["defined"].get(term, {}).get("refs", []) if family_of(t) and t != term]
        if pointer_text(bundle, term):     # pointer definition: the terms named in the cited clause
            txt, named = definition, []
            for t in sorted(bundle.defs["defined"], key=len, reverse=True):   # longest first: "Indebtedness" inside
                rx = r"(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])"           # "Consolidated Total Indebtedness" is
                if t != term and re.search(rx, txt):                             # not a separate name
                    if family_of(t):
                        named.append(t)
                    txt = re.sub(rx, " ", txt)
        got = {c["ref_term"] for c in comps}
        for t in named:
            if t not in got:
                probs.setdefault("ratio", []).append(f"the definition names {t!r} but no component references it (ref_term)")
    # a definition with a covenant-specific variant ("solely with respect to ... Section 6.13"): model can't resolve which
    # applies without knowing the covenant -> always to review, naming the section
    all_caps = [cap for c in comps for cap in c["caps"]]
    for m in VARIANT.finditer(definition):
        sec = m.group(4)
        if all_caps and not any(_sec(cap["covenant"]).startswith(sec.split("(")[0]) for cap in all_caps):
            probs.setdefault("variant", []).append(f"the definition gives a value {m.group(0)[:100]!r}: add a cap with covenant "
                                                   f"'Section {sec}' for that value (keep the general one with covenant 'all')")
        elif not all_caps:
            review.append({"clause": "variant", "why": f"covenant-specific wording for Section {sec} but no caps: confirm "
                                                       f"nothing differs for that covenant ({m.group(0)[:120]!r})"})
    pools, pool_parts = {}, {}
    for c in comps:
        for cap in c["caps"]:
            if cap["pool"]:
                scope = (cap["from"], cap["to"], cap["during"], _sec(cap["covenant"]))
                pools.setdefault((cap["pool"], scope), set()).add((cap["type"], _limbs(cap["value"])))
                pool_parts.setdefault(cap["pool"], set()).add(norm(c["clause"]))
    for (name, _), sigs in pools.items():
        if len(sigs) > 1:
            probs.setdefault("pool", []).append(f"pool {name!r} is given different limits {sorted(sigs)}; a shared pool has one limit")
    for name, parts in pool_parts.items():
        if len(parts) < 2:
            probs.setdefault("pool", []).append(f"pool {name!r} is used by one clause only; pool is for a limit shared by "
                                                f"several clauses: set pool to ''")
    if family == "ratio":
        for c in comps:
            if c["sign"] == "+/-":
                probs.setdefault(norm(c["clause"]), []).append("a ratio part can't be '+/-'; say which side and sign")
    if family in ("debt", "cash", "interest", "capex"):
        amounts = [c for c in comps if c["category"] not in QUALIFIERS]
        if not any(c["sign"] in ("+", "+/-") for c in amounts):
            probs.setdefault("base", []).append("no part adds an amount: give the base amount(s) the term counts (sign '+'), "
                                                "not only filters or exclusions")
        for cid, _, _ in leaves:
            mine = [c for c in comps if norm(c["clause"]) == cid or norm(c["clause"]).startswith(cid + "(")]
            if mine and all(c["category"] in QUALIFIERS - {"calculation_rule"} for c in mine):
                probs.setdefault(cid, []).append("this clause has only qualifier parts (scope/filter/rule); it states an "
                                                 "amount: add the amount part")
    # definitions with no clause markers: every operative segment (base, proviso, exclusion, condition, arithmetic step)
    # must be touched by some part's quote. Pointer definitions ("has the meaning assigned in Section x") are skipped.
    if not leaves and family in ("debt", "cash", "interest", "capex", "ratio") and not POINTER.search(definition):
        for lab, a, b_, snip in segments_unclaused(definition):
            span = (len(squash(definition[:a])), len(squash(definition[:b_])))
            if not any(squash(c["quote"]) in dsq and _overlaps(dsq, squash(c["quote"]), span) for c in comps):
                probs.setdefault(lab, []).append(f"the text {snip.strip()[:120]!r} is not covered by any part: add the part "
                                                 f"for this amount, condition or exclusion (quote its opening words)")
    # #25 text no clause marker owns (provisos, 'For purposes of' / closing sentences): some part quote or cap quote must
    # touch it, else the loop asks for it (a limit -> cap on its part; a condition / rule -> a part)
    if leaves:
        qs = [c["quote"] for c in comps] + [cap["quote"] for c in comps for cap in c["caps"]]
        for lab, a, b_, snip in unnumbered_spans(definition, leaves):
            span = (len(squash(definition[:a])), len(squash(definition[:b_])))
            if not any(q and squash(q) in dsq and _overlaps(dsq, squash(q), span) for q in qs):
                probs.setdefault(lab, []).append(
                    f"the text {snip.strip()[:140]!r} is not covered by any part or cap: if it limits an amount, add it as a "
                    f"cap on that part (cap quote = its words); if it is a condition, exclusion or calculation rule, add a part "
                    f"for it (quote its opening words)")
    # reference check: a clause that IS another known-family term must reference it
    if family in ("debt", "cash", "interest", "capex"):
        for c in comps:
            if c["category"] in QUALIFIERS or c["category"].startswith("excluded") or c["category"] == "custom":
                continue
            t = amount_term(c["quote"], bundle.defs["defined"], term, family)
            if t and c["ref_term"] != t:
                probs.setdefault(norm(c["clause"]), []).append(f"this clause is the defined term {t!r}: set ref_term to it "
                                                               f"(and ref_clauses if only some of its clauses count)")
    # a part that takes only some clauses of a referenced term (ref_clauses): together, the parts referencing that term
    # must take all of its top-level clauses, or the rest is silently dropped (QUAD Transaction Charges (ii), 2026-10-13)
    by_ref = {}
    for c in comps:
        if c["ref_term"] and c["ref_term"] in bundle.defs["defined"]:
            by_ref.setdefault(c["ref_term"], []).append(c)
    for c in comps:            # a cap quoted from the referenced term's own definition that names "clause (ii)" limits
        rt = c["ref_term"]     # only that clause: the capped part takes just it (ref_clauses), the rest goes in another part
        if not rt or rt not in bundle.defs["defined"]:
            continue
        rdef = squash(bundle.defs["defined"][rt]["text"])
        for cap in c["caps"]:
            if cap["quote"] and squash(cap["quote"])[:60] in rdef:
                named = re.findall(r"clauses?\s+\(([a-z0-9]+)\)", cap["quote"]) or \
                        re.findall(r"clauses?\s+\(([a-z0-9]+)\)", bundle.defs["defined"][rt]["text"][
                            max(0, bundle.defs["defined"][rt]["text"].find(cap["quote"][:40]) - 200):][:400])
                want = {f"({x})" for x in named}
                have = set(re.findall(r"\([a-z0-9]+\)", c.get("ref_clauses", "").replace(" ", "")))
                if want and have != want:
                    probs.setdefault(norm(c["clause"]), []).append(
                        f"the cap {cap['quote'][:60]!r} applies only to {rt} clauses {sorted(want)}: give this capped part "
                        f"ref_clauses {','.join(sorted(want))} and add an uncapped part for the other clauses")
    for rt, parts in by_ref.items():
        if not any(p_.get("ref_clauses") for p_ in parts) or any(not p_.get("ref_clauses") for p_ in parts):
            continue
        rdef = squash(bundle.defs["defined"][rt]["text"])        # only a split made because of a cap inside rt's own
        if not any(cap["quote"] and squash(cap["quote"])[:60] in rdef for p_ in parts for cap in p_["caps"]):
            continue                                              # definition (a partial reference by design is fine)
        top = {c_.split(")")[0] + ")" for c_, _, _ in enumerate_clauses(bundle.defs["defined"][rt]["text"])}
        got = {x.strip().replace(" ", "") for p_ in parts for x in re.findall(r"\([a-z0-9]+\)", p_["ref_clauses"])}
        miss = sorted(top - got)
        if miss and len(top) > 1:
            probs.setdefault(norm(parts[0]["clause"]), []).append(
                f"the parts referencing {rt!r} take only clauses {sorted(got)}; its clauses {miss} are not taken by any "
                f"part: add a part for them (with its own caps), or a note if the definition excludes them")
    if family == "cash" and len(cap_signatures(comps)) > 1:
        probs.setdefault("cap_on_total", []).append("cap_on_total parts state different caps; give the full list of caps "
                                                   "(with from/to, during, covenant) on each, or on one part")
    for c in comps:
        k = norm(c["clause"])
        p = []
        if ckey(c) in seen:
            p.append("duplicate part (same clause, category and ref_term)")
        seen[ckey(c)] = c
        if squash(c["quote"])[:80] not in bundle.live_sq:
            p.append("quote not found verbatim in the agreement; copy the opening words exactly")
        elif (kk := k if k in leaf_span else max((x for x in leaf_span if k.startswith(x) and k != x), key=len, default=None)) \
                and not _overlaps(dsq, squash(c["quote"]), leaf_span[kk]):
            p.append(f"quote is not from the text of clause {kk}; copy the opening words of THAT clause, or label the "
                     f"part by where its text is (e.g. 'base' / 'lead-in' for text before the first clause)")
        # spec v1.1: a part references only a defined term its own clause names (plural allowed)
        if c["ref_term"] and c["category"] not in ("measurement_period",):
            rx = r"(?<![A-Za-z])" + re.escape(c["ref_term"]) + r"(?:s|es)?(?![A-Za-z])"
            own = definition[[pp for cc, pp, _ in leaves if cc == kk][0]:] if kk in leaf_span else definition
            if kk in leaf_span:
                nxt = [pp for cc, pp, _ in leaves if pp > [q for cc, q, _ in leaves if cc == kk][0]]
                own = own[:nxt[0] - [q for cc, q, _ in leaves if cc == kk][0]] if nxt else own
            if not re.search(rx, own):
                p.append(f"ref_term {c['ref_term']!r} is not named in {'clause ' + kk if kk in leaf_span else 'the definition'}; "
                         f"reference only a term the clause itself names (put related terms in note)")
        for cap in c["caps"]:
            if cap["type"] == "relative":                 # a clause, a defined term, or a short description; never a number
                v = cap["value"].strip()
                if not v or re.fullmatch(r"[\d.,$%]+", v):
                    p.append(f"relative cap value {v!r} must name what the limit equals: a clause of this definition "
                             f"(e.g. '(a)(v)'), a defined term, or a short description ('earnings the proceeds replace')")
            if cap["type"] == "pct_of_other" and not c["note"].strip():
                p.append("pct_of_other cap: say in note what the percentage is of")
        p += source_problems(c)
        for cap in c["caps"]:
            limbs = [x.strip() for x in cap["value"].split("|") if x.strip()]
            nums = []
            for x in limbs:
                m = re.fullmatch(r"(excess_over:)?\$?([\d,]+(\.\d+)?)", x)
                nums.append(float(m.group(2).replace(",", "")) if m else None)
            if cap["type"] == "relative":
                pass                                       # checked above: names a clause or defined term
            elif cap["type"] in ("greater_of", "lesser_of"):
                if len(limbs) < 2:
                    p.append(f"{cap['type']} cap needs every limb in value, separated by '|'")
            elif cap["type"] == "other" and (len(limbs) != 1 or nums[0] is None):
                p.append(f"cap value {cap['value']!r} must be a bare number (put descriptions in note)")
            elif cap["type"] != "other":
                if len(limbs) != 1 or nums[0] is None:
                    p.append(f"{cap['type']} cap value {cap['value']!r} must be one number")
                elif (cap["type"].startswith("pct_of") or cap["type"] == "step_down") and not 0 < nums[0] <= 1:
                    p.append(f"{cap['type']} value {cap['value']} must be a fraction between 0 and 1")
            for side in ("from", "to"):
                v = cap[side].strip()
                if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) and not REL.fullmatch(v):
                    p.append(f"cap {side} {v!r}: use YYYY-MM-DD, '<n> months after <defined date>' or ''")
                m = REL.fullmatch(v)
                if m and m.group(3) not in bundle.defs["defined"]:
                    p.append(f"cap {side} anchor {m.group(3)!r} is not a defined term; use its exact name")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cap["from"]) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cap["to"]) and cap["from"] > cap["to"]:
                p.append(f"cap from {cap['from']} is after to {cap['to']}")
            if cap["quote"] and squash(cap["quote"])[:80] not in bundle.live_sq:
                p.append("cap quote not found verbatim in the agreement")
        if c["category"] in REF_CATS and c["ref_term"] not in bundle.defs["defined"]:
            p.append(f"ref_term {c['ref_term']!r} is not a defined term in this agreement; give its exact name")
        elif c["ref_term"] in bundle.defs["defined"] and c["ref_clauses"]:
            have = {x for x, _, _ in enumerate_clauses(bundle.defs["defined"][c["ref_term"]]["text"])}
            have |= {g for x in list(have) for g in re.findall(r"\([^)]+\)", x)}
            bad = [x for x in re.findall(r"\([^)]+\)", c["ref_clauses"]) if x not in have]
            if bad:
                p.append(f"ref_clauses {bad} not found in the definition of {c['ref_term']!r}")
        if c["category"] == "custom":
            review.append({"clause": c["clause"], "why": "no library part fits (custom)", "note": c["note"][:200]})
        if p:
            probs[k] = p
    missing, unknown = coverage(leaves, [c["clause"] for c in comps], starts_from)
    return probs, missing, unknown, review


def run(borrower, term, model="sonnet", escalate="opus", max_repairs=2, strip_context=False, family=None, used_by=()):
    family = family or family_of(term) or "ebitda"
    schema = schema_for(family)
    rules = RULES.replace("{family_rules}", FAMILY_RULES[family])
    b = Bundle(borrower)
    definition, context, ctx_names = context_for(b, term, strip=strip_context)
    leaves = enumerate_clauses(definition)
    leaf_text = {c: snip for c, _, snip in leaves}
    trace, comps, starts_from = [], {}, ""
    have = set(ctx_names) | {term}
    searched = set()

    def call(prompt, m):
        r = call_model(SYSTEM, prompt, schema, model=m, timeout=900)
        trace.append({"model": r.get("model"), "cost": r.get("cost_usd"), "seconds": r.get("seconds"), "ok": r["ok"]})
        return r

    r = call(FIRST.format(term=term, rules=rules, definition=definition, context=context,
                      used_by=("This term is used by: " + "; ".join(used_by) + ".\n") if used_by else "",
                      rules_text="" if strip_context else (calc_rules(b) or "(none found)")), model)
    if not r["ok"]:
        return {"error": r.get("error"), "trace": trace}
    out = r["output"]
    starts_from = out["starts_from"].strip().strip('"“”').strip()
    for c in out["components"]:
        comps.setdefault(ckey(c), c)
    comps = relabel_list_items(term, comps)
    requests = {"needs_context": out["needs_context"], "search_for": out["search_for"]}

    for rnd in range(1, max_repairs + 2):          # repair rounds, then one escalation round
        probs, missing, unknown, review = check(list(comps.values()), starts_from, leaves, b, family, definition, term)
        trace[-1].update({"problems": {k: v for k, v in probs.items()}, "missing": missing,
                          "requested": {k: v for k, v in requests.items() if v}})
        # fetch what was asked for, plus defined terms the model mentioned that it wasn't given
        extra = []
        for n in requests["needs_context"]:
            if n in have:
                continue
            t = b.section(n) if re.match(r"(Section|SECTION)\s+\d", n) else b.definition(n)
            extra.append(f"[{n}]\n{t}" if t else f"[{n}] not found in this agreement")
            have.add(n)
        for ph in requests["search_for"]:
            if ph.lower() in searched:
                continue
            searched.add(ph.lower())
            hits = b.search(ph)
            extra.append(f"[search: {ph}]\n" + ("\n---\n".join(hits) if hits else "no matches"))
        mentioned = {t for c in comps.values() for t in b.defs["defined"]
                     if t not in have and len(t) > 4 and re.search(r"(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])", c["note"] + " " + " ".join(cap["quote"] for cap in c["caps"]))}
        for t in sorted(mentioned)[:6]:
            extra.append(f"[{t}]\n{b.definition(t)[:4000]}")
            have.add(t)
        trace[-1]["context_fetched"] = len(extra)
        if not probs and not missing and not extra:
            break                                      # clean, and nothing new to show the model
        if rnd > max_repairs:                          # escalation round: only for real failures, never for context alone
            if not probs and not missing:
                break
            m = escalate
        else:
            m = model
        lines = [f"- {k}: {'; '.join(v)}" for k, v in probs.items()]
        lines += [f"- {k}: MISSING. Its text starts: {leaf_text[k][:160]!r}" for k in missing]
        if not lines:                                  # only context arrived: let the model revise anything it affects
            lines = ["- any clause the new context changes (return none if nothing changes)"]
        extra_txt = ("ADDITIONAL CONTEXT YOU ASKED FOR OR MENTIONED:\n<<<\n" + "\n\n".join(extra) + "\n>>>\n\n") if extra else ""
        r = call(REPAIR.format(term=term, rules=rules, problems="\n".join(lines), extra=extra_txt, definition=definition), m)
        if not r["ok"]:
            break
        ret = r["output"]["components"]
        # Only clauses code FLAGGED (problems / missing) are replaced wholesale. Anything else the model returns (e.g.
        # in a context-only round) updates the matching part or adds a new one, and never deletes others: MEC lost 5
        # of 6 parts sharing a blank clause label when one of them was returned.
        flagged = set(probs) | set(missing)
        redo = {norm(c["clause"]) for c in ret} & flagged
        comps = {k: v for k, v in comps.items() if k[0] not in redo}
        for c in ret:
            comps[ckey(c)] = c
        comps = relabel_list_items(term, dedupe(comps))
        comps, _dropped = dedupe_relabelled(comps, {c for c, _, _ in leaves}) if family != "ratio" else (comps, [])
        if _dropped:
            trace[-1]["duplicates_dropped"] = _dropped
        requests = {"needs_context": r["output"]["needs_context"], "search_for": r["output"]["search_for"]}

    probs, missing, unknown, review = check(list(comps.values()), starts_from, leaves, b, family, definition, term)
    review += [{"clause": k, "why": "; ".join(v)} for k, v in probs.items()]
    review += [{"clause": k, "why": "clause not returned by the model"} for k in missing]
    if family != "ratio" and (family == "ebitda" or starts_from) and starts_from not in b.defs["defined"]:
        review.append({"clause": "starts_from", "why": f"base amount {starts_from!r} is not a defined term"})
    refs = sorted({c["ref_term"] for c in comps.values() if c["category"] in REF_CATS and c["ref_term"]})
    review += [{"clause": "ref_term", "why": f"term_reference {t!r} is not a defined term"} for t in refs if t not in b.defs["defined"]]
    return {"term": term, "family": family, "ref_terms": [t for t in refs if t in b.defs["defined"]], "starts_from": starts_from, "components": list(comps.values()), "code_clauses": [c for c, _, _ in leaves],
            "unknown_clauses": unknown, "review": review, "trace": trace,
            "cost": round(sum(t["cost"] or 0 for t in trace), 4), "seconds": round(sum(t["seconds"] or 0 for t in trace), 1),
            "context_given": ctx_names}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("borrower")
    ap.add_argument("term")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--escalate", default="opus")
    ap.add_argument("--strip-context", action="store_true", help="test: give no referenced definitions")
    a = ap.parse_args()
    res = run(a.borrower, a.term, a.model, a.escalate, strip_context=a.strip_context)
    print(json.dumps({k: v for k, v in res.items() if k != "components"}, indent=1)[:4000])


if __name__ == "__main__":
    main()
