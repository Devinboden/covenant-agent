"""Enumerate the clauses of a definition with code, so completeness doesn't depend on the model.

Lettered and roman-numbered items are found by SEQUENCE, not by pattern alone: a marker counts only if it continues
the run in progress ((a)->(b)->(c), (i)->(ii)->(iii)) and isn't part of a cross-reference ("clause (a)(vii)",
"Section 8.1(a) or (b)", "twelve (12) months"). "(i)" after "(h)" is a letter (CARS runs (a)..(u)); after "(a)" it
is a roman numeral. Uppercase and numeric markers are sub-items (cap schedules, provisos) and are not enumerated.

Returns leaf clause ids like ["(a)(i)", ..., "(a)(xviii)", "(b)", "(c)(i)", ...] with their start offsets.
"""
import re

MARK = re.compile(r"\(([a-z]{1,2}|[ivxl]{1,6})\)")
REF_BEFORE = re.compile(r"(clauses?|Sections?|paragraphs?|subclauses?|sub-clauses?|subsections?|items?|"
                        r"\d\.\d+[A-Za-z]?|\)|–|—|-|,|through|and|or)\s*$", re.I)
ROMANS = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii",
          "xviii", "xix", "xx", "xxi", "xxii", "xxiii", "xxiv", "xxv"]
LETTERS = [chr(c) for c in range(ord("a"), ord("z") + 1)]


def _is_reference(text, pos):
    before = text[max(0, pos - 25):pos]
    if REF_BEFORE.search(before):
        # "plus (a)" / "; and (b)" are list items, "clause (a)" / "8.1(a) or (b)" are references
        if re.search(r"(clauses?|Sections?|paragraphs?|subclauses?|subsections?|items?)\s*$", before, re.I):
            return True
        if re.search(r"\([a-z]{1,4}\)\s*$", before):             # "(a)(vii)" chained reference
            return True
        if re.search(r"\d\.\d+[A-Za-z]?\s*$", before):
            return True
        if re.search(r"\([a-z]{1,4}\)\s*(,|or|and|through|–|-)\s*$", before):  # "(x), (y) and (z)" lists of references
            return _list_started_by_clause_word(text, pos)
    return False


def _list_started_by_clause_word(text, pos, depth=0):
    """Walk back through "(a), (b) and" to the first marker of the run; it's a reference list only if THAT marker
    follows "clause(s)" / "Section(s)" etc. "Schedule 1.01(b), (r) ..." is a one-off reference, then a new item."""
    before = text[max(0, pos - 25):pos]
    m = re.search(r"\(([a-z]{1,4})\)\s*(,|or|and|through|–|-)\s*$", before)
    if not m or depth > 10:
        return False
    prev = max(0, pos - 25) + m.start()
    pb = text[max(0, prev - 25):prev]
    if re.search(r"(clauses?|Sections?|paragraphs?|subclauses?|subsections?|items?)\s*$", pb, re.I):
        return True
    if re.search(r"\d\.\d+[A-Za-z]?\s*$", pb):
        # "Section 8.1(a) or (b)" continues the reference; "Schedule 1.01(b), (r) ..." starts a new item
        return m.group(2) != ","
    return _list_started_by_clause_word(text, prev, depth + 1)


EXCL_BEFORE = re.compile(r"(except|excluding|other\s+than|shall\s+not\s+include|provided\s+that|but\s+not|less)\b[^()]{0,120}$", re.I)
# a roman list after the LAST letter that belongs to the whole definition, not to that letter: it starts a new sentence
# ("... (l) ... . Notwithstanding ..., Indebtedness shall not include (i) ...") or a new arithmetic step ("minus (i)",
# "less (i)", "but excluding, for the avoidance of doubt, (i)"). Found by the GPT second-model check (2026-10-13).
TOP_CUE = re.compile(r"(\bminus\b|\bless\b(?!\s+than)|but\s+excluding|for\s+the\s+avoidance\s+of\s+doubt)[^()]{0,200}$", re.I)
PAREN = re.compile(r"\([^()]{12,}\)")      # long parentheticals ("(including in respect of ...)") hide the cue
COND_BEFORE = re.compile(r"\b(that|which|who|whose|if|where|when)(\s+(are|is|has|have|shall))?\s*$", re.I)


