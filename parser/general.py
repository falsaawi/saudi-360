"""
General table readers.

The CPI recipe in `excel.py` handles tables with an explicit hierarchy Level
column. Most GASTAT products do not have one, but nearly all of their tables
reduce to one of two shapes:

  matrix      row labels down the side, labelled columns across the top; the
              period comes from the column header when it carries one, else
              from the release itself.
              e.g. indicator x (nationality x gender), activity x index columns

  row-series  the period runs DOWN the first column ("2017 Q1", "Q1 of 2024",
              "January 2019"); every other column is a measure.
              One such table yields a whole time series from a single file.

Both emit the same flat record, so the warehouse does not care which produced
it. A sheet matching neither is rejected with a reason.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from parser.excel import (
    MAX_SCAN_COLS,
    Grid,
    Rejected,
    _effective,
    as_number,
    is_number,
    normalize,
    split_bilingual,
    text,
)

# Header words that say what a column of numbers means.
CHANGE_WORDS = ("change", "تغير", "معدل", "نسبه التغير", "growth")
INDEX_WORDS = ("index", "الرقم القياسي", "الارقام القياسيه", "القياسي", "القياسيه", "مؤشر", "المؤشر")
WEIGHT_WORDS = ("weight", "وزن", "الاوزان", "الوزن", "الاهميه", "relative importance")
PCT_WORDS = ("percent", "%", "نسبه", "percentage", "ratio", "معدل")

QUARTERS = {"q1": 1, "q2": 2, "q3": 3, "q4": 4,
            "الربع الاول": 1, "الربع الثاني": 2, "الربع الثالث": 3, "الربع الرابع": 4}
MONTHS = {
    "january": 1, "jan": 1, "يناير": 1, "february": 2, "feb": 2, "فبراير": 2,
    "march": 3, "mar": 3, "مارس": 3, "april": 4, "apr": 4, "ابريل": 4,
    "may": 5, "مايو": 5, "june": 6, "jun": 6, "يونيو": 6,
    "july": 7, "jul": 7, "يوليو": 7, "august": 8, "aug": 8, "اغسطس": 8,
    "september": 9, "sep": 9, "sept": 9, "سبتمبر": 9,
    "october": 10, "oct": 10, "اكتوبر": 10, "november": 11, "nov": 11, "نوفمبر": 11,
    "december": 12, "dec": 12, "ديسمبر": 12,
}


@dataclass
class Record:
    row_en: str
    row_ar: str
    col_en: str
    col_ar: str
    value: float
    kind: str                      # index | change | weight | value
    period: str | None             # YYYY, YYYY-MM or YYYY-Qn, when the table says
    period_type: str | None
    weight: float | None = None


# --------------------------------------------------------------------------
# period parsing
# --------------------------------------------------------------------------

def parse_period(raw: str) -> tuple[str, str] | None:
    """A period written in a cell, as (period, period_type).

    Handles the forms GASTAT actually uses: "2017 Q1", "Q1 of 2024",
    "الربع الثاني 2024", "Jan-26", "January 2019", "2024-01-01", "2024".
    """
    if raw is None:
        return None
    if isinstance(raw, (dt.datetime, dt.date)):
        return f"{raw.year}-{raw.month:02d}", "monthly"

    token = normalize(text(raw))
    if not token or len(token) > 40:
        return None

    year = None
    m = re.search(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)", token)
    if m:
        year = int(m.group(0))
    if year is None:
        return None

    for word, q in QUARTERS.items():
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", token):
            return f"{year}-Q{q}", "quarterly"

    for word, num in MONTHS.items():
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", token):
            return f"{year}-{num:02d}", "monthly"

    iso = re.match(r"^(\d{4})-(\d{2})(-\d{2})?", token)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}", "monthly"

    # A bare year only counts when the cell is essentially just that year.
    if re.fullmatch(r"[^\d]{0,12}\d{4}[^\d]{0,12}", token):
        return str(year), "annual"
    return None


# A sheet whose NAME says it holds changes rather than levels. GASTAT ships the
# same table three times per release -- City_I, City_dA, City_dM -- with
# identical row and column labels. Without this hint all three collapse into one
# series and index levels end up interleaved with percentage changes.
SHEET_CHANGE = re.compile(r"(_d[aqmy]\b|_d[aqmy]_|\bd[aqmy]\b|change|تغير|التغيّر)", re.I)
SHEET_INDEX = re.compile(r"(_i\b|_i_|index|الرقم القياسي|الأرقام القياسية)", re.I)


def classify(label: str, sheet: str = "") -> str:
    low = normalize(label)
    if any(w in low for w in WEIGHT_WORDS):
        return "weight"
    says_change = any(w in low for w in CHANGE_WORDS)
    says_index = any(w in low for w in INDEX_WORDS)
    if says_change and not says_index:
        return "change"
    if says_index:
        return "index"
    # The column header alone was not decisive; fall back to the sheet's name.
    if sheet:
        if SHEET_CHANGE.search(sheet):
            return "change"
        if SHEET_INDEX.search(sheet):
            return "index"
    return "value"


# --------------------------------------------------------------------------
# stable labels and multi-column periods
# --------------------------------------------------------------------------

ORDINAL_Q = {"first": 1, "second": 2, "third": 3, "fourth": 4}
AR_QUARTER = {"الاول": 1, "الثاني": 2, "الثالث": 3, "الرابع": 4}

# Everything that names a period rather than a measure. Removing these from a
# column label is what lets the same measure keep one identity across releases.
PERIOD_BITS = re.compile(
    r"(?<![a-z\u0600-\u06ff])("
    r"q[1-4]([_\- ]?(19|20)\d{2})?"
    r"|(19|20)\d{2}[-/](0?[1-9]|1[0-2])([-/]\d{1,2})?"
    r"|(19|20)\d{2}"
    r"|(first|second|third|fourth)\s+quarter"
    r"|january|february|march|april|may|june|july|august|september|october|november|december"
    r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
    r"|الربع\s+(الاول|الأول|الثاني|الثالث|الرابع)"
    r"|يناير|فبراير|مارس|ابريل|أبريل|مايو|يونيو|يوليو|اغسطس|أغسطس|سبتمبر|اكتوبر|أكتوبر|نوفمبر|ديسمبر"
    r")(?![a-z\u0600-\u06ff])",
    re.IGNORECASE)

# Connector words left stranded once the period is removed.
STRANDED = re.compile(
    r"(?<![a-z])(in|from|of|for|the|to|and|at|على|في|من|مقارنة|مع|خلال)(?![a-z])",
    re.IGNORECASE)


def stable_label(label: str) -> str:
    """A column label with its period removed, so it survives the next release.

    "Index Numbers Q2_2026" and "Index Numbers Q1_2026" are the same measure
    read in two quarters. Keeping the quarter in the label made them two
    different series, one point each, and no chart could ever be drawn.
    """
    if not label:
        return ""
    # The index base is captured as its own field; as text it only differs
    # between releases that rebased, which is already modelled.
    out = re.sub(r"\(?\s*(19|20)\d{2}\s*=\s*100\s*\)?", " ", label)
    out = PERIOD_BITS.sub(" ", out)
    # A footnote marker on the period ("2026*" for provisional data) is left
    # stranded once the period goes. It is not part of the measure, and leaving
    # it split one GDP series into a "*" run for the provisional years and a
    # separate run for the settled ones.
    out = re.sub(r"(?<![A-Za-z0-9؀-ۿ])[*٭∗†‡]+(?![A-Za-z0-9؀-ۿ])", " ", out)
    if out.strip() != label.strip():
        # Only tidy connectors when something was actually removed, so ordinary
        # labels like "Number of the Establishments" keep their wording.
        out = STRANDED.sub(" ", out)
        # Day-of-month digits left behind by a stripped date, and the "=100"
        # tail of a base note, are debris rather than part of the measure.
        out = re.sub(r"(?<![\d.])\d{1,2}(?![\d.%])", " ", out)
        out = out.replace("=", " ")
    out = re.sub(r"[\s,;:\-–_/()]+", " ", out).strip(" ,;:-–_/()=")
    out = " ".join(out.split())
    # A column headed only by its period ("January", "2026*") names no measure:
    # the period is carried in the period field. Falling back to the original
    # wording gave every release its own one-point series, or -- where a
    # footnote marker outlived the stripping -- a column called "*". What is
    # left must still say something; a classification code still does, so the
    # test is for any letter OR digit rather than for a letter alone.
    if not re.search(r"[A-Za-z؀-ۿ0-9]", out):
        return ""
    return out


def cell_period_part(cell) -> tuple[str, int | None, int | None]:
    """What a single cell contributes to a period: (kind, year, sub).

    kind is "year", "quarter", "month" or "" -- a period split over two columns
    is assembled from the parts each one carries.
    """
    if isinstance(cell, dt.datetime) or isinstance(cell, dt.date):
        return "month", cell.year, cell.month
    token = normalize(text(cell))
    if not token or len(token) > 30:
        return "", None, None

    # A year cell routinely carries a footnote marker for provisional data --
    # "2026*". The marker is not part of the period, and demanding a bare year
    # here dropped every provisional row: the whole of 2026 was missing from the
    # foreign-trade tables, which is the most recent year anyone would look for.
    bare = re.sub(r"[*٭∗†‡\s]+", "", token)
    exact_year = re.fullmatch(r"(19[5-9]\d|20[0-4]\d)(\.0)?", bare)
    if exact_year:
        return "year", int(exact_year.group(1)), None

    for word, q in QUARTERS.items():
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", token):
            y = re.search(r"(19[5-9]\d|20[0-4]\d)", token)
            return "quarter", int(y.group(0)) if y else None, q
    for word, q in ORDINAL_Q.items():
        if re.search(r"(?<![a-z])" + word + r"\s+quarter", token):
            y = re.search(r"(19[5-9]\d|20[0-4]\d)", token)
            return "quarter", int(y.group(0)) if y else None, q
    for word, q in AR_QUARTER.items():
        if "الربع" in token and word in token:
            y = re.search(r"(19[5-9]\d|20[0-4]\d)", token)
            return "quarter", int(y.group(0)) if y else None, q
    for word, num in MONTHS.items():
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", token):
            y = re.search(r"(19[5-9]\d|20[0-4]\d)", token)
            return "month", int(y.group(0)) if y else None, num
    return "", None, None


def row_period(row: list, cols: range) -> tuple[str, str] | None:
    """The period a data row names, assembled across its first few columns.

    GASTAT often puts the year in one column and the quarter in the next. Read
    one cell at a time and neither is a period; read them together and the row
    is a point on a time axis.
    """
    year = sub = None
    kind = ""
    for c in cols:
        if c >= len(row):
            break
        k, y, v = cell_period_part(row[c])
        if not k:
            continue
        if y is not None and year is None:
            year = y
        if k in ("quarter", "month") and sub is None:
            sub, kind = v, k
    if year is None:
        return None
    if kind == "quarter" and sub:
        return f"{year}-Q{sub}", "quarterly"
    if kind == "month" and sub:
        return f"{year}-{sub:02d}", "monthly"
    return str(year), "annual"


def period_columns(grid: Grid, blk: "Block") -> list[int]:
    """Columns that carry the time axis rather than a measurement."""
    body = grid[blk.first_data_row : blk.last_data_row + 1]
    if not body:
        return []
    out = []
    for c in range(0, min(4, MAX_SCAN_COLS)):
        hits = sum(1 for row in body
                   if c < len(row) and cell_period_part(row[c])[0])
        if hits >= max(3, len(body) * 0.6):
            out.append(c)
    return out


# --------------------------------------------------------------------------
# block detection
# --------------------------------------------------------------------------

@dataclass
class Block:
    header_start: int
    first_data_row: int
    last_data_row: int
    label_col: int
    alt_label_col: int | None
    value_cols: list[int]


def _row_width(grid: Grid, r: int) -> int:
    return sum(1 for c in grid[r] if text(c))


def find_block(grid: Grid) -> Block:
    """Locate the rectangular data block and the header rows above it."""
    first = None
    for r, row in enumerate(grid):
        numeric = sum(1 for c in row if is_number(c))
        labelled = any(text(c) and not is_number(c) for c in row[:6])
        if numeric >= 2 and labelled:
            first = r
            break
    if first is None:
        raise Rejected("no data block found")

    last = first
    blanks = 0
    for r in range(first, len(grid)):
        if sum(1 for c in grid[r] if is_number(c)) >= 1:
            last = r
            blanks = 0
        else:
            blanks += 1
            if blanks >= 4:
                break

    header_start = first
    for r in range(first - 1, max(-1, first - 15), -1):
        cells = [text(c) for c in grid[r] if text(c)]
        if len(cells) == 1 and len(cells[0]) > 30:
            break
        header_start = r
    while header_start < first and _row_width(grid, header_start) == 0:
        header_start += 1

    body = grid[first : last + 1]
    # Row labels: the column whose distinct values read as names rather than as
    # codes. Ranking on the count alone let a column of ISIC section letters
    # ("B", "C", "D") beat the column of activity names beside it, so a whole
    # Foreign Direct Investment table charted as B, C and D. The distinct count
    # stays as the tie-break, so a table whose labels are genuinely all short
    # still picks the column it used to.
    def label_score(c: int) -> tuple[int, int]:
        vals = {text(row[c]) for row in body
                if c < len(row) and text(row[c]) and not is_number(row[c])}
        named = {v for v in vals
                 if HAS_LETTER.search(v) and (" " in v or len(v.strip(" .)(-:،")) > 3)}
        return len(named), len(vals)

    best_score, label_col = (0, 0), None
    for c in range(0, min(8, MAX_SCAN_COLS)):
        score = label_score(c)
        if score > best_score:
            best_score, label_col = score, c
    best = best_score[1]
    if label_col is None or best < 2:
        raise Rejected("no row-label column")

    # A second label column in the other script, if present.
    def arabic_share(col: int) -> float:
        vals = [text(row[col]) for row in body if col < len(row) and text(row[col]) and not is_number(row[col])]
        if not vals:
            return -1.0
        return sum(bool(re.search(r"[؀-ۿ]", v)) for v in vals) / len(vals)

    alt = None
    primary_ar = arabic_share(label_col) > 0.5
    best_alt = 0
    # A same-script neighbour is the other shape this takes: a column of row
    # numbers with the names beside it. It is only consulted when no
    # cross-script column exists, and only next to the label column, so a text
    # column elsewhere on the sheet cannot be mistaken for one.
    same_alt, best_same = None, 0
    for c in range(0, min(MAX_SCAN_COLS, max(len(r) for r in body) if body else 0)):
        if c == label_col:
            continue
        vals = {text(row[c]) for row in body if c < len(row) and text(row[c]) and not is_number(row[c])}
        if len(vals) < max(3, best // 3):
            continue
        if (arabic_share(c) > 0.5) != primary_ar:
            if len(vals) > best_alt:
                best_alt, alt = len(vals), c
        elif abs(c - label_col) <= 2 and len(vals) > best_same:
            # This column is consulted only for rows whose own label cell holds
            # no letters, so it may legitimately be a mixed column -- serials on
            # some rows, names on others, which is exactly GDP table 1.1.
            # Demanding that most of it be words dropped those rows entirely.
            if sum(1 for v in vals if HAS_LETTER.search(v)) >= 2:
                best_same, same_alt = len(vals), c
    if alt is None:
        alt = same_alt

    counts: dict[int, int] = {}
    for row in body:
        for c in range(len(row)):
            if c != label_col and c != alt and is_number(row[c]):
                counts[c] = counts.get(c, 0) + 1
    need = max(2, len(body) // 5)
    value_cols = sorted(c for c, n in counts.items() if n >= need)

    # Drop columns that are a time axis rather than a measurement: almost every
    # entry a bare year, and sitting where an axis sits. Without this, "2021"
    # is stored as though someone had measured it.
    def is_year_axis(col: int) -> bool:
        vals = [as_number(row[col]) for row in body
                if col < len(row) and is_number(row[col])]
        if len(vals) < 3:
            return False
        yearish = sum(1 for v in vals if v is not None and v == int(v)
                      and 1960 <= v <= 2035)
        if yearish < len(vals) * 0.9:
            return False
        header = normalize(" ".join(column_label(grid, blk_header, col)))
        return col < 3 or "year" in header or "سنه" in header or "العام" in header

    blk_header = Block(header_start, first, last, label_col, alt, value_cols)
    value_cols = [c for c in value_cols if not is_year_axis(c)]
    if not value_cols:
        raise Rejected("no value columns")

    return Block(header_start, first, last, label_col, alt, value_cols)


# Sheets caption their tables ("Table 2.1", "جدول (3-1)") on a row inside the
# header block. That caption is not part of any column's meaning, so it is
# dropped rather than prefixed onto every label.
CAPTION = re.compile(
    r"^\s*(table|جدول)\s*[\(\[]?\s*[\d٠-٩]+([.\-–][\d٠-٩]+)*\s*[\)\]]?\s*[:\-–]?\s*",
    re.IGNORECASE,
)


def strip_caption(value: str) -> str:
    out = CAPTION.sub("", value or "").strip()
    return out or (value or "").strip()


# A label is made of words. Anything without a letter in it -- "1-", "(2)", "*"
# -- is a serial, a footnote marker or punctuation left behind by cleaning, and
# is not something a reader can identify a row or column by.
HAS_LETTER = re.compile(r"[A-Za-z؀-ۿ]")


def _run_inside(hay: list[str], needle: list[str]) -> bool:
    """True when needle appears as a contiguous word run inside hay."""
    n = len(needle)
    if not n or n > len(hay):
        return False
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def _drop_restated(parts: list[str]) -> list[str]:
    """Drop a heading part that the part below it already states in full.

    GASTAT stacks a group banner over a more specific heading that repeats it --
    "Agricultural" above "Total Agricultural" -- and joining the stack verbatim
    reads "Agricultural Total Agricultural Index". The banner adds nothing the
    longer part does not already carry, so it goes. A part is dropped only when
    its whole word run sits inside a LONGER part: that keeps genuine groupings
    such as "Saudi" over "Total", where neither contains the other.
    """
    words = [normalize(p).split() for p in parts]
    keep = []
    for i, part in enumerate(parts):
        if words[i] and any(len(words[j]) > len(words[i]) and _run_inside(words[j], words[i])
                            for j in range(len(parts)) if j != i):
            continue
        keep.append(part)
    return keep or parts


def column_label(grid: Grid, blk: Block, col: int) -> tuple[str, str]:
    """A column's full heading, stacked down the header block."""
    en_parts: list[str] = []
    ar_parts: list[str] = []
    for r in range(blk.header_start, blk.first_data_row):
        value, _owned = _effective(grid, r, col)
        if not value:
            continue
        if CAPTION.match(value):
            continue          # the sheet's table caption, not a column heading
        # Inherited values matter as much as owned ones: in a header like
        # Saudi | Non-Saudi | Total over Male | Female | Total, the group a
        # column belongs to is ALWAYS inherited from a merged cell to its left.
        # Dropping inherited parts made "Saudi Total" and the overall "Total"
        # both read as "Total", so two different columns became one series.
        #
        # Each header cell carries both languages, so split per cell rather than
        # joining the stack first: a row that is Arabic-only must not glue itself
        # onto the English run of the row above it.
        en, ar = split_bilingual(value)
        if en and en not in en_parts:
            en_parts.append(en)
        if ar and ar not in ar_parts:
            ar_parts.append(ar)
    return (strip_caption(" ".join(_drop_restated(en_parts))),
            strip_caption(" ".join(_drop_restated(ar_parts))))


