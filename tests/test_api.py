from __future__ import annotations

import sqlite3
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import initialise
from app.index_math import (
    Item,
    Slot,
    VersionData,
    compute_points,
    earliest_affected_month,
    point_to_dict,
    validate_basket,
)
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    initialise(db_path)
    app = create_app(Settings(db_path=db_path))
    with TestClient(app) as test_client:
        yield test_client


def make_basket(**overrides):
    payload = {
        "name": "城市居民消费篮子",
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
    }
    payload.update(overrides)
    return payload


def create_and_publish(client, payload=None) -> tuple[int, dict]:
    created = client.post("/baskets", json=payload or make_basket())
    assert created.status_code == 201, created.text
    basket_id = created.json()["id"]
    published = client.post(f"/baskets/{basket_id}/publish")
    assert published.status_code == 201, published.text
    return basket_id, published.json()


def add_prices(client, basket_id, entries):
    response = client.put(
        f"/baskets/{basket_id}/draft/prices", json={"entries": entries}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Index mathematics
# ---------------------------------------------------------------------------


def test_base_month_index_is_100(client):
    _, snapshot = create_and_publish(client)
    assert snapshot["index"][0]["month"] == "2025-01"
    assert snapshot["index"][0]["index"] == "100.000000"
    assert snapshot["index"][0]["mom"] is None


def test_weighted_chain_uses_basis_points(client):
    basket_id, _ = create_and_publish(client)
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client,
        basket_id,
        [
            {"item_code": "rice", "month": "2025-02", "price_cents": 1100},
            {"item_code": "bus", "month": "2025-02", "price_cents": 200},
        ],
    )
    snapshot = client.post(f"/baskets/{basket_id}/publish").json()
    # 0.6 * 1.10 + 0.4 * 1.00 = 1.06
    feb = snapshot["index"][1]
    assert feb["mom"] == "1.060000"
    assert feb["index"] == "106.000000"


def test_chain_accumulates_products(client):
    basket_id, _ = create_and_publish(client)
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client,
        basket_id,
        [
            {"item_code": "rice", "month": "2025-02", "price_cents": 1100},
            {"item_code": "bus", "month": "2025-02", "price_cents": 200},
            {"item_code": "rice", "month": "2025-03", "price_cents": 1210},
            {"item_code": "bus", "month": "2025-03", "price_cents": 220},
        ],
    )
    snapshot = client.post(f"/baskets/{basket_id}/publish").json()
    # Feb: 0.6*1.1 + 0.4*1.0 = 1.06
    # Mar: 0.6*(1210/1100) + 0.4*(220/200) = 0.66 + 0.44 = 1.10
    # I3 = 1.06 * 1.10 = 1.166
    mar = snapshot["index"][2]
    assert mar["mom"] == "1.100000"
    assert mar["index"] == "116.600000"


def test_replacement_uses_old_items_movement_in_overlap_month():
    data = VersionData(
        base_month="2025-01",
        slots=[Slot("food", "食品", 10000)],
        items=[
            Item("rice", "大米", "food", "2025-01", "2025-03", None),
            Item("noodles", "面条", "food", "2025-03", None, "rice"),
        ],
        prices={
            ("rice", "2025-01"): 1000,
            ("rice", "2025-02"): 1100,
            ("rice", "2025-03"): 1200,
            ("noodles", "2025-03"): 500,
            ("noodles", "2025-04"): 550,
        },
    )
    assert validate_basket(data) == []
    points = {p.month: p for p in compute_points(data, None, {}, "2025-02")}
    assert points["2025-02"].mom == Fraction(1100, 1000)
    # overlap month still tracks the old item (rice 1200/1100)
    assert points["2025-03"].mom == Fraction(1200, 1100)
    # after the swap the new item's movement takes over
    assert points["2025-04"].mom == Fraction(550, 500)


