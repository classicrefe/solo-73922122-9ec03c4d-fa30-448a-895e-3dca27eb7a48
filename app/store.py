"""Persistence and application services.

A basket owns an ordered series of immutable *published* versions plus at
most one editable *draft*.  Publishing writes the version, its definition and
its computed index points in one SQLite transaction and only then flips the
version to ``published`` and updates ``baskets.current_version_id``.  A failed
validation rolls the whole transaction back, so the previously published
version remains intact and queryable.

A *revision* copies the current published version into a new draft.  Its
``parent_id`` records what it was derived from; publishing reuses every point
before the earliest affected month and recomputes from that month onward.
"""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from typing import Any

from .db import read_txn, write_txn
from .errors import ApiError, conflict, invalid, not_found
from .index_math import (
    Item,
    Point,
    Slot,
    VersionData,
    compute_points,
    earliest_affected_month,
    point_to_dict,
    validate_basket,
)
from .months import add_months, month_index, now_iso, parse_month

# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _basket_row(conn: sqlite3.Connection, basket_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM baskets WHERE id = ?", (basket_id,)).fetchone()
    if row is None:
        raise not_found("basket_not_found", f"basket {basket_id} does not exist")
    return row


def _version_row(
    conn: sqlite3.Connection, basket_id: int, version_no: int
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM basket_versions WHERE basket_id = ? AND version_no = ?",
        (basket_id, version_no),
    ).fetchone()
    if row is None:
        raise not_found(
            "version_not_found",
            f"basket {basket_id} has no version {version_no}",
        )
    return row


def _draft_row(conn: sqlite3.Connection, basket_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM basket_versions WHERE basket_id = ? AND status = 'draft'",
        (basket_id,),
    ).fetchone()
    if row is None:
        raise conflict("no_draft", f"basket {basket_id} has no editable draft")
    return row


def _published_row(conn: sqlite3.Connection, basket_id: int, version_no: int) -> sqlite3.Row:
    row = _version_row(conn, basket_id, version_no)
    if row["status"] != "published":
        raise conflict(
            "version_not_published",
            f"version {version_no} of basket {basket_id} is a draft and cannot be queried",
        )
    return row


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_data(conn: sqlite3.Connection, version_id: int, base_month: str) -> VersionData:
    slots = [
        Slot(row["slot_key"], row["name"], row["weight_bp"])
        for row in conn.execute(
            "SELECT slot_key, name, weight_bp FROM basket_slots "
            "WHERE basket_version_id = ? ORDER BY slot_key",
            (version_id,),
        )
    ]
    items = [
        Item(
            code=row["code"],
            name=row["name"],
            slot_key=row["slot_key"],
            active_from=row["active_from"],
            active_through=row["active_through"],
            predecessor_code=row["predecessor_code"],
        )
        for row in conn.execute(
            "SELECT code, name, slot_key, active_from, active_through, predecessor_code "
            "FROM basket_items WHERE basket_version_id = ? "
            "ORDER BY slot_key, active_from",
            (version_id,),
        )
    ]
    prices = {
        (row["item_code"], row["month"]): row["price_cents"]
        for row in conn.execute(
            "SELECT item_code, month, price_cents FROM prices "
            "WHERE basket_version_id = ?",
            (version_id,),
        )
    }
    return VersionData(base_month=base_month, slots=slots, items=items, prices=prices)


def _load_points(conn: sqlite3.Connection, version_id: int) -> dict[str, Point]:
    from fractions import Fraction

    points: dict[str, Point] = {}
    for row in conn.execute(
        "SELECT month, mom_num, mom_den, index_num, index_den, origin "
        "FROM index_points WHERE basket_version_id = ? ORDER BY month",
        (version_id,),
    ):
        points[row["month"]] = Point(
            month=row["month"],
            mom=(
                Fraction(int(row["mom_num"]), int(row["mom_den"]))
                if row["mom_num"] is not None
                else None
            ),
            index=Fraction(int(row["index_num"]), int(row["index_den"])),
            origin=row["origin"],
        )
    return points


def _version_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "version_no": row["version_no"],
        "status": row["status"],
        "base_month": row["base_month"],
        "parent_version_no": None,  # filled in by caller when available
        "recomputed_from": row["recomputed_from"],
        "reused_point_count": row["reused_point_count"],
        "computed_point_count": row["computed_point_count"],
        "snapshot_sha256": row["snapshot_sha256"],
        "created_at": row["created_at"],
        "published_at": row["published_at"],
    }