# --------------------------------------------------------------------------
# the two readers
# --------------------------------------------------------------------------

def drop_common_parts(labels: dict[int, tuple[str, str]]) -> dict[int, tuple[str, str]]:
    """Remove heading fragments shared by EVERY column.

    A banner row inside the header block ("Labor Market Ratios") is inherited by
    all columns alike. It says nothing about any one column, so it only makes
    labels long -- but a fragment shared by merely most columns is a real group
    heading and must stay.
    """
    if len(labels) < 2:
        return labels

    def parts(text: str) -> list[str]:
        return [p for p in text.split(" ") if p]

    out: dict[int, tuple[str, str]] = {}
    for lang in (0, 1):
        seqs = {c: parts(v[lang]) for c, v in labels.items()}
        common = set.intersection(*(set(s) for s in seqs.values())) if seqs else set()
        # Keep at least one word per label, even when everything is shared.
        for c, seq in seqs.items():
            kept = [w for w in seq if w not in common] or seq
            prev = out.get(c, ("", ""))
            out[c] = (" ".join(kept), prev[1]) if lang == 0 else (prev[0], " ".join(kept))
    return out


def read_table(grid: Grid, fallback: tuple[str, str] | None = None,
               sheet: str = "") -> tuple[str, list[Record]]:
    """Read a sheet as row-series if a time axis runs down it, else as a matrix."""
    blk = find_block(grid)
    body = grid[blk.first_data_row : blk.last_data_row + 1]

    # A period may sit in one cell ("2017 Q1") or be split across two columns
    # (Year | Quarter). Assemble it from the first few columns either way; the
    # single-cell case is just the one-column version of the same thing.
    pcols = period_columns(grid, blk)
    if pcols:
        span = range(min(pcols), max(pcols) + 1)
        periods_down = [row_period(row, span) for row in body]
        hits = sum(1 for x in periods_down if x)
        if hits >= max(3, len(body) * 0.6):
            return "row-series-v1", _read_row_series(
                grid, blk, body, periods_down, sheet, set(span))

    return "matrix-v1", _read_matrix(grid, blk, body, fallback, sheet)