def enumerate_clauses(text):
    letter_i = -1          # index into LETTERS of the last accepted top-level letter
    roman_i = -1           # index into ROMANS of the last accepted roman under the current letter
    shadow_l = None        # a nested (a),(b),(c) list inside a clause: its letters are NOT top-level
    shadow_r = None        # a nested (i),(ii) list restarted inside a roman clause
    items = []             # (id, offset, level)
    last_end = None        # end of the last ACCEPTED marker
    cond_letter = None     # letter whose roman sub-list is a list of CONDITIONS ("that are (i) ... (ii) ...")
    excl_parents = set()   # letters whose roman sub-list is EXCLUSIONS from the letter's own amount: the letter is a leaf too
    top_list = False       # the current roman list belongs to the whole definition (labels "excl(i)")
    letter_pos = None
    for m in MARK.finditer(text):
        tok, pos = m.group(1), m.start()
        # "(b) (i) time deposits ...": a marker right after an accepted list marker opens a sub-list; it is not a
        # chained cross-reference like "clause (a)(vii)"
        opens_sublist = last_end is not None and text[last_end:pos].strip() == ""
        if not opens_sublist and _is_reference(text, pos):
            continue
        # A fresh "(a)" (or "(i)") after the list is under way opens a NESTED list; follow it in the shadow
        if tok == "a" and letter_i >= 0 and not (roman_i < 0 and letter_i == 0):
            shadow_l = 0
            continue
        if shadow_l is not None and tok == LETTERS[shadow_l + 1]:
            shadow_l += 1
            continue
        if tok == "i" and roman_i >= 1:
            shadow_r = 0
            continue
        if shadow_r is not None and shadow_r + 1 < len(ROMANS) and tok == ROMANS[shadow_r + 1] and \
                not (roman_i + 1 < len(ROMANS) and tok == ROMANS[roman_i + 1]):
            shadow_r += 1
            continue
        nxt_letter = LETTERS[letter_i + 1] if letter_i + 1 < len(LETTERS) else None
        nxt_roman = ROMANS[roman_i + 1] if roman_i + 1 < len(ROMANS) else None
        is_letter_next = tok == nxt_letter
        is_roman_next = tok == nxt_roman
        if is_letter_next and is_roman_next:
            # "(i)": letter if we're running letters with no romans under the current one, else roman
            is_roman_next = roman_i >= 0 or letter_i < 0
            is_letter_next = not is_roman_next
        if is_letter_next and (letter_i >= 0 or tok == "a"):
            letter_i += 1
            roman_i = -1
            shadow_l = shadow_r = None
            top_list = False
            letter_pos = pos
            items.append((f"({tok})", pos, 1))
            last_end = m.end()
        elif is_roman_next:
            roman_i += 1
            shadow_r = None
            if roman_i == 0 and letter_i >= 0:
                nxt = LETTERS[letter_i + 1] if letter_i + 1 < len(LETTERS) else None
                # the next letter may also be a roman ("(v)" after "(u)"): then the letter after it must follow too
                want = [nxt] if nxt not in ROMANS else [nxt, LETTERS[letter_i + 2] if letter_i + 2 < len(LETTERS) else nxt]
                last_letter = nxt is None or not all(re.search(r"(;|,|\bplus|\band|\bor)\s*\(" + w + r"\)", text[pos:]) for w in want)
                new_sentence = bool(re.search(r"(?<![A-Z])\.\s+[A-Z“]", text[letter_pos:pos]))   # not "U.S. Dollar"
                top_list = last_letter and (new_sentence or bool(TOP_CUE.search(PAREN.sub("", text[max(0, pos - 400):pos]))))
            if roman_i == 0 and not top_list:
                # "(e) money market funds that (i) comply ..., (ii) are rated ...": conditions on one item, not items
                cond_letter = letter_i if COND_BEFORE.search(text[max(0, pos - 30):pos]) else None
                # "(c) ... Indebtedness ...; provided that ... shall not include (i) ...": (c) is an amount AND has exclusions
                if letter_i >= 0 and EXCL_BEFORE.search(text[max(0, pos - 160):pos]):
                    excl_parents.add(f"({LETTERS[letter_i]})")
            if cond_letter is not None and cond_letter == letter_i:
                continue
            parent = "excl" if top_list else (f"({LETTERS[letter_i]})" if letter_i >= 0 else "")
            items.append((f"{parent}({tok})", pos, 2))
            last_end = m.end()
    ids = [i for i, _, _ in items]
    leaves = []
    for k, (i, p, lvl) in enumerate(items):
        if lvl == 1 and any(x.startswith(i + "(") for x in ids) and i not in excl_parents:
            continue                                   # a letter with roman children is not itself a leaf (unless they are exclusions)
        nxt = items[k + 1][1] if k + 1 < len(items) else len(text)
        snippet = text[p:min(nxt, p + 220)]
        if re.match(r"\([^)]+\)\s*\[\s*reserved\s*\]", snippet, re.I):
            continue                                   # "(xi) [Reserved]" is not a component
        leaves.append((i, p, snippet))
    return leaves


def norm(c):
    c = c.replace(" ", "").lower()
    c = re.sub(r"^(clause|proviso)", "", c)
    return c


def coverage(code_leaves, model_clauses, starts_from=""):
    """Which code-enumerated clauses has the model covered? A model clause covers a code leaf if it equals it, or is a
    sub-part of it ((a)(xv)(x) covers (a)(xv)), or the leaf is a sub-part of it. A leaf that just names the base amount
    (e.g. "(a) Consolidated Net Income ...") is covered by the model's starts_from."""
    mc = [norm(c) for c in model_clauses]
    sf = starts_from.lower()[:40]
    missing = []
    for c, _, snip in code_leaves:
        # covered by the same clause or a finer sub-part of it; a part for the PARENT ("(d)") no longer covers its
        # sub-items ("(d)(i)" ...): those are often exclusions with their own amount and sign
        if any(m == c or m.startswith(c) for m in mc):
            continue
        if sf and sf in snip.lower()[:120]:
            continue
        missing.append(c)
    unknown = [m for m in mc if not any(m == c or m.startswith(c) or c.startswith(m) for c, _, _ in code_leaves)]
    return missing, unknown