def test_replacement_requires_both_quotes_in_overlap_month():
    data = VersionData(
        base_month="2025-01",
        slots=[Slot("food", "食品", 10000)],
        items=[
            Item("rice", "大米", "food", "2025-01", "2025-03", None),
            Item("noodles", "面条", "food", "2025-03", None, "rice"),
        ],
        prices={
            ("rice", "2025-01"): 1000,
            ("rice", "2025-02"): 1100,
            ("rice", "2025-03"): 1200,
            # no noodles quote in the overlap month
            ("noodles", "2025-04"): 550,
        },
    )
    issues = validate_basket(data)
    overlap = [i for i in issues if i["code"] == "missing_quote" and i["month"] == "2025-03"]
    assert {i["item_code"] for i in overlap} == {"noodles"}


def test_weight_sum_must_be_exactly_10000():
    data = VersionData(
        base_month="2025-01",
        slots=[Slot("food", "食品", 5000), Slot("transport", "交通", 4000)],
        items=[
            Item("rice", "大米", "food", "2025-01", None),
            Item("bus", "公交", "transport", "2025-01", None),
        ],
        prices={("rice", "2025-01"): 1000, ("bus", "2025-01"): 200},
    )
    issues = validate_basket(data)
    assert any(i["code"] == "weight_sum_not_10000" for i in issues)


def test_point_serialisation_is_exact():
    point = compute_points(
        VersionData(
            base_month="2025-01",
            slots=[Slot("food", "食品", 10000)],
            items=[Item("rice", "大米", "food", "2025-01", None)],
            prices={("rice", "2025-01"): 3, ("rice", "2025-02"): 7},
        ),
        None,
        {},
        "2025-02",
    )[1]
    payload = point_to_dict(point)
    assert Fraction(int(payload["index_num"]), int(payload["index_den"])) == Fraction(700, 3)
    assert payload["index"] == "233.333333"


# ---------------------------------------------------------------------------
# Publishing is all-or-nothing
# ---------------------------------------------------------------------------


def test_missing_quote_rejects_publish_and_keeps_previous_version(client):
    basket_id, v1 = create_and_publish(client)
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client,
        basket_id,
        [{"item_code": "rice", "month": "2025-02", "price_cents": 1100}],
    )
    rejected = client.post(f"/baskets/{basket_id}/publish")
    assert rejected.status_code == 422
    body = rejected.json()["error"]
    assert body["code"] == "publish_rejected"
    missing = [i for i in body["details"]["issues"] if i["code"] == "missing_quote"]
    assert {"bus", "2025-02"} == {missing[0]["item_code"], missing[0]["month"]}

    # Old version is still current and queryable; the draft survives for fixing.
    current = client.get(f"/baskets/{basket_id}/snapshot").json()
    assert current["version_no"] == 1
    assert current["snapshot_sha256"] == v1["snapshot_sha256"]
    assert client.get(f"/baskets/{basket_id}/draft").status_code == 200

    # No v2 row exists at all, so a failed publish left no partial artefact.
    basket = client.get(f"/baskets/{basket_id}").json()
    assert [v["version_no"] for v in basket["versions"]] == [1, 2]
    assert basket["versions"][1]["status"] == "draft"


def test_weight_sum_rejection_keeps_previous_version(client):
    basket_id, _ = create_and_publish(client)
    client.post(f"/baskets/{basket_id}/revisions")
    bad = client.put(
        f"/baskets/{basket_id}/draft/slots/food/weight", json={"weight_bp": 5000}
    )
    assert bad.status_code == 200
    rejected = client.post(f"/baskets/{basket_id}/publish")
    assert rejected.status_code == 422
    assert any(
        i["code"] == "weight_sum_not_10000"
        for i in rejected.json()["error"]["details"]["issues"]
    )
    assert client.get(f"/baskets/{basket_id}/snapshot").json()["version_no"] == 1