def _labels(grid: Grid, blk: Block, row: list) -> tuple[str, str]:
    en, ar = split_bilingual(row[blk.label_col] if blk.label_col < len(row) else None)
    if blk.alt_label_col is not None and blk.alt_label_col < len(row):
        a_en, a_ar = split_bilingual(row[blk.alt_label_col])
        # GASTAT numbers the rows of some tables in their own column, so the
        # names sit one column over: "1-" beside "Oil Activities". An entry with
        # no letter in it is a serial, not a label, so the neighbouring column
        # stands in for it -- not merely when the primary cell is empty, which
        # is what left GDP table 1.1 with rows called "1", "2" and "3".
        en = en if HAS_LETTER.search(en or "") else (a_en or en)
        ar = ar if HAS_LETTER.search(ar or "") else (a_ar or ar)
    return strip_caption(en), strip_caption(ar)


def _read_row_series(grid, blk, body, periods_down, sheet="", skip=frozenset()) -> list[Record]:
    out: list[Record] = []
    # A column holding the year is the axis, not a measurement. Reading it as a
    # value is what produced observations whose "value" was 2021.
    cols = [c for c in blk.value_cols if c not in skip]
    if not cols:
        raise Rejected("row-series matched but every column is part of the time axis")
    labels = drop_common_parts({c: column_label(grid, blk, c) for c in cols})
    for row, per in zip(body, periods_down):
        if not per:
            continue
        for c in cols:
            if c >= len(row) or not is_number(row[c]):
                continue
            value = as_number(row[c])
            if value is None:
                continue
            col_en, col_ar = labels[c]
            if not (col_en or col_ar):
                continue
            out.append(Record(col_en, col_ar, "", "", value,
                              classify(col_en + " " + col_ar, sheet), per[0], per[1]))
    if not out:
        raise Rejected("row-series matched but produced no values")
    return out