# ---- definitions with no lettered/numbered clauses: split at operative phrases so coverage can still be checked ----
SEG_CUES = re.compile(
    r"(;\s*provided,?\s*(?:however,?\s*)?that|,?\s*provided,?\s*(?:however,?\s*)?that|\bbut\s+excluding\b|\bexcluding\b|"
    r"\bother\s+than\b|\bexcept\b|\bshall\s+not\s+include\b|\bin\s+each\s+case\b|\bthat\s+(?:are|is)\s+(?:not\s+)?|"
    r"\bnot\s+subject\s+to\b|\bheld\s+in\b|\blocated\s+in\b|\bplus\b|\bminus\b|\bless\b|\bnet\s+of\b|\btogether\s+with\b)", re.I)
MIN_SEG = 40


def segments_unclaused(text):
    """Split a definition with no clause markers into operative segments: the base, then one segment per proviso,
    exclusion, condition or arithmetic step. Short fragments (< MIN_SEG chars) merge into the previous segment.
    Returns [(label, start, end, snippet)]. The defined term's own lead-in ('“X” means') belongs to the base."""
    cuts = [0] + [m.start() for m in SEG_CUES.finditer(text) if m.start() > 0] + [len(text)]
    segs = []
    for a, b in zip(cuts, cuts[1:]):
        if segs and (b - a < MIN_SEG or segs[-1][2] - segs[-1][1] < MIN_SEG):
            lab, s0, _, _ = segs.pop()
            segs.append((lab, s0, b, text[s0:b]))
        else:
            segs.append(("base" if not segs else f"segment {len(segs)}", a, b, text[a:b]))
    return segs


# ---- #25: text no clause marker owns: provisos, "For purposes of" / "avoidance of doubt" / "Notwithstanding"
#      sentences, closing sentences. Coverage = some part quote or cap quote touches the span.
PROVISO = re.compile(r"provided\s*,?\s*(?:further\s*,?\s*)?(?:however\s*,?\s*)?(?:further\s*,?\s*)?that\b", re.I)
SENT_SPLIT = re.compile(r"(?<=[.;:])\s+(?=(?:[A-Z“\"]|\([a-z]{1,2}\)|\([ivxl]{1,6}\)))")
DEEMED_LIST = re.compile(r"\$\s?[\d,]+\s+(?:for|in respect of)\s+the\s+(?:fiscal\s+)?quarter\s+end", re.I)
LEADS = re.compile(r"^(For (the )?purposes? of|For the avoidance of doubt|Notwithstanding|In addition|Without limiting|"
                   r"It is (understood|agreed)|Any |Such |All |The |Unless|Solely|To the extent|In no event|In the case of|"
                   r"With respect to|There shall be|Consolidated|Adjusted|If |In the event|When )", re.I)


def unnumbered_spans(text, leaves):
    """Spans no clause marker owns, plus every proviso. Returns [(label, start, end, snippet)].
    - proviso: each 'provided (however/further) that ...' to the next ';' / sentence end (a limit, condition or carve-out)
    - sentence: a sentence after the first that contains no clause marker and starts a new thought (LEADS), i.e. the
      'For purposes of' / 'avoidance of doubt' / 'Notwithstanding' / closing sentences. The opening sentence ('X means
      ...') is the lead-in and is skipped."""
    out = []
    marks = sorted(p for _, p, _ in leaves)
    for m in PROVISO.finditer(text):
        if "(" in text[max(0, m.start() - 3):m.start()]:     # parenthetical proviso: part of its clause's own wording
            continue
        end = len(text)
        for stop in (re.compile(r";"), re.compile(r"\.\s+(?=[A-Z“\"(])")):
            n = stop.search(text, m.end())
            if n:
                end = min(end, n.start())
        out.append((f"proviso@{m.start()}", m.start(), end, text[m.start():end]))
    pos = 0
    for i, s in enumerate(re.split(SENT_SPLIT, text)):
        a = text.find(s, pos)
        b = a + len(s)
        pos = b
        if i == 0 or len(s) < 40 or DEEMED_LIST.search(s):   # deemed-value lists are read by code (#22)
            continue
        if any(a <= p < b for p in marks):            # holds a clause marker: owned by the clause list
            continue
        after_list = bool(marks) and a > marks[-1] and text[marks[-1]:a].count(".") > 0   # past the clause list's end
        if not s.endswith(".") and not LEADS.match(s) and not after_list:   # a ';' fragment continuing a clause
            continue
        if LEADS.match(s) or after_list:
            if not any(a <= x[1] < a + 5 for x in out):   # not just a proviso already listed
                out.append((f"sentence@{a}", a, b, s))
    return sorted(out, key=lambda x: x[1])