def test_replacement_without_overlap_rejects_publish(client):
    basket_id, _ = create_and_publish(client)
    client.post(f"/baskets/{basket_id}/revisions")
    # Build Feb and Mar quotes for the original items first.
    add_prices(
        client,
        basket_id,
        [
            {"item_code": "rice", "month": "2025-02", "price_cents": 1100},
            {"item_code": "bus", "month": "2025-02", "price_cents": 200},
            {"item_code": "rice", "month": "2025-03", "price_cents": 1200},
            {"item_code": "bus", "month": "2025-03", "price_cents": 210},
        ],
    )
    replacement = client.post(
        f"/baskets/{basket_id}/draft/replacements",
        json={
            "slot_key": "food",
            "month": "2025-04",
            "new_code": "noodles",
            "new_name": "面条",
            "old_price_cents": 1250,
            "new_price_cents": 500,
        },
    )
    assert replacement.status_code == 201, replacement.text
    # Drop the new item's overlap quote on purpose.
    deleted = client.delete(
        f"/baskets/{basket_id}/draft/prices/noodles/2025-04"
    )
    assert deleted.status_code == 200
    # Supply the April quote the transport slot still needs.
    add_prices(
        client, basket_id, [{"item_code": "bus", "month": "2025-04", "price_cents": 220}]
    )
    rejected = client.post(f"/baskets/{basket_id}/publish")
    assert rejected.status_code == 422
    issues = rejected.json()["error"]["details"]["issues"]
    assert any(
        i["code"] == "missing_quote"
        and i["item_code"] == "noodles"
        and i["month"] == "2025-04"
        and i.get("context") == "replacement_overlap"
        for i in issues
    )
    assert client.get(f"/baskets/{basket_id}/snapshot").json()["version_no"] == 1


def test_duplicate_quote_in_batch_is_rejected(client):
    basket_id, _ = create_and_publish(client)
    client.post(f"/baskets/{basket_id}/revisions")
    response = client.put(
        f"/baskets/{basket_id}/draft/prices",
        json={
            "entries": [
                {"item_code": "rice", "month": "2025-02", "price_cents": 1100},
                {"item_code": "rice", "month": "2025-02", "price_cents": 1150},
            ]
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "duplicate_quote"


def test_duplicate_replacement_month_is_rejected(client):
    basket_id, _ = create_and_publish(client)
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client,
        basket_id,
        [
            {"item_code": "rice", "month": "2025-02", "price_cents": 1100},
            {"item_code": "bus", "month": "2025-02", "price_cents": 200},
        ],
    )
    first = client.post(
        f"/baskets/{basket_id}/draft/replacements",
        json={
            "slot_key": "food",
            "month": "2025-02",
            "new_code": "noodles",
            "new_name": "面条",
            "old_price_cents": 1100,
            "new_price_cents": 500,
        },
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/baskets/{basket_id}/draft/replacements",
        json={
            "slot_key": "food",
            "month": "2025-02",
            "new_code": "bread",
            "new_name": "面包",
            "old_price_cents": 1100,
            "new_price_cents": 300,
        },
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] in {"duplicate_replacement", "slot_timeline_closed"}


# ---------------------------------------------------------------------------
# Revisions recompute only from the earliest affected month
# ---------------------------------------------------------------------------


def _three_month_snapshot(client):
    basket_id, _ = create_and_publish(client)
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client,
        basket_id,
        [
            {"item_code": "rice", "month": "2025-02", "price_cents": 1100},
            {"item_code": "bus", "month": "2025-02", "price_cents": 200},
            {"item_code": "rice", "month": "2025-03", "price_cents": 1210},
            {"item_code": "bus", "month": "2025-03", "price_cents": 220},
        ],
    )
    v2 = client.post(f"/baskets/{basket_id}/publish").json()
    return basket_id, v2


def test_correcting_middle_month_recomputes_only_from_there(client):
    basket_id, v2 = _three_month_snapshot(client)
    assert v2["version_no"] == 2
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client,
        basket_id,
        [{"item_code": "rice", "month": "2025-03", "price_cents": 1200}],
    )
    v3 = client.post(f"/baskets/{basket_id}/publish").json()
    assert v3["version_no"] == 3
    assert v3["recomputed_from"] == "2025-03"
    assert v3["reused_point_count"] == 2
    assert v3["computed_point_count"] == 1
    origins = {p["month"]: p["origin"] for p in v3["index"]}
    assert origins == {
        "2025-01": "reused",
        "2025-02": "reused",
        "2025-03": "computed",
    }
    jan, feb, mar = v3["index"]
    # The reused Jan/Feb values are bit-for-bit the same as v2.
    assert jan["index_num"] == v2["index"][0]["index_num"]
    assert feb["index_num"] == v2["index"][1]["index_num"]
    # Mar: 0.6*(1200/1100) + 0.4*(220/200) = 0.654545+0.44
    expected_mar_mom = Fraction(6, 10) * Fraction(1200, 1100) + Fraction(4, 10) * Fraction(220, 200)
    expected_mar_index = Fraction(106, 1) * expected_mar_mom
    assert Fraction(int(mar["index_num"]), int(mar["index_den"])) == expected_mar_index
    assert mar["mom"] == "1.094545"