def _read_matrix(grid, blk, body, fallback, sheet="") -> list[Record]:
    out: list[Record] = []
    raw_labels = {c: column_label(grid, blk, c) for c in blk.value_cols}
    periods = {c: parse_period_from_header(grid, blk, c) for c in blk.value_cols}
    # The period belongs in the period field, not in the series name. Leaving it
    # in the label gave every release its own one-point series: eight quarters of
    # Real Estate became 420 series, none of them chartable.
    labels = drop_common_parts(
        {c: (stable_label(en), stable_label(ar)) for c, (en, ar) in raw_labels.items()})

    weight_cols = [c for c in blk.value_cols
                   if classify(" ".join(labels[c]), "") == "weight"]
    weight_col = weight_cols[0] if weight_cols else None

    def is_header_row(row: list) -> bool:
        """A header row that slipped into the data block.

        Such a row carries the period across its columns, so every number in it
        is a bare year. A real measurement row does not look like that, and
        storing one leaves observations whose "value" is 2021.
        """
        nums = [as_number(row[c]) for c in blk.value_cols
                if c < len(row) and is_number(row[c])]
        nums = [v for v in nums if v is not None]
        return len(nums) >= 2 and all(v == int(v) and 1960 <= v <= 2035 for v in nums)

    for row in body:
        row_en, row_ar = _labels(grid, blk, row)
        if not (row_en or row_ar):
            continue
        if is_header_row(row):
            continue
        weight = as_number(row[weight_col]) if weight_col is not None and weight_col < len(row) else None
        for c in blk.value_cols:
            if c == weight_col or c >= len(row) or not is_number(row[c]):
                continue
            value = as_number(row[c])
            if value is None:
                continue
            col_en, col_ar = labels[c]
            raw_en, raw_ar = raw_labels[c]
            per = periods.get(c)
            out.append(Record(
                row_en, row_ar, col_en, col_ar, value,
                classify(raw_en + " " + raw_ar, sheet),
                per[0] if per else (fallback[0] if fallback else None),
                per[1] if per else (fallback[1] if fallback else None),
                weight,
            ))
    if not out:
        raise Rejected("matrix matched but produced no values")
    return out


