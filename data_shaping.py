"""
Turns raw monday.com board rows into small, LLM-friendly payloads:
either a compact aggregate summary, or a filtered/capped row subset.

This exists because free-tier LLM APIs (and just good practice generally)
cap how many tokens you can send per request - dumping ~350 raw rows with
a dozen+ columns each blows past that instantly. Pre-aggregating in Python
is also just better BI practice: a founder asking "how's the pipeline"
wants the rollup, not the raw table.
"""
from collections import defaultdict
from datetime import datetime

try:
    from dateutil import parser as _dateutil_parser
    _HAS_DATEUTIL = True
except ImportError:
    _HAS_DATEUTIL = False


def _num(value):
    """Best-effort parse of a messy numeric string. None if it's not numeric."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("₹", "").replace("$", "")
    if s == "" or s.lower() in ("nan", "none", "n/a", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm(value):
    """Normalize text for grouping: strip, collapse case, treat blanks as
    an explicit 'Missing' bucket so gaps in the data are visible, not hidden."""
    if value is None:
        return "Missing"
    s = str(value).strip()
    return s if s else "Missing"


def normalize_date(value) -> tuple[str | None, bool]:
    """
    Best-effort parse of a messy date value into a single canonical form
    (ISO 'YYYY-MM-DD'). Returns (normalized_value_or_None, was_changed).

    Tries exact ISO formats first (what monday.com Date columns already
    export as), then falls back to dateutil's fuzzy parser for anything
    else (e.g. "26/12/2025", "Dec 26, 2025") with day-first parsing, since
    this is an Indian company's data and dd/mm/yyyy is the common written
    convention when a field ends up as free text instead of a Date column.

    Returns (None, False) for blank values, and (None, True) for values
    that exist but couldn't be parsed at all - the caller can tell these
    apart from genuinely missing data (a real, if malformed, value was
    present) versus a truly empty field.
    """
    if value is None:
        return None, False
    s = str(value).strip()
    if not s:
        return None, False

    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            iso = dt.strftime("%Y-%m-%d")
            return iso, (iso != s)
        except ValueError:
            pass

    if _HAS_DATEUTIL:
        try:
            dt = _dateutil_parser.parse(s, dayfirst=True, fuzzy=False)
            return dt.strftime("%Y-%m-%d"), True
        except (ValueError, TypeError, OverflowError):
            pass

    return None, True  # had a value, but couldn't parse it - flagged, not hidden


def normalize_dates_in_rows(rows: list[dict], date_fields: list[str]) -> tuple[list[dict], dict]:
    """
    Returns (new_rows, stats). new_rows is a copy of rows with each field in
    date_fields rewritten to a canonical 'YYYY-MM-DD' string (or left as ""
    if blank/unparseable). stats reports, per field, how many values were
    actually reformatted vs. left as-is vs. present-but-unparseable, so the
    normalization is auditable rather than a silent transformation.
    """
    stats = {f: {"normalized_count": 0, "unparseable_count": 0} for f in date_fields}
    new_rows = []
    for row in rows:
        new_row = dict(row)
        for f in date_fields:
            if f not in row:
                continue
            iso, changed = normalize_date(row.get(f))
            if iso is None and changed:
                stats[f]["unparseable_count"] += 1
                new_row[f] = row.get(f)  # keep original malformed value visible, don't blank it out
            else:
                if changed and iso is not None:
                    stats[f]["normalized_count"] += 1
                new_row[f] = iso if iso is not None else ""
        new_rows.append(new_row)
    return new_rows, stats


def find_unmatched(primary_rows: list[dict], other_rows: list[dict],
                    filter_field: str | None = None, filter_value: str | None = None,
                    limit: int = 25) -> dict:
    """
    Cross-board join by item name: returns rows from primary_rows whose
    "name" has no matching row (case-insensitive, exact match on the
    normalized name) in other_rows. Optionally pre-filters primary_rows by
    a substring match on filter_field first (e.g. only "Project Won" deals).

    This is the server-side join for questions like "which won deals don't
    have a matching work order yet" - computed once in Python rather than
    asking the LLM to eyeball two separate raw row dumps and match names
    itself.
    """
    other_names = {str(r.get("name", "")).strip().lower() for r in other_rows if r.get("name")}

    candidates = primary_rows
    if filter_field and filter_value:
        needle = str(filter_value).lower()
        candidates = [r for r in candidates if needle in str(r.get(filter_field, "")).lower()]

    unmatched = [
        r for r in candidates
        if str(r.get("name", "")).strip() and str(r.get("name", "")).strip().lower() not in other_names
    ]

    limit = min(int(limit or 25), 30)
    return {
        "total_matches": len(unmatched),
        "returned": unmatched[:limit],
        "truncated": len(unmatched) > limit,
    }


def summarize_rows(rows: list[dict], group_by: list[str], value_field: str | None = None,
                    missing_check_fields: list[str] | None = None) -> dict:
    """
    Generic aggregator: counts (and optionally sums a numeric field) grouped
    by one or more text fields, plus missing-data stats for named fields.
    """
    total = len(rows)
    groups = defaultdict(lambda: {"count": 0, "value_sum": 0.0, "value_known": 0})

    for row in rows:
        key = tuple(_norm(row.get(f)) for f in group_by)
        g = groups[key]
        g["count"] += 1
        if value_field:
            v = _num(row.get(value_field))
            if v is not None:
                g["value_sum"] += v
                g["value_known"] += 1

    breakdown = []
    for key, g in sorted(groups.items(), key=lambda kv: -kv[1]["count"]):
        entry = dict(zip(group_by, key))
        entry["count"] = g["count"]
        if value_field:
            entry[f"{value_field}_sum"] = round(g["value_sum"], 2)
            entry[f"{value_field}_known_count"] = g["value_known"]
        breakdown.append(entry)

    missing_stats = {}
    for f in (missing_check_fields or []):
        missing = sum(1 for row in rows if _norm(row.get(f)) == "Missing")
        missing_stats[f] = {
            "missing_count": missing,
            "missing_pct": round(100 * missing / total, 1) if total else 0,
        }

    return {
        "total_rows": total,
        "grouped_by": group_by,
        "breakdown": breakdown,
        "missing_data": missing_stats,
    }


def filter_rows(rows: list[dict], filters: dict, limit: int = 25,
                 missing_field: str | None = None,
                 fields: list[str] | None = None) -> dict:
    """
    Case-insensitive substring filter across the given {field: value} pairs.
    If missing_field is set, only rows where that field is blank/missing are
    returned (ignores the substring filters). Returns up to `limit` matching
    rows plus the true match count, so the model (and the user) knows if
    results were truncated.

    `limit` is always hard-capped server-side (MAX_LIMIT) regardless of what
    the caller asks for - this is what keeps a single tool call from ever
    blowing the LLM's token budget, no matter what the model requests.

    If `fields` is given, only those columns (plus "name") are included per
    row - keeps the payload small when the full ~12-38 column row isn't
    needed for the question being asked.
    """
    MAX_LIMIT = 30
    limit = min(int(limit or 25), MAX_LIMIT)

    if missing_field:
        matches = [row for row in rows if _norm(row.get(missing_field)) == "Missing"]
    else:
        matches = []
        for row in rows:
            ok = True
            for field, needle in filters.items():
                if not needle:
                    continue
                hay = str(row.get(field, "") or "").lower()
                if str(needle).lower() not in hay:
                    ok = False
                    break
            if ok:
                matches.append(row)

    returned = matches[:limit]
    if fields:
        keep = set(fields) | {"name"}
        returned = [{k: v for k, v in row.items() if k in keep} for row in returned]

    return {
        "total_matches": len(matches),
        "returned": returned,
        "truncated": len(matches) > limit,
    }
