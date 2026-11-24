"""HTTP-level tests for the continuation (minimum-switch chain) endpoint."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import store
from app.config import Settings
from app.db import initialise
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    initialise(db_path)
    app = create_app(Settings(db_path=db_path))
    with TestClient(app) as test_client:
        yield test_client


def _one_slot_basket(client, name="接续篮子"):
    payload = {
        "name": name,
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
    }
    created = client.post("/baskets", json=payload)
    assert created.status_code == 201, created.text
    basket_id = created.json()["id"]
    assert client.post(f"/baskets/{basket_id}/publish").status_code == 201
    assert client.post(f"/baskets/{basket_id}/revisions").status_code == 201
    return basket_id


def _rice_through_march(client, basket_id):
    response = client.put(
        f"/baskets/{basket_id}/draft/prices",
        json={
            "entries": [
                {"item_code": "rice", "month": "2025-02", "price_cents": 1100},
                {"item_code": "rice", "month": "2025-03", "price_cents": 1210},
                {"item_code": "bus", "month": "2025-02", "price_cents": 200},
                {"item_code": "bus", "month": "2025-03", "price_cents": 210},
            ]
        },
    )
    assert response.status_code == 200, response.text


def _chain_body() -> dict:
    return {
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


def test_continuation_writes_minimum_switch_chain_and_publishes(client):
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)

    response = client.post(
        f"/baskets/{basket_id}/draft/continuation", json=_chain_body()
    )
    assert response.status_code == 201, response.text
    body = response.json()
    plan = body["plan"]
    # corn reaches 2025-07 in one switch, beating the two-switch path.
    assert plan["replacement_count"] == 1
    assert plan["replacement_months"] == ["2025-03"]
    assert [segment["item_code"] for segment in plan["segments"]] == ["rice", "corn"]
    corn = plan["segments"][-1]
    assert corn["active_from"] == "2025-03" and corn["active_through"] is None

    draft = body["draft"]
    items = {item["code"]: item for item in draft["items"]}
    assert items["rice"]["active_through"] == "2025-03"
    assert items["corn"]["active_from"] == "2025-03"
    assert items["corn"]["predecessor_code"] == "rice"
    # The whole chain, including every candidate quote, is in the draft.
    corn_prices = {p["month"]: p["price_cents"] for p in draft["prices"] if p["item_code"] == "corn"}
    assert corn_prices == {
        "2025-03": 800, "2025-04": 820, "2025-05": 840,
        "2025-06": 860, "2025-07": 880,
    }
    # The overlap month keeps both quotes.
    march = {(p["item_code"], p["month"]): p["price_cents"] for p in draft["prices"]}
    assert march[("rice", "2025-03")] == 1210 and march[("corn", "2025-03")] == 800

    # The resulting draft is publishable as-is, with the index continuous
    # (the other slot still needs its monthly quotes filled in).
    client.put(
        f"/baskets/{basket_id}/draft/prices",
        json={"entries": [
            {"item_code": "bus", "month": month, "price_cents": 220 + index}
            for index, month in enumerate(
                ["2025-04", "2025-05", "2025-06", "2025-07"]
            )
        ]},
    )
    published = client.post(f"/baskets/{basket_id}/publish")
    assert published.status_code == 201, published.text
    snap = published.json()
    assert [point["month"] for point in snap["index"]][-1] == "2025-07"
    timeline = {i["code"]: (i["active_from"], i["active_through"], i["predecessor_code"])
                for i in snap["items"]}
    assert timeline["rice"] == ("2025-01", "2025-03", None)
    assert timeline["corn"] == ("2025-03", None, "rice")


def test_continuation_uses_multiple_switches_when_needed(client):
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)
    body = _chain_body()
    body["candidates"].pop(2)  # remove corn; noodles -> bread is the only path
    response = client.post(f"/baskets/{basket_id}/draft/continuation", json=body)
    assert response.status_code == 201, response.text
    plan = response.json()["plan"]
    assert plan["replacement_count"] == 2
    assert plan["replacement_months"] == ["2025-03", "2025-05"]
    assert [segment["item_code"] for segment in plan["segments"]] == [
        "rice", "noodles", "bread",
    ]
    middle = plan["segments"][1]
    assert middle["active_through"] == "2025-05"

    # Fill the transport slot for the new months and publish.
    client.put(
        f"/baskets/{basket_id}/draft/prices",
        json={"entries": [
            {"item_code": "bus", "month": month, "price_cents": 220 + index}
            for index, month in enumerate(
                ["2025-04", "2025-05", "2025-06", "2025-07"]
            )
        ]},
    )
    published = client.post(f"/baskets/{basket_id}/publish")
    assert published.status_code == 201, published.text


def test_no_viable_chain_is_rejected_without_touching_draft(client):
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)
    before = client.get(f"/baskets/{basket_id}/draft").json()
    body = _chain_body()
    body["target_month"] = "2025-12"  # no candidate reaches December
    response = client.post(f"/baskets/{basket_id}/draft/continuation", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_continuation_chain"
    after = client.get(f"/baskets/{basket_id}/draft").json()
    assert before == after


def test_rejects_when_current_quote_timeline_has_a_gap(client):
    basket_id = _one_slot_basket(client)
    # January (the start month) and March quotes exist, but February is missing.
    client.put(
        f"/baskets/{basket_id}/draft/prices",
        json={"entries": [
            {"item_code": "rice", "month": "2025-02", "price_cents": 1100},
            {"item_code": "rice", "month": "2025-03", "price_cents": 1210},
        ]},
    )
    client.delete(f"/baskets/{basket_id}/draft/prices/rice/2025-02")
    response = client.post(
        f"/baskets/{basket_id}/draft/continuation", json=_chain_body()
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "current_quote_timeline_not_continuous"


def test_rejects_candidate_code_already_used_in_draft(client):
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)
    body = _chain_body()
    body["candidates"][0]["code"] = "rice"
    response = client.post(f"/baskets/{basket_id}/draft/continuation", json=body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "candidate_code_already_used"


def test_rejects_candidate_with_internal_gap(client):
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)
    body = _chain_body()
    del body["candidates"][2]["prices"]["2025-05"]
    response = client.post(f"/baskets/{basket_id}/draft/continuation", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "candidate_quote_timeline_not_continuous"


def test_rejects_candidate_quoted_past_target_month(client):
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)
    body = _chain_body()
    body["candidates"][2]["prices"]["2025-08"] = 900
    response = client.post(f"/baskets/{basket_id}/draft/continuation", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "candidate_quote_beyond_target"


def test_rejects_target_month_not_later_than_last_quote(client):
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)
    body = _chain_body()
    body["target_month"] = "2025-03"
    response = client.post(f"/baskets/{basket_id}/draft/continuation", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "target_month_not_later_than_last_quote"


def test_rejects_unknown_slot_and_duplicate_candidate(client):
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)
    body = _chain_body()
    body["slot_key"] = "energy"
    response = client.post(f"/baskets/{basket_id}/draft/continuation", json=body)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "slot_not_found"

    body = _chain_body()
    body["candidates"][1]["code"] = body["candidates"][0]["code"]
    response = client.post(f"/baskets/{basket_id}/draft/continuation", json=body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "duplicate_candidate_code"


def test_more_than_ten_candidates_is_a_request_error(client):
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)
    body = _chain_body()
    body["candidates"] = [
        {"code": f"item{i}", "name": f"商品{i}", "prices": {"2025-03": 100 + i}}
        for i in range(11)
    ]
    response = client.post(f"/baskets/{basket_id}/draft/continuation", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_invalid"


def test_continuation_requires_a_draft(client):
    # A fresh basket only has its initial draft; publish it so no draft remains.
    created = client.post(
        "/baskets",
        json={
            "name": "无草稿篮子", "base_month": "2025-01",
            "slots": [{"slot_key": "food", "name": "食品", "weight_bp": 10000}],
            "items": [{"code": "rice", "name": "大米", "slot_key": "food",
                       "active_from": "2025-01", "prices": {"2025-01": 1000}}],
        },
    )
    basket_id = created.json()["id"]
    client.post(f"/baskets/{basket_id}/publish")
    response = client.post(
        f"/baskets/{basket_id}/draft/continuation", json=_chain_body()
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_draft"


def test_concurrent_slot_edit_makes_write_back_refuse_and_roll_back(
    client, monkeypatch
):
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)
    before = client.get(f"/baskets/{basket_id}/draft").json()

    real_fingerprint = store._slot_fingerprint
    calls = {"n": 0}

    def flaky_fingerprint(conn, draft, slot_key):
        digest = real_fingerprint(conn, draft, slot_key)
        calls["n"] += 1
        if calls["n"] == 2:
            # Simulate another writer changing the slot between planning and
            # the write-back re-check (the read phase already completed).
            return "0" * 64
        return digest

    monkeypatch.setattr(store, "_slot_fingerprint", flaky_fingerprint)
    response = client.post(
        f"/baskets/{basket_id}/draft/continuation", json=_chain_body()
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "slot_changed_concurrently"

    monkeypatch.undo()
    # Nothing was written: the whole request rolled back.
    after = client.get(f"/baskets/{basket_id}/draft").json()
    assert after == before
    assert "corn" not in {item["code"] for item in after["items"]}


def test_publishing_between_plan_and_write_is_rejected(client, monkeypatch):
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)
    before = client.get(f"/baskets/{basket_id}/draft").json()

    real_fingerprint = store._slot_fingerprint
    calls = {"n": 0}

    def publish_then_hash(conn, draft, slot_key):
        calls["n"] += 1
        if calls["n"] == 2:
            # The second fingerprint call happens inside the write transaction:
            # emulate the draft being published while the request was in flight.
            conn.execute(
                "UPDATE basket_versions SET status = 'published' WHERE id = ?",
                (draft["id"],),
            )
        return real_fingerprint(conn, draft, slot_key)

    monkeypatch.setattr(store, "_slot_fingerprint", publish_then_hash)
    response = client.post(
        f"/baskets/{basket_id}/draft/continuation", json=_chain_body()
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] in {
        "draft_published_concurrently",
        "continuation_write_conflict",
    }
    monkeypatch.undo()

    after = client.get(f"/baskets/{basket_id}/draft").json()
    assert after == before


def test_concurrent_continuation_and_price_entry_serialise_without_corruption(client):
    """A continuation racing a normal price entry either wins or loses; the
    loser must be rejected and the committed draft must stay self-consistent.
    """
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)
    body = _chain_body()

    barrier = threading.Barrier(2)
    results: list[tuple[int, str]] = []

    def run_continuation() -> None:
        barrier.wait()
        response = client.post(
            f"/baskets/{basket_id}/draft/continuation", json=body
        )
        results.append((response.status_code, response.json()["error"]["code"]
                        if response.status_code != 201 else "ok"))

    def run_price_entry() -> None:
        barrier.wait()
        response = client.put(
            f"/baskets/{basket_id}/draft/prices",
            json={"entries": [
                {"item_code": "rice", "month": "2025-03", "price_cents": 1300},
            ]},
        )
        results.append((response.status_code, "ok" if response.status_code == 200
                        else response.json()["error"]["code"]))

    threads = [threading.Thread(target=run_continuation),
               threading.Thread(target=run_price_entry)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert all(status in (200, 201, 409) for status, _ in results)
    draft = client.get(f"/baskets/{basket_id}/draft").json()
    item_codes = {item["code"] for item in draft["items"]}
    if any(code == "ok" and status == 201 for status, code in results):
        # Continuation won: the full chain is present and no rice quote was
        # overwritten by the racing entry.
        assert {"rice", "corn"} <= item_codes
        rice_march = next(
            p["price_cents"] for p in draft["prices"]
            if p["item_code"] == "rice" and p["month"] == "2025-03"
        )
        assert rice_march == 1210
    else:
        # Continuation lost the race: nothing of the chain leaked in.
        assert item_codes == {"rice", "bus"}


def test_editing_another_slot_does_not_invalidate_the_chain(client):
    """The optimistic check is scoped to the requested slot: a price recorded
    for a different slot leaves both operations valid.
    """
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)

    # Record a transport quote first; the food fingerprint must be unaffected.
    client.put(
        f"/baskets/{basket_id}/draft/prices",
        json={"entries": [
            {"item_code": "bus", "month": "2025-04", "price_cents": 220},
        ]},
    )
    response = client.post(
        f"/baskets/{basket_id}/draft/continuation", json=_chain_body()
    )
    assert response.status_code == 201, response.text
    # And a later edit to the other slot still works on the same draft.
    follow_up = client.put(
        f"/baskets/{basket_id}/draft/prices",
        json={"entries": [
            {"item_code": "bus", "month": "2025-05", "price_cents": 242},
        ]},
    )
    assert follow_up.status_code == 200
    draft = follow_up.json()
    assert "corn" in {item["code"] for item in draft["items"]}


def test_continuation_works_after_an_existing_replacement_history(client):
    """The slot may already carry a replacement timeline; the continuation
    applies to the current open-ended item and never reuses retired items.
    """
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)
    # First replacement via the classic endpoint: rice -> noodles in March.
    replacement = client.post(
        f"/baskets/{basket_id}/draft/replacements",
        json={
            "slot_key": "food",
            "month": "2025-03",
            "new_code": "noodles",
            "new_name": "面条",
            "old_price_cents": 1210,
            "new_price_cents": 500,
        },
    )
    assert replacement.status_code == 201, replacement.text
    client.put(
        f"/baskets/{basket_id}/draft/prices",
        json={"entries": [
            {"item_code": "noodles", "month": "2025-04", "price_cents": 550},
            {"item_code": "noodles", "month": "2025-05", "price_cents": 600},
        ]},
    )
    # noodles now stops in May; extend it to July.  rice is a retired code and
    # therefore cannot be offered again.
    body = {
        "slot_key": "food",
        "target_month": "2025-07",
        "candidates": [
            {"code": "bread", "name": "面包",
             "prices": {"2025-05": 300, "2025-06": 310, "2025-07": 320}},
            {"code": "rice", "name": "大米(旧)",
             "prices": {"2025-05": 999}},
        ],
    }
    rejected = client.post(f"/baskets/{basket_id}/draft/continuation", json=body)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "candidate_code_already_used"

    body["candidates"].pop(1)
    response = client.post(f"/baskets/{basket_id}/draft/continuation", json=body)
    assert response.status_code == 201, response.text
    plan = response.json()["plan"]
    assert plan["replacement_months"] == ["2025-05"]
    assert [segment["item_code"] for segment in plan["segments"]] == [
        "noodles", "bread",
    ]
    draft = response.json()["draft"]
    timeline = {item["code"]: (item["active_from"], item["active_through"],
                               item["predecessor_code"]) for item in draft["items"]}
    assert timeline["rice"] == ("2025-01", "2025-03", None)
    assert timeline["noodles"] == ("2025-03", "2025-05", "rice")
    assert timeline["bread"] == ("2025-05", None, "noodles")


def test_switch_month_relative_uses_old_item_then_new_item(client):
    """Index semantics of the chain match the replacement rules."""
    basket_id = _one_slot_basket(client)
    _rice_through_march(client, basket_id)
    body = _chain_body()
    body["target_month"] = "2025-05"
    body["candidates"] = [body["candidates"][0]]  # noodles only, through May
    response = client.post(f"/baskets/{basket_id}/draft/continuation", json=body)
    assert response.status_code == 201, response.text

    # Transport quotes for the same months.
    client.put(
        f"/baskets/{basket_id}/draft/prices",
        json={"entries": [
            {"item_code": "bus", "month": "2025-04", "price_cents": 220},
            {"item_code": "bus", "month": "2025-05", "price_cents": 242},
        ]},
    )
    snap = client.post(f"/baskets/{basket_id}/publish").json()
    from fractions import Fraction

    points = {p["month"]: p for p in snap["index"]}
    # March (switch month): old item movement rice 1210/1100, bus 210/200.
    expected_mar = (
        Fraction(6, 10) * Fraction(1210, 1100)
        + Fraction(4, 10) * Fraction(210, 200)
    )
    assert Fraction(int(points["2025-03"]["mom_num"]),
                   int(points["2025-03"]["mom_den"])) == expected_mar
    # April: new item movement noodles 550/500.
    expected_apr = (
        Fraction(6, 10) * Fraction(550, 500)
        + Fraction(4, 10) * Fraction(220, 210)
    )
    assert Fraction(int(points["2025-04"]["mom_num"]),
                   int(points["2025-04"]["mom_den"])) == expected_apr