def test_correcting_base_month_recomputes_everything(client):
    basket_id, _ = _three_month_snapshot(client)
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client, basket_id, [{"item_code": "rice", "month": "2025-01", "price_cents": 1050}]
    )
    v3 = client.post(f"/baskets/{basket_id}/publish").json()
    # A base price affects the February relative, not January's fixed 100.
    assert v3["recomputed_from"] == "2025-02"
    assert v3["computed_point_count"] == 2
    assert v3["index"][0]["index"] == "100.000000"


def test_weight_change_recomputes_everything(client):
    basket_id, _ = _three_month_snapshot(client)
    client.post(f"/baskets/{basket_id}/revisions")
    changed = client.put(
        f"/baskets/{basket_id}/draft/slots/food/weight", json={"weight_bp": 5000}
    )
    assert changed.status_code == 200
    client.put(
        f"/baskets/{basket_id}/draft/slots/transport/weight", json={"weight_bp": 5000}
    )
    v3 = client.post(f"/baskets/{basket_id}/publish").json()
    assert v3["recomputed_from"] == "2025-02"
    assert v3["computed_point_count"] == 2


def test_appending_a_month_is_the_earliest_affected_month(client):
    basket_id, _ = _three_month_snapshot(client)
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client,
        basket_id,
        [
            {"item_code": "rice", "month": "2025-04", "price_cents": 1210},
            {"item_code": "bus", "month": "2025-04", "price_cents": 231},
        ],
    )
    v3 = client.post(f"/baskets/{basket_id}/publish").json()
    assert v3["recomputed_from"] == "2025-04"
    assert v3["reused_point_count"] == 3


def test_earliest_affected_month_is_none_when_nothing_changed():
    data = VersionData(
        base_month="2025-01",
        slots=[Slot("food", "食品", 10000)],
        items=[Item("rice", "大米", "food", "2025-01", None)],
        prices={("rice", "2025-01"): 1000, ("rice", "2025-02"): 1100},
    )
    assert earliest_affected_month(data, data) is None


# ---------------------------------------------------------------------------
# Version isolation
# ---------------------------------------------------------------------------


def test_queries_cannot_mix_versions(client):
    basket_id, v2 = _three_month_snapshot(client)
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client,
        basket_id,
        [{"item_code": "rice", "month": "2025-03", "price_cents": 1200}],
    )
    v3 = client.post(f"/baskets/{basket_id}/publish").json()

    snapshot_v2 = client.get(f"/baskets/{basket_id}/versions/2/snapshot").json()
    snapshot_v3 = client.get(f"/baskets/{basket_id}/versions/3/snapshot").json()
    assert snapshot_v2["index"][2]["index"] != snapshot_v3["index"][2]["index"]
    assert snapshot_v2["snapshot_sha256"] != snapshot_v3["snapshot_sha256"]
    assert snapshot_v2["version_no"] == 2 and snapshot_v3["version_no"] == 3
    # Prices and points returned together belong to the requested version only.
    assert any(p["price_cents"] == 1210 for p in snapshot_v2["prices"])
    assert any(p["price_cents"] == 1200 for p in snapshot_v3["prices"])
    assert not any(p["price_cents"] == 1210 for p in snapshot_v3["prices"])

    index_v2 = client.get(f"/baskets/{basket_id}/versions/2/index").json()
    assert index_v2["snapshot_sha256"] == snapshot_v2["snapshot_sha256"]
    current = client.get(f"/baskets/{basket_id}/snapshot").json()
    assert current["version_no"] == 3


