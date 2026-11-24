"""Exact index mathematics, basket validation and partial recomputation.

Method
------
The index is a chained, fixed-weight index of monthly price relatives:

    I_base = 100
    R_t    = Σ_i (w_i / 10000) · (p_i,t / p_i,t-1)
    I_t    = I_t-1 · R_t

Weights ``w_i`` are integer basis points (万分比) fixed for the whole basket
version and must sum to exactly 10000.

A replacement keeps the slot (and therefore its weight) and changes the
representative item.  The old item remains active through the overlap month
``m`` and the new item starts in ``m``; both quotes are mandatory in ``m``.
The relative for ``m`` uses the old item's own movement (old_m/old_m-1),
because the new item has no historical price.  From ``m+1`` the new item's
own movement (new_m+1/new_m) is used.  The overlap quote at ``m`` is the
base that makes that continuation possible.

All arithmetic uses :class:`fractions.Fraction`; index values are stored as
reduced numerator/denominator strings so chained values never lose precision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
from typing import Iterable

from .months import add_months, month_index, month_range

BASE_INDEX = Fraction(100, 1)
DECIMAL_PLACES = 6


@dataclass(frozen=True)
class Slot:
    slot_key: str
    name: str
    weight_bp: int


@dataclass(frozen=True)
class Item:
    code: str
    name: str
    slot_key: str
    active_from: str
    active_through: str | None
    predecessor_code: str | None = None


@dataclass
class VersionData:
    base_month: str
    slots: list[Slot]
    items: list[Item]
    prices: dict[tuple[str, str], int]
    _by_slot: dict[str, list[Item]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        grouped: dict[str, list[Item]] = {}
        for item in self.items:
            grouped.setdefault(item.slot_key, []).append(item)
        for values in grouped.values():
            values.sort(key=lambda item: month_index(item.active_from))
        self._by_slot = grouped

    def items_for_slot(self, slot_key: str) -> list[Item]:
        return self._by_slot.get(slot_key, [])

    def price(self, item_code: str, month: str) -> int | None:
        return self.prices.get((item_code, month))

    def latest_month(self) -> str | None:
        if not self.prices:
            return None
        return max(month for _, month in self.prices)


def is_item_active(item: Item, month: str) -> bool:
    if month < item.active_from:
        return False
    return item.active_through is None or month <= item.active_through


def opening_item(items: list[Item], month: str) -> Item | None:
    """Item whose month-on-month movement is used for ``month``.

    It must have been active in ``month - 1`` and still be linked in ``month``
    (i.e. its ``active_through`` is ``month`` or later, or open-ended).  In a
    replacement month this is deliberately the *old* item.
    """
    previous = add_months(month, -1)
    matches = [
        item
        for item in items
        if item.active_from <= previous
        and (item.active_through is None or item.active_through >= month)
    ]
    return matches[0] if len(matches) == 1 else None


def carry_item(items: list[Item], month: str) -> Item | None:
    """Item that remains in the basket after ``month`` (the new item at a
    replacement month, otherwise the only active item)."""
    matches = [
        item
        for item in items
        if item.active_from <= month
        and (item.active_through is None or item.active_through > month)
    ]
    return matches[0] if len(matches) == 1 else None


def item_at_base(items: list[Item], base_month: str) -> Item | None:
    matches = [item for item in items if is_item_active(item, base_month)]
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _issue(code: str, message: str, **extra: str | None) -> dict:
    payload = {"code": code, "message": message}
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload


def validate_basket(data: VersionData) -> list[dict]:
    issues: list[dict] = []
    base = data.base_month
    if not data.slots:
        issues.append(_issue("no_slots", "basket has no slots"))

    weight_sum = sum(slot.weight_bp for slot in data.slots)
    if data.slots and weight_sum != 10000:
        issues.append(
            _issue(
                "weight_sum_not_10000",
                f"slot weights sum to {weight_sum}, expected 10000",
            )
        )

    latest = data.latest_month()
    if latest is None:
        issues.append(_issue("no_prices", "basket has no prices"))

    # Price rows must belong to an item that is active in that month.
    for (code, month), _price in sorted(data.prices.items()):
        item = next((i for i in data.items if i.code == code), None)
        if item is None:
            issues.append(
                _issue(
                    "orphan_price",
                    f"price for unknown item {code!r} in {month}",
                    item_code=code,
                    month=month,
                )
            )
        elif not is_item_active(item, month):
            issues.append(
                _issue(
                    "price_for_inactive_item",
                    f"item {code!r} is not active in {month}",
                    slot_key=item.slot_key,
                    item_code=code,
                    month=month,
                )
            )

    if latest is None:
        # ``no_prices`` was already recorded; slots still need a base quote
        # check below, but there is no month loop to run.
        latest_reachable = None
    elif month_index(latest) < month_index(base):
        latest_reachable = None
        issues.append(
            _issue(
                "no_quote_at_or_after_base",
                f"latest quote is {latest}, before the base month {base}",
            )
        )
    else:
        latest_reachable = latest

    for slot in sorted(data.slots, key=lambda s: s.slot_key):
        items = data.items_for_slot(slot.slot_key)
        if not items:
            issues.append(
                _issue(
                    "slot_without_item",
                    f"slot {slot.slot_key!r} has no representative item",
                    slot_key=slot.slot_key,
                )
            )
            continue

        first = items[0]
        if first.active_from != base:
            issues.append(
                _issue(
                    "timeline_must_start_at_base",
                    f"slot {slot.slot_key!r} starts at {first.active_from}, expected {base}",
                    slot_key=slot.slot_key,
                    item_code=first.code,
                )
            )
        if first.predecessor_code is not None:
            issues.append(
                _issue(
                    "first_item_has_predecessor",
                    f"first item {first.code!r} cannot have a predecessor",
                    slot_key=slot.slot_key,
                    item_code=first.code,
                )
            )

        for previous, following in zip(items, items[1:]):
            if previous.active_through is None:
                issues.append(
                    _issue(
                        "timeline_open_before_successor",
                        f"item {previous.code!r} has no end month but is followed by {following.code!r}",
                        slot_key=slot.slot_key,
                        item_code=previous.code,
                    )
                )
            elif previous.active_through != following.active_from:
                issues.append(
                    _issue(
                        "timeline_discontinuous",
                        f"item {previous.code!r} ends {previous.active_through} but {following.code!r} starts {following.active_from}",
                        slot_key=slot.slot_key,
                        item_code=following.code,
                        month=following.active_from,
                    )
                )
            if following.predecessor_code != previous.code:
                issues.append(
                    _issue(
                        "predecessor_mismatch",
                        f"item {following.code!r} predecessor is {following.predecessor_code!r}, expected {previous.code!r}",
                        slot_key=slot.slot_key,
                        item_code=following.code,
                    )
                )

        if items[-1].active_through is not None:
            issues.append(
                _issue(
                    "timeline_ends_early",
                    f"last item {items[-1].code!r} ends at {items[-1].active_through}; basket timeline is not open-ended",
                    slot_key=slot.slot_key,
                    item_code=items[-1].code,
                )
            )

        base_item = item_at_base(items, base)
        if base_item is None:
            issues.append(
                _issue(
                    "no_base_item",
                    f"slot {slot.slot_key!r} has no single item covering {base}",
                    slot_key=slot.slot_key,
                    month=base,
                )
            )
        elif data.price(base_item.code, base) is None:
            issues.append(
                _issue(
                    "missing_quote",
                    f"missing base quote for {base_item.code!r} in {base}",
                    slot_key=slot.slot_key,
                    item_code=base_item.code,
                    month=base,
                )
            )

        relative_months = (
            month_range(add_months(base, 1), latest_reachable)
            if latest_reachable is not None
            else []
        )
        for month in relative_months:
            opening = opening_item(items, month)
            carry = carry_item(items, month)
            if opening is None:
                issues.append(
                    _issue(
                        "timeline_cannot_open",
                        f"slot {slot.slot_key!r} has no unique item carrying the relative into {month}",
                        slot_key=slot.slot_key,
                        month=month,
                    )
                )
            if carry is None:
                issues.append(
                    _issue(
                        "timeline_cannot_carry",
                        f"slot {slot.slot_key!r} has no item continuing beyond {month}",
                        slot_key=slot.slot_key,
                        month=month,
                    )
                )

            required: list[tuple[Item, str]] = []
            if opening is not None:
                required.append((opening, add_months(month, -1)))
                required.append((opening, month))
            if carry is not None:
                required.append((carry, month))
            for item, price_month in required:
                if data.price(item.code, price_month) is None:
                    overlap = (
                        item.active_from == price_month
                        and item.predecessor_code is not None
                    )
                    issues.append(
                        _issue(
                            "missing_quote",
                            f"missing quote for {item.code!r} in {price_month}",
                            slot_key=slot.slot_key,
                            item_code=item.code,
                            month=price_month,
                            context="replacement_overlap" if overlap else None,
                        )
                    )

    return _dedupe(issues)


def _dedupe(issues: Iterable[dict]) -> list[dict]:
    seen: set[tuple] = set()
    result: list[dict] = []
    for issue in issues:
        key = (
            issue.get("code"),
            issue.get("slot_key"),
            issue.get("item_code"),
            issue.get("month"),
        )
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


# ---------------------------------------------------------------------------
# Fingerprints and partial recomputation
# ---------------------------------------------------------------------------

def month_fingerprint(data: VersionData, month: str) -> tuple:
    """All calculation inputs for ``month``'s relative, in stable order."""
    parts: list[tuple] = []
    for slot in sorted(data.slots, key=lambda s: s.slot_key):
        opening = opening_item(data.items_for_slot(slot.slot_key), month)
        if opening is None:  # pragma: no cover - validation prevents this
            raise ValueError(f"cannot fingerprint {month}: no opening item for {slot.slot_key}")
        previous = add_months(month, -1)
        p_prev = data.price(opening.code, previous)
        p_cur = data.price(opening.code, month)
        if p_prev is None or p_cur is None:  # pragma: no cover
            raise ValueError(f"cannot fingerprint {month}: missing quote for {opening.code}")
        parts.append(
            (slot.slot_key, slot.weight_bp, opening.code, p_prev, p_cur)
        )
    return tuple(parts)


