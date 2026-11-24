#!/usr/bin/env python3
"""End-to-end demo of the representative-item continuation endpoint.

Start the API first (any of):

    docker compose up --build
    PPI_DB_PATH=./data/ppi.sqlite3 python -m uvicorn app.main:app --port 8000

Then:

    python examples/continuation_demo.py                 # http://127.0.0.1:8000
    python examples/continuation_demo.py http://host:8000

The scenario: the representative item of the ``food`` slot stops reporting
after 2025-03.  The statistician offers up to ten candidate items with their
month-by-month quotes and asks the service to extend the slot to a later
target month with the fewest replacements.  The whole solved chain is written
back to the draft in one transaction; the script then publishes it and checks
the index continuity rules.  Exits non-zero on failure.
"""

from __future__ import annotations

import json
import sys
import time
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
    run_id = str(int(time.time()))

    status, basket = call(
        "POST",
        "/baskets",
        {
            "name": f"接续演示篮子-{run_id}",
            "base_month": "2025-01",
            "slots": [
                {"slot_key": "food", "name": "食品", "weight_bp": 6000},
                {"slot_key": "transport", "name": "交通", "weight_bp": 4000},
            ],
            "items": [
                {"code": "rice", "name": "大米", "slot_key": "food",
                 "active_from": "2025-01", "prices": {"2025-01": 1000}},
                {"code": "bus", "name": "公交", "slot_key": "transport",
                 "active_from": "2025-01", "prices": {"2025-01": 200}},
            ],
        },
    )
    expect(status == 201, f"create basket -> {status}")
    basket_id = basket["id"]
    expect(call("POST", f"/baskets/{basket_id}/publish")[0] == 201, "publish v1")
    expect(call("POST", f"/baskets/{basket_id}/revisions")[0] == 201, "open revision draft")

    # The current representative has continuous quotes through 2025-03 and then
    # stops reporting.
    status, _ = call(
        "PUT",
        f"/baskets/{basket_id}/draft/prices",
        {"entries": [
            {"item_code": "rice", "month": "2025-02", "price_cents": 1100},
            {"item_code": "rice", "month": "2025-03", "price_cents": 1210},
            {"item_code": "bus", "month": "2025-02", "price_cents": 200},
            {"item_code": "bus", "month": "2025-03", "price_cents": 210},
        ]},
    )
    expect(status == 200, "record rice quotes continuously through 2025-03")

    # corn reaches the target in one switch; noodles->bread would need two.
    continuation_body = {
        "slot_key": "food",
        "target_month": "2025-07",
        "candidates": [
            {"code": "noodles", "name": "面条",
             "prices": {"2025-03": 500, "2025-04": 550, "2025-05": 600}},
            {"code": "bread", "name": "面包",
             "prices": {"2025-05": 300, "2025-06": 310, "2025-07": 320}},
            {"code": "corn", "name": "玉米",
             "prices": {"2025-03": 800, "2025-04": 820, "2025-05": 840,
                        "2025-06": 860, "2025-07": 880}},
        ],
    }
    status, answer = call(
        "POST", f"/baskets/{basket_id}/draft/continuation", continuation_body
    )
    expect(status == 201, f"continuation request accepted -> {status}")
    plan = answer["plan"]
    expect(plan["replacement_count"] == 1, "solver minimises switches (1)")
    expect(plan["replacement_months"] == ["2025-03"], "switch month is the last old-quote month")
    expect(
        [segment["item_code"] for segment in plan["segments"]] == ["rice", "corn"],
        "chain segments are rice -> corn",
    )

    # An unsolvable request is rejected and changes nothing.  It needs a fresh
    # draft of its own: the previous request already extended this slot.
    status, second_basket = call(
        "POST",
        "/baskets",
        {
            "name": f"接续演示篮子-无解-{run_id}",
            "base_month": "2025-01",
            "slots": [
                {"slot_key": "food", "name": "食品", "weight_bp": 10000},
            ],
            "items": [
                {"code": "rice", "name": "大米", "slot_key": "food",
                 "active_from": "2025-01",
                 "prices": {"2025-01": 1000, "2025-02": 1100, "2025-03": 1210}},
            ],
        },
    )
    expect(status == 201, f"create second basket -> {status}")
    second_id = second_basket["id"]
    call("POST", f"/baskets/{second_id}/publish")
    call("POST", f"/baskets/{second_id}/revisions")
    unsolvable = dict(continuation_body)
    unsolvable["target_month"] = "2025-12"
    status, error = call(
        "POST", f"/baskets/{second_id}/draft/continuation", unsolvable
    )
    expect(status == 422, f"unreachable target rejected -> {status}")
    expect(error["error"]["code"] == "no_continuation_chain", "error code no_continuation_chain")
    untouched = call("GET", f"/baskets/{second_id}/draft")[1]
    expect(
        {item["code"] for item in untouched["items"]} == {"rice"},
        "rejected request left no partial items in the draft",
    )

    # Fill the other slot and publish: the chain must validate as a normal
    # replacement timeline.
    call(
        "PUT",
        f"/baskets/{basket_id}/draft/prices",
        {"entries": [
            {"item_code": "bus", "month": month, "price_cents": 220 + index}
            for index, month in enumerate(
                ["2025-04", "2025-05", "2025-06", "2025-07"]
            )
        ]},
    )
    status, snapshot = call("POST", f"/baskets/{basket_id}/publish")
    expect(status == 201, f"publish draft containing the chain -> {status}")
    months = [point["month"] for point in snapshot["index"]]
    expect(months[0] == "2025-01" and months[-1] == "2025-07",
            f"index runs continuously {months[0]}..{months[-1]}")
    switch = next(p for p in snapshot["index"] if p["month"] == "2025-03")
    after = next(p for p in snapshot["index"] if p["month"] == "2025-04")
    expect(switch["mom"] is not None and after["mom"] is not None,
           "every month after the base still has a month-on-month relative")

    print("\nAll continuation demo checks passed.")


if __name__ == "__main__":
    main()