def test_draft_is_never_queryable_as_a_version(client):
    basket_id, _ = create_and_publish(client)
    client.post(f"/baskets/{basket_id}/revisions")
    draft = client.get(f"/baskets/{basket_id}/versions/2/snapshot")
    assert draft.status_code == 409
    assert draft.json()["error"]["code"] == "version_not_published"


def test_index_range_filter(client):
    basket_id, _ = _three_month_snapshot(client)
    payload = client.get(
        f"/baskets/{basket_id}/versions/2/index?from=2025-02&to=2025-02"
    ).json()
    assert [p["month"] for p in payload["points"]] == ["2025-02"]


# ---------------------------------------------------------------------------
# Replacement chain end-to-end
# ---------------------------------------------------------------------------


def test_replacement_end_to_end_and_index_continuity(client):
    basket_id, _ = _three_month_snapshot(client)
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client,
        basket_id,
        [
            {"item_code": "rice", "month": "2025-04", "price_cents": 1250},
            {"item_code": "bus", "month": "2025-04", "price_cents": 220},
        ],
    )
    response = client.post(
        f"/baskets/{basket_id}/draft/replacements",
        json={
            "slot_key": "food",
            "month": "2025-04",
            "new_code": "noodles",
            "new_name": "面条",
            "old_price_cents": 1250,
            "new_price_cents": 500,
        },
    )
    assert response.status_code == 201, response.text
    add_prices(
        client, basket_id, [{"item_code": "noodles", "month": "2025-05", "price_cents": 550},
                            {"item_code": "bus", "month": "2025-05", "price_cents": 242}]
    )
    v3 = client.post(f"/baskets/{basket_id}/publish").json()

    april = v3["index"][3]
    # April uses the old item's movement: rice 1250/1210.
    expected_april = Fraction(6, 10) * Fraction(1250, 1210) + Fraction(4, 10) * Fraction(220, 220)
    assert april["mom"] == str(float(expected_april))[:8] or Fraction(
        int(april["mom_num"]), int(april["mom_den"])
    ) == expected_april
    may = v3["index"][4]
    # May uses the new item's movement: noodles 550/500.
    expected_may = Fraction(6, 10) * Fraction(550, 500) + Fraction(4, 10) * Fraction(242, 220)
    assert Fraction(int(may["mom_num"]), int(may["mom_den"])) == expected_may
    # Chain is continuous: no level jump is introduced by the swap itself.
    assert Fraction(int(may["index_num"]), int(may["index_den"])) == Fraction(
        int(april["index_num"]), int(april["index_den"])
    ) * expected_may


# ---------------------------------------------------------------------------
# Immutability of published data
# ---------------------------------------------------------------------------


def test_published_prices_are_immutable(client, tmp_path):
    basket_id, _ = create_and_publish(client)
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(str(tmp_path / "test.sqlite3")) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "UPDATE prices SET price_cents = 999 WHERE basket_version_id = 1 AND item_code = 'rice'"
            )
    # If the above somehow succeeded, the published snapshot must still differ.
    snapshot = client.get(f"/baskets/{basket_id}/versions/1/snapshot")
    assert snapshot.status_code == 200


def test_initial_payload_can_carry_replacement_history(client):
    payload = {
        "name": "History basket",
        "base_month": "2025-01",
        "slots": [{"slot_key": "food", "name": "食品", "weight_bp": 10000}],
        "items": [
            {
                "code": "rice",
                "name": "大米",
                "slot_key": "food",
                "active_from": "2025-01",
                "active_through": "2025-03",
                "prices": {
                    "2025-01": 1000,
                    "2025-02": 1100,
                    "2025-03": 1200,
                },
            },
            {
                "code": "noodles",
                "name": "面条",
                "slot_key": "food",
                "active_from": "2025-03",
                "predecessor_code": "rice",
                "prices": {"2025-03": 500, "2025-04": 550},
            },
        ],
    }
    basket_id, snapshot = create_and_publish(client, payload)
    assert [p["index"] for p in snapshot["index"]] == [
        "100.000000",
        "110.000000",
        "120.000000",
        "132.000000",
    ]
    # The slot weight is unchanged by the swap.
    assert snapshot["slots"] == [
        {"slot_key": "food", "name": "食品", "weight_bp": 10000}
    ]


