"""Pure planning logic for continuation (replacement-chain) requests.

When a representative item stops reporting, a statistician extends one slot's
quote timeline to a later *target month* by chaining replacement items.  The
rules mirrored here are exactly the replacement rules enforced at publish
time in :mod:`app.index_math`:

* the currently open item's draft quotes run continuously from its
  ``active_from`` to its last quote month ``L`` — the first replacement month
  is therefore ``L`` (the continuation starts there);
* a replacement in month ``m`` needs both the old and the new item quoted in
  ``m``; the month-on-month relative for ``m`` still uses the old item and the
  new item only takes over from ``m+1``;
* ``m`` must be strictly later than the outgoing item's own start month, so an
  item that was itself switched in can never be switched out again in the
  same month;
* an item switched out can never come back: every candidate is used at most
  once and the chain never revisits an item.

A candidate that reaches the target becomes the final (open-ended) segment;
every other candidate must hand over in its own last quote month.  The solver
returns the chain with the fewest replacements, breaking ties by the order in
which candidates were supplied.

This module holds no I/O or HTTP concerns: it works on plain values and is
covered by unit tests independently of SQLite.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .months import month_index, month_range

MAX_CANDIDATES = 10


@dataclass(frozen=True)
class Candidate:
    code: str
    name: str
    # month -> price_cents, already validated as positive integers
    prices: dict[str, int]


@dataclass(frozen=True)
class Segment:
    """One item's part of the solved chain."""

    item_code: str
    item_name: str
    active_from: str
    # None for the final, open-ended segment; the switch month otherwise.
    active_through: str | None
    # Month in which this item takes over from its predecessor (None for the
    # item that was already in the draft).
    switch_month: str | None
    prices: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class ContinuationPlan:
    slot_key: str
    target_month: str
    current_code: str
    segments: tuple[Segment, ...]

    @property
    def replacement_months(self) -> list[str]:
        return [segment.switch_month for segment in self.segments if segment.switch_month]

    @property
    def replacement_count(self) -> int:
        return len(self.replacement_months)


def first_month(prices: dict[str, int]) -> str:
    return min(prices)


def last_month(prices: dict[str, int]) -> str:
    return max(prices)


def continuity_break(prices: dict[str, int]) -> str | None:
    """First month missing between the earliest and latest quote, if any."""
    if not prices:
        return None
    for month in month_range(first_month(prices), last_month(prices)):
        if month not in prices:
            return month
    return None


@dataclass(frozen=True)
class _Node:
    # Item currently carrying the slot after this node's last switch.
    code: str
    # Month up to which that item's own quotes reach.
    reached: str
    # Bitmask of candidates already consumed by the path.
    mask: int
    # ((candidate index, switch month), ...) in chain order.
    history: tuple[tuple[int, str], ...]


def solve(
    slot_key: str,
    current_code: str,
    current_name: str,
    current_active_from: str,
    current_quotes: dict[str, int],
    candidates: list[Candidate],
    target_month: str,
) -> ContinuationPlan | None:
    """Find the minimum-replacement chain reaching ``target_month``.

    ``current_quotes`` is assumed continuous from ``current_active_from``.
    Returns ``None`` when no chain reaches the target.
    """
    if not current_quotes:
        return None
    current_last = last_month(current_quotes)

    # Only candidates with gap-free quotes can ever take part in a chain.
    usable: list[tuple[Candidate, str, str]] = []
    for candidate in candidates:
        if not candidate.prices or continuity_break(candidate.prices) is not None:
            continue
        usable.append((candidate, first_month(candidate.prices), last_month(candidate.prices)))

    active_from = {current_code: current_active_from}
    for candidate, first, _last in usable:
        active_from[candidate.code] = first

    start = _Node(code=current_code, reached=current_last, mask=0, history=())
    queue: deque[_Node] = deque([start])
    # (item code, consumed mask) fully describes a node's future options;
    # BFS visits the cheapest path first, so the first occurrence wins.
    seen: set[tuple[str, int]] = {(current_code, 0)}
    goal: _Node | None = None

    while queue:
        node = queue.popleft()
        if node.code != current_code and node.reached == target_month:
            goal = node
            break
        outgoing_from = active_from[node.code]
        for index, (candidate, first, last) in enumerate(usable):
            bit = 1 << index
            if node.mask & bit:
                continue  # an item switched out can never be reused
            # The new item starts exactly when the outgoing item's quotes end,
            # which is also the mandatory overlap (both quoted that month).
            if first != node.reached:
                continue
            # The replacement month must be later than the outgoing item's own
            # start month (no same-month in-and-out).
            if month_index(first) <= month_index(outgoing_from):
                continue
            new_mask = node.mask | bit
            key = (candidate.code, new_mask)
            if key in seen:
                continue
            seen.add(key)
            queue.append(
                _Node(
                    code=candidate.code,
                    reached=last,
                    mask=new_mask,
                    history=node.history + ((index, first),),
                )
            )

    if goal is None:
        return None

    segments: list[Segment] = [
        Segment(
            item_code=current_code,
            item_name=current_name,
            active_from=current_active_from,
            # Closed at the first switch month; the chain always switches at
            # least once because callers require target > current last month.
            active_through=goal.history[0][1],
            switch_month=None,
            prices=tuple(sorted(current_quotes.items())),
        )
    ]
    for position, (index, switch_month) in enumerate(goal.history):
        candidate = usable[index][0]
        is_last = position == len(goal.history) - 1
        segments.append(
            Segment(
                item_code=candidate.code,
                item_name=candidate.name,
                active_from=switch_month,
                active_through=None if is_last else goal.history[position + 1][1],
                switch_month=switch_month,
                prices=tuple(sorted(candidate.prices.items())),
            )
        )
    return ContinuationPlan(
        slot_key=slot_key,
        target_month=target_month,
        current_code=current_code,
        segments=tuple(segments),
    )
