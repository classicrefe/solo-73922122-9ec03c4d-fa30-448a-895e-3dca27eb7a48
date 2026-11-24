"""Month helpers.

Months are canonical ``YYYY-MM`` strings.  Lexicographic order equals
chronological order for that representation, so SQLite can sort them directly.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


class InvalidMonth(ValueError):
    pass


def parse_month(value: str) -> tuple[int, int]:
    match = _MONTH_RE.match(value or "")
    if not match:
        raise InvalidMonth(f"month must be YYYY-MM, got {value!r}")
    year = int(match.group(1))
    month = int(match.group(2))
    if year < 1900 or year > 2200:
        raise InvalidMonth("month year must be between 1900 and 2200")
    return year, month


def month_index(value: str) -> int:
    year, month = parse_month(value)
    return year * 12 + (month - 1)


def month_from_index(index: int) -> str:
    year, month0 = divmod(index, 12)
    return f"{year:04d}-{month0 + 1:02d}"


def add_months(value: str, delta: int) -> str:
    return month_from_index(month_index(value) + delta)


def month_range(start: str, end: str) -> list[str]:
    first = month_index(start)
    last = month_index(end)
    if last < first:
        return []
    return [month_from_index(i) for i in range(first, last + 1)]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