def earliest_affected_month(
    parent: VersionData | None, draft: VersionData
) -> str | None:
    """First month whose index inputs differ from the published parent.

    Returns ``None`` when every month can be reused.  Months before the
    earliest affected month are never recomputed.
    """
    if parent is None:
        return draft.base_month
    base = draft.base_month
    draft_latest = draft.latest_month()
    parent_latest = parent.latest_month()
    if draft_latest is None:
        return base
    end = draft_latest
    if parent_latest is not None and month_index(parent_latest) > month_index(end):
        end = parent_latest
    for month in month_range(add_months(base, 1), end):
        new_fp = (
            month_fingerprint(draft, month)
            if month_index(month) <= month_index(draft_latest)
            else None
        )
        old_fp = (
            month_fingerprint(parent, month)
            if parent_latest is not None and month_index(month) <= month_index(parent_latest)
            else None
        )
        if new_fp != old_fp:
            return month
    return None


def _relative(data: VersionData, month: str) -> Fraction:
    total = Fraction(0, 1)
    for slot in sorted(data.slots, key=lambda s: s.slot_key):
        opening = opening_item(data.items_for_slot(slot.slot_key), month)
        if opening is None:  # pragma: no cover
            raise ValueError(f"no opening item for {slot.slot_key} in {month}")
        previous = add_months(month, -1)
        p_prev = data.price(opening.code, previous)
        p_cur = data.price(opening.code, month)
        if p_prev is None or p_cur is None:  # pragma: no cover
            raise ValueError(f"missing quote for {opening.code} around {month}")
        total += Fraction(slot.weight_bp, 10000) * Fraction(p_cur, p_prev)
    return total


