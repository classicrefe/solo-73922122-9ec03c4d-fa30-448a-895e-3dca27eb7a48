#!/usr/bin/env python3
"""Runnable end-to-end example against a live server.

Start the API first (any of):

    docker compose up --build
    PPI_DB_PATH=./data/ppi.sqlite3 python -m uvicorn app.main:app --port 8000

Then:

    python examples/smoke.py                 # uses http://127.0.0.1:8000
    python examples/smoke.py http://host:8000

The script prints the index after each publish and asserts the rejection
cases keep the previous version live.  Exits non-zero on failure.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    payload = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAILED: {message}")
    print(f"  ok  {message}")


def main() -> None:
    print(f"Target: {BASE}")

    status, basket = call(
        "POST",
        "/baskets",
        {
            "name": "城市居民消费篮子 smoke",
            "base_month": "2025-01",
            "slots": [
                {"slot_key": "food", "name": "食品", "weight_bp": 6000},
                {"slot_key": "transport", "name": "交通", "weight_bp": 4000},
            ],
            "items": [
                {
                    "code": "rice",
                    "name": "大米",
                    "slot_key": "food",
                    "active_from": "2025-01",
                    "prices": {"2025-01": 1000},
                },
                {
                    "code": "bus",
                    "name": "公交",
                    "slot_key": "transport",
                    "active_from": "2025-01",
                    "prices": {"2025-01": 200},
                },
            ],
        },
    )
    expect(status == 201, f"create basket -> {status}")
    basket_id = basket["id"]

    status, v1 = call("POST", f"/baskets/{basket_id}/publish")
    expect(status == 201, f"publish v1 -> {status}")
    expect(v1["index"][0]["index"] == "100.000000", "base month index is 100.000000")

    call("POST", f"/baskets/{basket_id}/revisions")
    call(
        "PUT",
        f"/baskets/{basket_id}/draft/prices",
        {
            "entries": [
                {"item_code": "rice", "month": "2025-02", "price_cents": 1100},
                {"item_code": "bus", "month": "2025-02", "price_cents": 200},
                {"item_code": "rice", "month": "2025-03", "price_cents": 1210},
                {"item_code": "bus", "month": "2025-03", "price_cents": 220},
            ]
        },
    )
    status, v2 = call("POST", f"/baskets/{basket_id}/publish")
    expect(status == 201, f"publish v2 -> {status}")
    levels = [p["index"] for p in v2["index"]]
    expect(levels == ["100.000000", "106.000000", "116.600000"], f"chained levels {levels}")

    # --- rejection paths -------------------------------------------------
    call("POST", f"/baskets/{basket_id}/revisions")
    call(
        "PUT",
        f"/baskets/{basket_id}/draft/prices",
        {"entries": [{"item_code": "rice", "month": "2025-04", "price_cents": 1210}]},
    )
    status, error = call("POST", f"/baskets/{basket_id}/publish")
    expect(status == 422, f"missing bus quote rejected -> {status}")
    expect(error["error"]["code"] == "publish_rejected", "error code is publish_rejected")
    status, current = call("GET", f"/baskets/{basket_id}/snapshot")
    expect(current["version_no"] == 2, "previous version 2 still current after rejection")

    call(
        "PUT",
        f"/baskets/{basket_id}/draft/slots/food/weight",
        {"weight_bp": 5000},
    )
    status, error = call("POST", f"/baskets/{basket_id}/publish")
    expect(status == 422, f"weight sum 9000 rejected -> {status}")
    expect(
        any(i["code"] == "weight_sum_not_10000" for i in error["error"]["details"]["issues"]),
        "weight_sum_not_10000 reported",
    )
    call(
        "PUT",
        f"/baskets/{basket_id}/draft/slots/food/weight",
        {"weight_bp": 6000},
    )

    # --- replacement with overlapping quotes -----------------------------
    call(
        "PUT",
        f"/baskets/{basket_id}/draft/prices",
        {
            "entries": [
                {"item_code": "rice", "month": "2025-04", "price_cents": 1250},
                {"item_code": "bus", "month": "2025-04", "price_cents": 220},
            ]
        },
    )
    status, _ = call(
        "POST",
        f"/baskets/{basket_id}/draft/replacements",
        {
            "slot_key": "food",
            "month": "2025-04",
            "new_code": "noodles",
            "new_name": "面条",
            "old_price_cents": 1250,
            "new_price_cents": 500,
        },
    )
    expect(status == 201, f"replace rice with noodles in April -> {status}")
    call(
        "PUT",
        f"/baskets/{basket_id}/draft/prices",
        {
            "entries": [
                {"item_code": "noodles", "month": "2025-05", "price_cents": 550},
                {"item_code": "bus", "month": "2025-05", "price_cents": 242},
            ]
        },
    )
    status, v3 = call("POST", f"/baskets/{basket_id}/publish")
    expect(status == 201, f"publish v3 with replacement -> {status}")
    expect(v3["recomputed_from"] == "2025-04", "v3 recomputed from replacement month")

    # --- correction recomputes only from the affected month --------------
    call("POST", f"/baskets/{basket_id}/revisions")
    call(
        "PUT",
        f"/baskets/{basket_id}/draft/prices",
        {"entries": [{"item_code": "rice", "month": "2025-03", "price_cents": 1200}]},
    )
    status, v4 = call("POST", f"/baskets/{basket_id}/publish")
    expect(status == 201, f"publish v4 correction -> {status}")
    expect(v4["recomputed_from"] == "2025-03", "v4 recomputed_from is 2025-03")
    expect(v4["reused_point_count"] == 2, "v4 reused the two untouched points")
    origins = {p["month"]: p["origin"] for p in v4["index"]}
    expect(
        origins == {
            "2025-01": "reused",
            "2025-02": "reused",
            "2025-03": "computed",
            "2025-04": "computed",
            "2025-05": "computed",
        },
        f"v4 point origins {origins}",
    )

    status, old = call("GET", f"/baskets/{basket_id}/versions/3/snapshot")
    expect(status == 200 and old["version_no"] == 3, "historical version 3 still queryable")
    status, current = call("GET", f"/baskets/{basket_id}/snapshot")
    expect(current["version_no"] == 4, "current snapshot is version 4")
    expect(
        current["snapshot_sha256"] != old["snapshot_sha256"],
        "versions have distinct snapshot hashes",
    )

    print("\nAll smoke checks passed.")


if __name__ == "__main__":
    main()