def parse_period_from_header(grid: Grid, blk: Block, col: int) -> tuple[str, str] | None:
    """A period stated in a column's own header cells, if any."""
    # A bare year sitting above a row of quarters is the group's banner, not the
    # column's period. Only the merged cell's first column owns it, so returning
    # it as soon as it was seen dated every Q1 as though it were the whole year:
    # GDP's 2026-Q1 came through as "2026". Hold it and keep looking for
    # something more specific.
    annual = None
    for r in range(blk.header_start, blk.first_data_row):
        if col < len(grid[r]):
            own = grid[r][col]
            if text(own):
                got = parse_period(own)
                if got:
                    if got[1] != "annual":
                        return got
                    annual = annual or got
    # A year may span the group; combine it with this column's own month.
    year = month = None
    for r in range(blk.header_start, blk.first_data_row):
        value, owned = _effective(grid, r, col)
        token = normalize(text(value))
        if not token:
            continue
        m = re.search(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)", token)
        if m:
            year = int(m.group(0))
        # A month or quarter in the column's OWN cell names that column. In an
        # inherited cell it counts only when the year rides along with it: such
        # a cell is a complete period banner over the whole group ("يوليو 2026"
        # above every column of the industrial production table), not a
        # sub-heading that belongs to some other column. Reading only owned
        # cells left those columns with a bare inherited year, and an annual
        # 2026 is not where a monthly release's figures belong.
        if owned or m:
            for word, num in MONTHS.items():
                if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", token):
                    month = num
                    break
            for word, q in QUARTERS.items():
                if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", token):
                    return (f"{year}-Q{q}", "quarterly") if year else annual
    if year and month:
        return f"{year}-{month:02d}", "monthly"
    # A year banner merged across several columns is owned by the first of them
    # and only inherited by the rest. Without this, the followers had no period
    # of their own and fell back to the release's, so the 2023 columns of the
    # population table were all dated 2024 -- and collided with the real ones.
    if year:
        return str(year), "annual"
    return annual