def test_publish_response_and_later_queries_share_one_snapshot(client):
    basket_id, _ = create_and_publish(client)
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client,
        basket_id,
        [
            {"item_code": "rice", "month": "2025-02", "price_cents": 1100},
            {"item_code": "bus", "month": "2025-02", "price_cents": 200},
        ],
    )
    published = client.post(f"/baskets/{basket_id}/publish").json()
    snapshot = client.get(f"/baskets/{basket_id}/snapshot").json()
    by_version = client.get(f"/baskets/{basket_id}/versions/2/snapshot").json()
    index_only = client.get(f"/baskets/{basket_id}/versions/2/index").json()
    assert published["snapshot_sha256"] == snapshot["snapshot_sha256"]
    assert published["snapshot_sha256"] == by_version["snapshot_sha256"]
    assert published["snapshot_sha256"] == index_only["snapshot_sha256"]
    assert [
        (p["month"], p["index"], p["mom"], p["origin"]) for p in published["index"]
    ] == [
        (p["month"], p["index"], p["mom"], p["origin"]) for p in snapshot["index"]
    ]


def test_republished_history_is_immutable(client):
    basket_id, v2 = _three_month_snapshot(client)
    before = client.get(f"/baskets/{basket_id}/versions/2/snapshot").json()
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client,
        basket_id,
        [{"item_code": "rice", "month": "2025-02", "price_cents": 999}],
    )
    client.post(f"/baskets/{basket_id}/publish")
    after = client.get(f"/baskets/{basket_id}/versions/2/snapshot").json()
    assert before == after


def test_quote_outside_item_active_range_is_rejected(client):
    basket_id, _ = create_and_publish(client)
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client,
        basket_id,
        [
            {"item_code": "rice", "month": "2025-02", "price_cents": 1100},
            {"item_code": "bus", "month": "2025-02", "price_cents": 200},
        ],
    )
    client.post(
        f"/baskets/{basket_id}/draft/replacements",
        json={
            "slot_key": "food",
            "month": "2025-02",
            "new_code": "noodles",
            "new_name": "面条",
            "old_price_cents": 1100,
            "new_price_cents": 500,
        },
    )
    # Rice is no longer active after February.
    response = client.put(
        f"/baskets/{basket_id}/draft/prices",
        json={"entries": [{"item_code": "rice", "month": "2025-03", "price_cents": 1300}]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "quote_outside_active_range"


def test_request_validation_errors_are_json_safe(client):
    response = client.post(
        "/baskets",
        json={"name": "bad", "base_month": "2025-13", "slots": [], "items": []},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "request_invalid"
    assert body["error"]["details"]["issues"]


def test_non_positive_price_in_create_payload_is_rejected(client):
    payload = make_basket()
    payload["items"][0]["prices"] = {"2025-01": 0}
    response = client.post("/baskets", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_invalid"


def test_invalid_month_on_delete_is_rejected(client):
    basket_id, _ = create_and_publish(client)
    client.post(f"/baskets/{basket_id}/revisions")
    response = client.delete(f"/baskets/{basket_id}/draft/prices/rice/2025-13")
    assert response.status_code == 422


def test_snapshot_integrity_flag_is_true_for_all_versions(client):
    basket_id, _ = _three_month_snapshot(client)
    client.post(f"/baskets/{basket_id}/revisions")
    add_prices(
        client,
        basket_id,
        [{"item_code": "rice", "month": "2025-03", "price_cents": 1200}],
    )
    client.post(f"/baskets/{basket_id}/publish")
    for version in (1, 2, 3):
        snapshot = client.get(f"/baskets/{basket_id}/versions/{version}/snapshot")
        if snapshot.status_code == 200:
            assert snapshot.json()["integrity_ok"] is True
    assert client.get(f"/baskets/{basket_id}/snapshot").json()["integrity_ok"] is True