@dataclass(frozen=True)
class Point:
    month: str
    mom: Fraction | None
    index: Fraction
    origin: str  # 'reused' | 'computed'


def compute_points(
    draft: VersionData,
    parent: VersionData | None,
    parent_points: dict[str, Point],
    earliest: str | None,
) -> list[Point]:
    """Build the full point series, reusing parent points before ``earliest``."""
    base = draft.base_month
    points: list[Point] = []

    parent_base = parent_points.get(base)
    if parent is not None and parent_base is not None:
        points.append(Point(base, None, parent_base.index, "reused"))
    else:
        points.append(Point(base, None, BASE_INDEX, "computed"))

    latest = draft.latest_month()
    if latest is None:
        return points

    earliest_index = month_index(earliest) if earliest is not None else None
    for month in month_range(add_months(base, 1), latest):
        reuse = (
            parent is not None
            and parent_points.get(month) is not None
            and (earliest_index is None or month_index(month) < earliest_index)
        )
        if reuse:
            old = parent_points[month]
            points.append(Point(month, old.mom, old.index, "reused"))
            continue
        mom = _relative(draft, month)
        index = points[-1].index * mom
        points.append(Point(month, mom, index, "computed"))
    return points


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def decimal_string(value: Fraction, places: int = DECIMAL_PLACES) -> str:
    with localcontext() as ctx:
        ctx.prec = 60
        decimal = Decimal(value.numerator) / Decimal(value.denominator)
        quantized = decimal.quantize(
            Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP
        )
        return format(quantized, "f")


def point_to_dict(point: Point) -> dict:
    return {
        "month": point.month,
        "index": decimal_string(point.index),
        "mom": decimal_string(point.mom) if point.mom is not None else None,
        "index_num": str(point.index.numerator),
        "index_den": str(point.index.denominator),
        "mom_num": str(point.mom.numerator) if point.mom is not None else None,
        "mom_den": str(point.mom.denominator) if point.mom is not None else None,
        "origin": point.origin,
    }
