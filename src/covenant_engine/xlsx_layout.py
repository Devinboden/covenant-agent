"""Row heights that show all the text, measured rather than guessed (code only).

Excel and LibreOffice don't resize rows to fit wrapped text in a file they didn't edit, so a generated workbook has to
set every row's height itself. fit(ws) wraps each text cell the way Excel would, word by word, at the column's real width
using the font's real glyph widths (Liberation Sans is metric-compatible with Arial), and sets each row tall enough for
its tallest cell. A text cell that would spill into a filled neighbour is wrapped first, so nothing is cut off.
"""
from functools import lru_cache
from pathlib import Path

from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.styles import Alignment

FONT_DIRS = ["/usr/share/fonts/truetype/liberation", "/usr/share/fonts/liberation", "/Library/Fonts",
             "/System/Library/Fonts/Supplemental", "C:/Windows/Fonts"]
FILES = {(False, False): ["LiberationSans-Regular.ttf", "Arial.ttf", "arial.ttf"],
         (True, False): ["LiberationSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"],
         (False, True): ["LiberationSans-Italic.ttf", "Arial Italic.ttf", "ariali.ttf"],
         (True, True): ["LiberationSans-BoldItalic.ttf", "Arial Bold Italic.ttf", "arialbi.ttf"]}
PX_PER_PT = 96 / 72
LINE = 1.28           # line height as a multiple of the font size (Arial; a little over Excel's, never clips)
RICH_LINE = 1.5       # line height in two-tone (rich text) cells
PAD_PT = 4            # top + bottom padding in a cell
CELL_PAD_PX = 7       # left + right padding inside a cell


@lru_cache(maxsize=None)
def _font(size, bold, italic):
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    for d in FONT_DIRS:
        for f in FILES[(bool(bold), bool(italic))]:
            p = Path(d) / f
            if p.exists():
                return ImageFont.truetype(str(p), max(1, round(size * PX_PER_PT)))
    return None


def text_px(text, size=10, bold=False, italic=False):
    f = _font(size, bold, italic)
    if f is None:                                          # no font file: average Arial glyph width
        return len(text) * size * PX_PER_PT * (0.58 if bold else 0.53)
    return f.getlength(text)


def col_px(width):
    """Excel column width (characters of the default font) -> pixels."""
    return int(width * 7 + 5)


def wrap_lines(text, avail, size=10, bold=False, italic=False):
    """Lines the text takes at avail pixels, wrapping on spaces as Excel does (long words break)."""
    n = 0
    for para in str(text).split("\n"):
        words, line = para.split(" "), ""
        n += 1
        for w in words:
            trial = w if not line else line + " " + w
            if text_px(trial, size, bold, italic) <= avail:
                line = trial
                continue
            if line:
                n += 1
            line = w
            while text_px(line, size, bold, italic) > avail and len(line) > 1:   # a word wider than the column
                cut = len(line)
                while cut > 1 and text_px(line[:cut], size, bold, italic) > avail:
                    cut -= 1
                line = line[cut:]
                n += 1
    return n


def _runs(cell):
    """[(text, size, bold, italic)] paragraphs of a cell, one per line break, with the font each is drawn in."""
    v = cell.value
    f = cell.font
    base = (f.sz or 11, bool(f.b), bool(f.i))
    if isinstance(v, CellRichText):
        out = []
        for part in v:
            if isinstance(part, TextBlock):
                fnt = part.font
                size, b, i = (fnt.sz or base[0]), bool(fnt.b), bool(fnt.i)
                text = part.text
            else:
                size, b, i = base
                text = str(part)
            for k, seg in enumerate(text.split("\n")):
                if k == 0 and out and not text.startswith("\n"):
                    prev = out.pop()                       # continues the previous paragraph
                    out.append((prev[0] + seg, max(prev[1], size), prev[2] or b, prev[3] or i))
                elif seg or k:
                    out.append((seg, size, b, i))
        return out
    return [(str(v), *base)]


def _height(cell, width_px):
    avail = max(20, width_px - CELL_PAD_PX)
    h = 0.0
    runs = _runs(cell)
    # two-tone cells (a name over the agreement's words): renderers space and widen these runs the most, so they are
    # sized for the most generous one; a little space beats a cut-off line
    line, fill = (RICH_LINE, 0.84) if len(runs) > 1 else (LINE, 0.96)
    for text, size, b, i in runs:
        h += wrap_lines(text, avail * fill, size, b, i) * size * line
    return h + PAD_PT


def fit(ws, min_height=15):
    """Wrap text that would be cut off and size every row to its tallest cell."""
    widths = {}
    for k, d in ws.column_dimensions.items():
        widths[k] = d.width
    from openpyxl.utils import get_column_letter

    def width_of(c1, c2):
        return sum(col_px(widths.get(get_column_letter(c)) or 8.43) for c in range(c1, c2 + 1))

    merged = {}
    for m in ws.merged_cells.ranges:
        if m.min_row == m.max_row:
            merged[(m.min_row, m.min_col)] = (m.min_col, m.max_col)
        for r in range(m.min_row, m.max_row + 1):
            for c in range(m.min_col, m.max_col + 1):
                if (r, c) != (m.min_row, m.min_col):
                    merged.setdefault((r, c), None)
    for row in ws.iter_rows():
        need = 0.0
        r = row[0].row
        for cell in row:
            if cell.value is None or (r, cell.column) in merged and merged[(r, cell.column)] is None:
                continue
            if not isinstance(cell.value, (str, CellRichText)) or str(cell.value).startswith("="):
                need = max(need, (cell.font.sz or 11) * LINE + PAD_PT)
                continue
            c1, c2 = merged.get((r, cell.column)) or (cell.column, cell.column)
            w = width_of(c1, c2)
            wrap = bool(cell.alignment and cell.alignment.wrap_text)
            if not wrap:
                one = max(text_px(t, s, b, i) for t, s, b, i in _runs(cell)) + CELL_PAD_PX
                right = ws.cell(row=r, column=c2 + 1).value
                if one > w and (right is not None or "\n" in str(cell.value)):
                    a = cell.alignment
                    cell.alignment = Alignment(horizontal=a.horizontal, vertical=a.vertical or "top", wrap_text=True,
                                               indent=a.indent)
                    wrap = True
            need = max(need, _height(cell, w) if wrap else (cell.font.sz or 11) * LINE + PAD_PT)
        if need:
            ws.row_dimensions[r].height = round(max(min_height, need), 1)