def _basket_summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    versions = conn.execute(
        "SELECT * FROM basket_versions WHERE basket_id = ? ORDER BY version_no",
        (row["id"],),
    ).fetchall()
    by_id = {v["id"]: v for v in versions}
    current = by_id.get(row["current_version_id"])
    draft = next((v for v in versions if v["status"] == "draft"), None)
    version_items = []
    for version in versions:
        summary = _version_summary(version)
        parent = by_id.get(version["parent_id"])
        summary["parent_version_no"] = parent["version_no"] if parent else None
        version_items.append(summary)
    return {
        "id": row["id"],
        "name": row["name"],
        "base_month": row["base_month"],
        "current_version_no": current["version_no"] if current else None,
        "draft_version_no": draft["version_no"] if draft else None,
        "created_at": row["created_at"],
        "versions": version_items,
    }


def _definition(conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
    slots = [
        {
            "slot_key": row["slot_key"],
            "name": row["name"],
            "weight_bp": row["weight_bp"],
        }
        for row in conn.execute(
            "SELECT slot_key, name, weight_bp FROM basket_slots "
            "WHERE basket_version_id = ? ORDER BY slot_key",
            (version_id,),
        )
    ]
    items = [
        {
            "code": row["code"],
            "name": row["name"],
            "slot_key": row["slot_key"],
            "active_from": row["active_from"],
            "active_through": row["active_through"],
            "predecessor_code": row["predecessor_code"],
        }
        for row in conn.execute(
            "SELECT code, name, slot_key, active_from, active_through, predecessor_code "
            "FROM basket_items WHERE basket_version_id = ? "
            "ORDER BY slot_key, active_from",
            (version_id,),
        )
    ]
    prices = [
        {"item_code": row["item_code"], "month": row["month"], "price_cents": row["price_cents"]}
        for row in conn.execute(
            "SELECT item_code, month, price_cents FROM prices "
            "WHERE basket_version_id = ? ORDER BY item_code, month",
            (version_id,),
        )
    ]
    return {"slots": slots, "items": items, "prices": prices}


def _snapshot_payload(
    basket_name: str,
    version_no: int,
    definition: dict[str, Any],
    base_month: str,
    points: list[Point],
) -> dict[str, Any]:
    return {
        "basket": basket_name,
        "version_no": version_no,
        "base_month": base_month,
        "slots": definition["slots"],
        "items": definition["items"],
        "prices": definition["prices"],
        "points": [
            {
                "month": p.month,
                "index_num": str(p.index.numerator),
                "index_den": str(p.index.denominator),
                "mom_num": str(p.mom.numerator) if p.mom is not None else None,
                "mom_den": str(p.mom.denominator) if p.mom is not None else None,
                "origin": p.origin,
            }
            for p in points
        ],
    }


def snapshot_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Structural validation (draft creation / raw item timelines)
# ---------------------------------------------------------------------------


def _validate_structure(
    base_month: str,
    slots: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> None:
    parse_month(base_month)
    slot_keys = [s["slot_key"] for s in slots]
    if len(set(slot_keys)) != len(slot_keys):
        raise invalid("duplicate_slot_key", "slot_key values must be unique")
    item_codes = [i["code"] for i in items]
    if len(set(item_codes)) != len(item_codes):
        raise invalid("duplicate_item_code", "item code values must be unique")
    known_slots = set(slot_keys)
    slot_starts: set[tuple[str, str]] = set()
    slot_predecessors: set[tuple[str, str]] = set()
    for item in items:
        if item["slot_key"] not in known_slots:
            raise invalid(
                "unknown_slot",
                f"item {item['code']!r} references unknown slot {item['slot_key']!r}",
            )
        start = item["active_from"]
        end = item.get("active_through")
        parse_month(start)
        if end is not None:
            parse_month(end)
            if month_index(end) < month_index(start):
                raise invalid(
                    "invalid_active_range",
                    f"item {item['code']!r} ends before it starts",
                )
        for month in item.get("prices", {}):
            parse_month(month)

        start_key = (item["slot_key"], start)
        if start_key in slot_starts:
            raise invalid(
                "duplicate_item_start",
                f"slot {item['slot_key']!r} has more than one item starting in {start}",
            )
        slot_starts.add(start_key)

        predecessor = item.get("predecessor_code")
        if predecessor is not None:
            pred_key = (item["slot_key"], predecessor)
            if pred_key in slot_predecessors:
                raise invalid(
                    "duplicate_predecessor",
                    f"slot {item['slot_key']!r} has more than one successor of {predecessor!r}",
                )
            slot_predecessors.add(pred_key)

    known_codes = set(item_codes)
    for item in items:
        predecessor = item.get("predecessor_code")
        if predecessor is not None and predecessor not in known_codes:
            raise invalid(
                "unknown_predecessor",
                f"item {item['code']!r} references unknown predecessor {predecessor!r}",
            )
        if predecessor == item["code"]:
            raise invalid(
                "self_predecessor",
                f"item {item['code']!r} cannot be its own predecessor",
            )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _insert_definition(
    conn: sqlite3.Connection,
    version_id: int,
    slots: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> None:
    conn.executemany(
        "INSERT INTO basket_slots (basket_version_id, slot_key, name, weight_bp) "
        "VALUES (?, ?, ?, ?)",
        [(version_id, s["slot_key"], s["name"], s["weight_bp"]) for s in slots],
    )
    conn.executemany(
        "INSERT INTO basket_items "
        "(basket_version_id, slot_key, code, name, active_from, active_through, predecessor_code) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                version_id,
                i["slot_key"],
                i["code"],
                i["name"],
                i["active_from"],
                i.get("active_through"),
                i.get("predecessor_code"),
            )
            for i in items
        ],
    )
    price_rows = [
        (version_id, i["code"], month, price)
        for i in items
        for month, price in sorted(i.get("prices", {}).items())
    ]
    conn.executemany(
        "INSERT INTO prices (basket_version_id, item_code, month, price_cents) "
        "VALUES (?, ?, ?, ?)",
        price_rows,
    )


def create_basket(
    db_path: Any, payload: dict[str, Any], busy_timeout_ms: int = 5_000
) -> dict[str, Any]:
    name = payload["name"]
    base_month = payload["base_month"]
    slots = payload["slots"]
    items = payload["items"]
    _validate_structure(base_month, slots, items)
    with write_txn(db_path, busy_timeout_ms) as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO baskets (name, base_month) VALUES (?, ?)",
                (name, base_month),
            )
        except sqlite3.IntegrityError as exc:
            raise conflict("basket_name_exists", f"basket name {name!r} is already in use") from exc
        basket_id = int(cursor.lastrowid)
        version_cursor = conn.execute(
            "INSERT INTO basket_versions (basket_id, version_no, parent_id, status, base_month) "
            "VALUES (?, 1, NULL, 'draft', ?)",
            (basket_id, base_month),
        )
        version_id = int(version_cursor.lastrowid)
        _insert_definition(conn, version_id, slots, items)
        basket = _basket_row(conn, basket_id)
        return _basket_summary(conn, basket)


