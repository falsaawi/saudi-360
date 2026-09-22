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
    # Row labels: the column with the most distinct non-numeric values.
    best, label_col = 0, None
    for c in range(0, min(8, MAX_SCAN_COLS)):
        vals = {text(row[c]) for row in body if c < len(row) and text(row[c]) and not is_number(row[c])}
        if len(vals) > best:
            best, label_col = len(vals), c
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
    for c in range(0, min(MAX_SCAN_COLS, max(len(r) for r in body) if body else 0)):
        if c == label_col:
            continue
        vals = {text(row[c]) for row in body if c < len(row) and text(row[c]) and not is_number(row[c])}
        if len(vals) < max(3, best // 3):
            continue
        if (arabic_share(c) > 0.5) != primary_ar and len(vals) > best_alt:
            best_alt, alt = len(vals), c

    counts: dict[int, int] = {}
    for row in body:
        for c in range(len(row)):
            if c != label_col and c != alt and is_number(row[c]):
                counts[c] = counts.get(c, 0) + 1
    need = max(2, len(body) // 5)
    value_cols = sorted(c for c, n in counts.items() if n >= need)
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


def column_label(grid: Grid, blk: Block, col: int) -> tuple[str, str]:
    """A column's full heading, stacked down the header block."""
    parts: list[str] = []
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
        if value not in parts:
            parts.append(value)
    joined = " ".join(parts)
    en, ar = split_bilingual(joined)
    return strip_caption(en), strip_caption(ar)


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
    """Read a sheet as row-series if periods run down it, else as a matrix."""
    blk = find_block(grid)
    body = grid[blk.first_data_row : blk.last_data_row + 1]

    periods_down = [parse_period(row[blk.label_col]) if blk.label_col < len(row) else None
                    for row in body]
    hits = sum(1 for p in periods_down if p)
    if hits >= max(3, len(body) * 0.6):
        return "row-series-v1", _read_row_series(grid, blk, body, periods_down, sheet)
    return "matrix-v1", _read_matrix(grid, blk, body, fallback, sheet)


def _labels(grid: Grid, blk: Block, row: list) -> tuple[str, str]:
    en, ar = split_bilingual(row[blk.label_col] if blk.label_col < len(row) else None)
    if blk.alt_label_col is not None and blk.alt_label_col < len(row):
        a_en, a_ar = split_bilingual(row[blk.alt_label_col])
        en, ar = en or a_en, ar or a_ar
    return strip_caption(en), strip_caption(ar)


def _read_row_series(grid, blk, body, periods_down, sheet="") -> list[Record]:
    out: list[Record] = []
    labels = drop_common_parts({c: column_label(grid, blk, c) for c in blk.value_cols})
    for row, per in zip(body, periods_down):
        if not per:
            continue
        for c in blk.value_cols:
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
    labels = drop_common_parts({c: column_label(grid, blk, c) for c in blk.value_cols})
    periods = {c: parse_period_from_header(grid, blk, c) for c in blk.value_cols}

    weight_cols = [c for c in blk.value_cols
                   if classify(" ".join(labels[c]), "") == "weight"]
    weight_col = weight_cols[0] if weight_cols else None

    for row in body:
        row_en, row_ar = _labels(grid, blk, row)
        if not (row_en or row_ar):
            continue
        weight = as_number(row[weight_col]) if weight_col is not None and weight_col < len(row) else None
        for c in blk.value_cols:
            if c == weight_col or c >= len(row) or not is_number(row[c]):
                continue
            value = as_number(row[c])
            if value is None:
                continue
            col_en, col_ar = labels[c]
            per = periods.get(c)
            out.append(Record(
                row_en, row_ar, col_en, col_ar, value,
                classify(col_en + " " + col_ar, sheet),
                per[0] if per else (fallback[0] if fallback else None),
                per[1] if per else (fallback[1] if fallback else None),
                weight,
            ))
    if not out:
        raise Rejected("matrix matched but produced no values")
    return out


def parse_period_from_header(grid: Grid, blk: Block, col: int) -> tuple[str, str] | None:
    """A period stated in a column's own header cells, if any."""
    for r in range(blk.header_start, blk.first_data_row):
        if col < len(grid[r]):
            own = grid[r][col]
            if text(own):
                got = parse_period(own)
                if got:
                    return got
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
        if owned:
            for word, num in MONTHS.items():
                if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", token):
                    month = num
                    break
            for word, q in QUARTERS.items():
                if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", token):
                    return (f"{year}-Q{q}", "quarterly") if year else None
    if year and month:
        return f"{year}-{month:02d}", "monthly"
    return None