def create_revision(
    db_path: Any, basket_id: int, busy_timeout_ms: int = 5_000
) -> dict[str, Any]:
    """Copy the current published version into a new editable draft."""
    with write_txn(db_path, busy_timeout_ms) as conn:
        basket = _basket_row(conn, basket_id)
        existing = conn.execute(
            "SELECT version_no FROM basket_versions WHERE basket_id = ? AND status = 'draft'",
            (basket_id,),
        ).fetchone()
        if existing is not None:
            raise conflict(
                "draft_exists",
                f"basket {basket_id} already has draft version {existing['version_no']}",
            )
        if basket["current_version_id"] is None:
            raise conflict(
                "no_published_version",
                f"basket {basket_id} has no published version to revise",
            )
        parent = conn.execute(
            "SELECT * FROM basket_versions WHERE id = ?", (basket["current_version_id"],)
        ).fetchone()
        max_no = conn.execute(
            "SELECT MAX(version_no) AS n FROM basket_versions WHERE basket_id = ?",
            (basket_id,),
        ).fetchone()["n"]
        version_no = int(max_no) + 1
        version_id = int(
            conn.execute(
                "INSERT INTO basket_versions (basket_id, version_no, parent_id, status, base_month) "
                "VALUES (?, ?, ?, 'draft', ?)",
                (basket_id, version_no, parent["id"], parent["base_month"]),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO basket_slots (basket_version_id, slot_key, name, weight_bp) "
            "SELECT ?, slot_key, name, weight_bp FROM basket_slots WHERE basket_version_id = ?",
            (version_id, parent["id"]),
        )
        conn.execute(
            "INSERT INTO basket_items "
            "(basket_version_id, slot_key, code, name, active_from, active_through, predecessor_code) "
            "SELECT ?, slot_key, code, name, active_from, active_through, predecessor_code "
            "FROM basket_items WHERE basket_version_id = ?",
            (version_id, parent["id"]),
        )
        conn.execute(
            "INSERT INTO prices (basket_version_id, item_code, month, price_cents) "
            "SELECT ?, item_code, month, price_cents FROM prices WHERE basket_version_id = ?",
            (version_id, parent["id"]),
        )
        basket = _basket_row(conn, basket_id)
        return _basket_summary(conn, basket)


def set_weight(
    db_path: Any,
    basket_id: int,
    slot_key: str,
    weight_bp: int,
    busy_timeout_ms: int = 5_000,
) -> dict[str, Any]:
    with write_txn(db_path, busy_timeout_ms) as conn:
        _basket_row(conn, basket_id)
        draft = _draft_row(conn, basket_id)
        cursor = conn.execute(
            "UPDATE basket_slots SET weight_bp = ? "
            "WHERE basket_version_id = ? AND slot_key = ?",
            (weight_bp, draft["id"], slot_key),
        )
        if cursor.rowcount == 0:
            raise not_found(
                "slot_not_found",
                f"draft of basket {basket_id} has no slot {slot_key!r}",
            )
        return _draft_detail(conn, basket_id, draft)


def upsert_prices(
    db_path: Any,
    basket_id: int,
    entries: list[dict[str, Any]],
    busy_timeout_ms: int = 5_000,
) -> dict[str, Any]:
    with write_txn(db_path, busy_timeout_ms) as conn:
        _basket_row(conn, basket_id)
        draft = _draft_row(conn, basket_id)
        known = {
            row["code"]: row
            for row in conn.execute(
                "SELECT code, active_from, active_through FROM basket_items "
                "WHERE basket_version_id = ?",
                (draft["id"],),
            )
        }
        seen: set[tuple[str, str]] = set()
        for entry in entries:
            key = (entry["item_code"], entry["month"])
            if key in seen:
                raise conflict(
                    "duplicate_quote",
                    f"duplicate quote for {entry['item_code']!r} in {entry['month']}",
                    {"item_code": entry["item_code"], "month": entry["month"]},
                )
            seen.add(key)
            item = known.get(entry["item_code"])
            if item is None:
                raise not_found(
                    "item_not_found",
                    f"draft of basket {basket_id} has no item {entry['item_code']!r}",
                )
            month = entry["month"]
            parse_month(month)
            if month < item["active_from"] or (
                item["active_through"] is not None and month > item["active_through"]
            ):
                raise invalid(
                    "quote_outside_active_range",
                    f"item {entry['item_code']!r} is not active in {month}",
                    {
                        "item_code": entry["item_code"],
                        "month": month,
                        "active_from": item["active_from"],
                        "active_through": item["active_through"],
                    },
                )
            conn.execute(
                "INSERT INTO prices (basket_version_id, item_code, month, price_cents) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (basket_version_id, item_code, month) "
                "DO UPDATE SET price_cents = excluded.price_cents",
                (draft["id"], entry["item_code"], entry["month"], entry["price_cents"]),
            )
        return _draft_detail(conn, basket_id, draft)


def delete_price(
    db_path: Any,
    basket_id: int,
    item_code: str,
    month: str,
    busy_timeout_ms: int = 5_000,
) -> dict[str, Any]:
    with write_txn(db_path, busy_timeout_ms) as conn:
        _basket_row(conn, basket_id)
        draft = _draft_row(conn, basket_id)
        parse_month(month)
        cursor = conn.execute(
            "DELETE FROM prices WHERE basket_version_id = ? AND item_code = ? AND month = ?",
            (draft["id"], item_code, month),
        )
        if cursor.rowcount == 0:
            raise not_found(
                "quote_not_found",
                f"draft of basket {basket_id} has no quote for {item_code!r} in {month}",
            )
        return _draft_detail(conn, basket_id, draft)


def replace_item(
    db_path: Any,
    basket_id: int,
    slot_key: str,
    month: str,
    new_code: str,
    new_name: str,
    old_price_cents: int,
    new_price_cents: int,
    busy_timeout_ms: int = 5_000,
) -> dict[str, Any]:
    """Swap the representative item of a slot in ``month``.

    The old item ends in ``month`` and the new one starts in ``month``; both
    quotes for that month are written atomically.  The slot weight is
    untouched, so the replacement never changes the basket weighting.
    """
    with write_txn(db_path, busy_timeout_ms) as conn:
        basket = _basket_row(conn, basket_id)
        draft = _draft_row(conn, basket_id)
        data = _load_data(conn, draft["id"], draft["base_month"])
        parse_month(month)
        if month_index(month) <= month_index(data.base_month):
            raise invalid(
                "replacement_before_first_relative",
                f"replacement month must be after the base month {data.base_month}",
            )
        if any(item.code == new_code for item in data.items):
            raise conflict(
                "duplicate_item_code",
                f"item code {new_code!r} already exists in this draft",
            )
        slot_items = data.items_for_slot(slot_key)
        if not slot_items:
            raise not_found(
                "slot_not_found",
                f"draft of basket {basket_id} has no slot {slot_key!r}",
            )
        current = slot_items[-1]
        if any(item.active_from == month for item in slot_items):
            raise conflict(
                "duplicate_replacement",
                f"slot {slot_key!r} already has an item starting in {month}",
            )
        if current.active_through is not None:
            raise conflict(
                "slot_timeline_closed",
                f"slot {slot_key!r} already has a later item; only the current open-ended item can be replaced",
            )
        if month_index(month) <= month_index(current.active_from):
            raise invalid(
                "replacement_in_start_month",
                f"replacement month {month} must be later than the current item's start month {current.active_from}",
            )
        previous_month = add_months(month, -1)
        latest = data.latest_month()
        if latest is not None and month_index(month) > month_index(latest) + 1:
            raise invalid(
                "replacement_would_leave_gap",
                f"replacement month {month} leaves a gap after {latest}",
            )
        if data.price(current.code, previous_month) is None:
            raise invalid(
                "missing_previous_quote",
                f"cannot replace in {month}: no quote for {current.code!r} in {previous_month}",
                {
                    "slot_key": slot_key,
                    "item_code": current.code,
                    "month": previous_month,
                },
            )
        try:
            conn.execute(
                "UPDATE basket_items SET active_through = ? WHERE id = ?",
                (month, _item_id(conn, draft["id"], current.code)),
            )
            conn.execute(
                "INSERT INTO basket_items "
                "(basket_version_id, slot_key, code, name, active_from, active_through, predecessor_code) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (draft["id"], slot_key, new_code, new_name, month, current.code),
            )
        except sqlite3.IntegrityError as exc:
            raise conflict(
                "duplicate_replacement",
                f"slot {slot_key!r} already has an item starting in {month}",
            ) from exc
        conn.execute(
            "INSERT INTO prices (basket_version_id, item_code, month, price_cents) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (basket_version_id, item_code, month) "
            "DO UPDATE SET price_cents = excluded.price_cents",
            (draft["id"], current.code, month, old_price_cents),
        )
        conn.execute(
            "INSERT INTO prices (basket_version_id, item_code, month, price_cents) "
            "VALUES (?, ?, ?, ?)",
            (draft["id"], new_code, month, new_price_cents),
        )
        return _draft_detail(conn, basket_id, draft)


def _item_id(conn: sqlite3.Connection, version_id: int, code: str) -> int:
    row = conn.execute(
        "SELECT id FROM basket_items WHERE basket_version_id = ? AND code = ?",
        (version_id, code),
    ).fetchone()
    if row is None:  # pragma: no cover - callers just loaded this item
        raise not_found("item_not_found", f"item {code!r} does not exist")
    return int(row["id"])


def publish(
    db_path: Any, basket_id: int, busy_timeout_ms: int = 5_000
) -> dict[str, Any]:
    """Validate, compute and atomically publish the current draft."""
    with write_txn(db_path, busy_timeout_ms) as conn:
        basket = _basket_row(conn, basket_id)
        draft = _draft_row(conn, basket_id)
        data = _load_data(conn, draft["id"], draft["base_month"])

        issues = validate_basket(data)
        if issues:
            raise ApiError(
                422,
                "publish_rejected",
                f"basket {basket_id} draft version {draft['version_no']} failed validation",
                {"issues": issues},
            )

        latest = data.latest_month()
        assert latest is not None  # validate_basket guarantees a price

        parent = None
        parent_points: dict[str, Point] = {}
        if draft["parent_id"] is not None:
            parent_row = conn.execute(
                "SELECT * FROM basket_versions WHERE id = ?", (draft["parent_id"],)
            ).fetchone()
            if parent_row is None:  # pragma: no cover - FK guarantees existence
                raise conflict("parent_missing", "draft parent version no longer exists")
            if parent_row["status"] != "published":  # pragma: no cover
                raise conflict("parent_not_published", "draft parent is not published")
            if parent_row["base_month"] != data.base_month:
                raise invalid(
                    "base_month_changed",
                    "a revision cannot change the base month",
                )
            parent = _load_data(conn, parent_row["id"], parent_row["base_month"])
            parent_latest = parent.latest_month()
            if parent_latest is not None and month_index(latest) < month_index(parent_latest):
                raise invalid(
                    "cannot_remove_months",
                    f"revision ends at {latest}, before published version's {parent_latest}",
                )
            parent_points = _load_points(conn, parent_row["id"])

        earliest = earliest_affected_month(parent, data)
        points = compute_points(data, parent, parent_points, earliest)

        definition = _definition(conn, draft["id"])
        payload = _snapshot_payload(
            basket["name"], draft["version_no"], definition, data.base_month, points
        )
        digest = snapshot_hash(payload)

        reused = sum(1 for p in points if p.origin == "reused")
        computed = len(points) - reused
        published_at = now_iso()

        conn.executemany(
            "INSERT INTO index_points "
            "(basket_version_id, month, mom_num, mom_den, index_num, index_den, origin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    draft["id"],
                    p.month,
                    str(p.mom.numerator) if p.mom is not None else None,
                    str(p.mom.denominator) if p.mom is not None else None,
                    str(p.index.numerator),
                    str(p.index.denominator),
                    p.origin,
                )
                for p in points
            ],
        )
        conn.execute(
            "UPDATE basket_versions SET status = 'published', published_at = ?, "
            "recomputed_from = ?, reused_point_count = ?, computed_point_count = ?, "
            "snapshot_sha256 = ? WHERE id = ?",
            (published_at, earliest, reused, computed, digest, draft["id"]),
        )
        conn.execute(
            "UPDATE baskets SET current_version_id = ? WHERE id = ?",
            (draft["id"], basket_id),
        )
        published_row = conn.execute(
            "SELECT * FROM basket_versions WHERE id = ?", (draft["id"],)
        ).fetchone()
        return _snapshot_response(
            conn,
            basket,
            published_row,
            points,
            digest,
            earliest,
            reused,
            computed,
            published_at,
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _draft_detail(
    conn: sqlite3.Connection, basket_id: int, draft: sqlite3.Row
) -> dict[str, Any]:
    return {
        "basket_id": basket_id,
        "version_no": draft["version_no"],
        "status": draft["status"],
        "base_month": draft["base_month"],
        **_definition(conn, draft["id"]),
    }


def list_baskets(db_path: Any, busy_timeout_ms: int = 5_000) -> list[dict[str, Any]]:
    with read_txn(db_path, busy_timeout_ms) as conn:
        rows = conn.execute("SELECT * FROM baskets ORDER BY id").fetchall()
        return [_basket_summary(conn, row) for row in rows]


def get_basket(
    db_path: Any, basket_id: int, busy_timeout_ms: int = 5_000
) -> dict[str, Any]:
    with read_txn(db_path, busy_timeout_ms) as conn:
        return _basket_summary(conn, _basket_row(conn, basket_id))


def get_draft(
    db_path: Any, basket_id: int, busy_timeout_ms: int = 5_000
) -> dict[str, Any]:
    with read_txn(db_path, busy_timeout_ms) as conn:
        _basket_row(conn, basket_id)
        return _draft_detail(conn, basket_id, _draft_row(conn, basket_id))


def _snapshot_response(
    conn: sqlite3.Connection,
    basket: sqlite3.Row,
    version: sqlite3.Row,
    points: list[Point],
    digest: str,
    recomputed_from: str | None,
    reused: int,
    computed: int,
    published_at: str | None,
) -> dict[str, Any]:
    definition = _definition(conn, version["id"])
    # Recompute the digest from the rows actually being returned so a caller
    # can detect storage corruption rather than trusting a stored string.
    recomputed_digest = snapshot_hash(
        _snapshot_payload(
            basket["name"],
            version["version_no"],
            definition,
            version["base_month"],
            points,
        )
    )
    return {
        "basket_id": basket["id"],
        "basket_name": basket["name"],
        "version_no": version["version_no"],
        "status": version["status"],
        "base_month": version["base_month"],
        "published_at": published_at,
        "recomputed_from": recomputed_from,
        "reused_point_count": reused,
        "computed_point_count": computed,
        "snapshot_sha256": digest,
        "integrity_ok": recomputed_digest == digest,
        "weight_sum_bp": sum(slot["weight_bp"] for slot in definition["slots"]),
        "slots": definition["slots"],
        "items": definition["items"],
        "prices": definition["prices"],
        "index": [point_to_dict(p) for p in points],
    }


def _snapshot_from_conn(
    conn: sqlite3.Connection, basket: sqlite3.Row, version: sqlite3.Row
) -> dict[str, Any]:
    points_map = _load_points(conn, version["id"])
    points = [points_map[m] for m in sorted(points_map)]
    return _snapshot_response(
        conn,
        basket,
        version,
        points,
        version["snapshot_sha256"] or "",
        version["recomputed_from"],
        version["reused_point_count"],
        version["computed_point_count"],
        version["published_at"],
    )


def get_snapshot(
    db_path: Any,
    basket_id: int,
    version_no: int,
    busy_timeout_ms: int = 5_000,
) -> dict[str, Any]:
    """Definition, prices and index points from a single committed snapshot."""
    with read_txn(db_path, busy_timeout_ms) as conn:
        basket = _basket_row(conn, basket_id)
        version = _published_row(conn, basket_id, version_no)
        return _snapshot_from_conn(conn, basket, version)


def get_current_snapshot(
    db_path: Any, basket_id: int, busy_timeout_ms: int = 5_000
) -> dict[str, Any]:
    with read_txn(db_path, busy_timeout_ms) as conn:
        basket = _basket_row(conn, basket_id)
        if basket["current_version_id"] is None:
            raise conflict(
                "no_published_version",
                f"basket {basket_id} has no published version",
            )
        version = conn.execute(
            "SELECT * FROM basket_versions WHERE id = ?",
            (basket["current_version_id"],),
        ).fetchone()
        if version["status"] != "published":  # pragma: no cover - invariant
            raise conflict("version_not_published", "current version is not published")
        return _snapshot_from_conn(conn, basket, version)


def get_index(
    db_path: Any,
    basket_id: int,
    version_no: int,
    from_month: str | None = None,
    to_month: str | None = None,
    busy_timeout_ms: int = 5_000,
) -> dict[str, Any]:
    with read_txn(db_path, busy_timeout_ms) as conn:
        _basket_row(conn, basket_id)
        version = _published_row(conn, basket_id, version_no)
        points_map = _load_points(conn, version["id"])
        months = sorted(points_map)
        if from_month is not None:
            parse_month(from_month)
        if to_month is not None:
            parse_month(to_month)
        if from_month and to_month and from_month > to_month:
            raise invalid("invalid_range", "from must not be later than to")
        selected = [
            m
            for m in months
            if (from_month is None or m >= from_month)
            and (to_month is None or m <= to_month)
        ]
        return {
            "basket_id": basket_id,
            "version_no": version["version_no"],
            "base_month": version["base_month"],
            "snapshot_sha256": version["snapshot_sha256"],
            "recomputed_from": version["recomputed_from"],
            "points": [
                {
                    "month": m,
                    "index": point_to_dict(points_map[m])["index"],
                    "mom": point_to_dict(points_map[m])["mom"],
                    "origin": points_map[m].origin,
                }
                for m in selected
            ],
        }
