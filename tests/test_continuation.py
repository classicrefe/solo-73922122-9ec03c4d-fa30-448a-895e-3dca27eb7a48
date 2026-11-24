"""Unit tests for the pure continuation planner."""

from __future__ import annotations

from app.continuation import Candidate, solve


def _candidate(code: str, months_prices: dict[str, int]) -> Candidate:
    return Candidate(code=code, name=code.upper(), prices=dict(months_prices))


def test_single_candidate_reaching_target_is_one_switch():
    plan = solve(
        slot_key="food",
        current_code="rice",
        current_name="大米",
        current_active_from="2025-01",
        current_quotes={"2025-01": 10, "2025-02": 11, "2025-03": 12},
        candidates=[
            _candidate("corn", {"2025-03": 80, "2025-04": 81, "2025-05": 82}),
        ],
        target_month="2025-05",
    )
    assert plan is not None
    assert plan.replacement_count == 1
    assert plan.replacement_months == ["2025-03"]
    assert [segment.item_code for segment in plan.segments] == ["rice", "corn"]
    first, second = plan.segments
    assert first.active_through == "2025-03" and first.switch_month is None
    assert second.active_from == "2025-03"
    assert second.active_through is None
    assert second.switch_month == "2025-03"


def test_prefers_chain_with_fewest_switches():
    # corn reaches the target directly (1 switch); noodles then bread needs 2.
    plan = solve(
        slot_key="food",
        current_code="rice",
        current_name="大米",
        current_active_from="2025-01",
        current_quotes={"2025-01": 10, "2025-02": 11, "2025-03": 12},
        candidates=[
            _candidate("noodles", {"2025-03": 50, "2025-04": 51, "2025-05": 52}),
            _candidate("bread", {"2025-05": 30, "2025-06": 31, "2025-07": 32}),
            _candidate("corn", {"2025-03": 80, "2025-04": 81, "2025-05": 82,
                                "2025-06": 83, "2025-07": 84}),
        ],
        target_month="2025-07",
    )
    assert plan is not None
    assert plan.replacement_count == 1
    assert plan.replacement_months == ["2025-03"]
    assert plan.segments[-1].item_code == "corn"


def test_multi_switch_chain_matches_overlap_months():
    plan = solve(
        slot_key="food",
        current_code="rice",
        current_name="大米",
        current_active_from="2025-01",
        current_quotes={"2025-01": 10, "2025-02": 11, "2025-03": 12},
        candidates=[
            _candidate("noodles", {"2025-03": 50, "2025-04": 51, "2025-05": 52}),
            _candidate("bread", {"2025-05": 30, "2025-06": 31, "2025-07": 32}),
        ],
        target_month="2025-07",
    )
    assert plan is not None
    assert plan.replacement_count == 2
    assert plan.replacement_months == ["2025-03", "2025-05"]
    codes = [segment.item_code for segment in plan.segments]
    assert codes == ["rice", "noodles", "bread"]
    middle = plan.segments[1]
    assert middle.active_from == "2025-03"
    assert middle.active_through == "2025-05"
    # Each switch month carries both the outgoing and incoming item's quote.
    for segment in plan.segments[1:]:
        switch = segment.switch_month
        assert dict(segment.prices).get(switch) is not None


def test_returns_none_when_no_candidate_starts_at_last_quote_month():
    plan = solve(
        slot_key="food",
        current_code="rice",
        current_name="大米",
        current_active_from="2025-01",
        current_quotes={"2025-01": 10, "2025-02": 11, "2025-03": 12},
        candidates=[_candidate("corn", {"2025-04": 80, "2025-05": 81})],
        target_month="2025-05",
    )
    assert plan is None


def test_returns_none_when_chain_falls_short_of_target():
    plan = solve(
        slot_key="food",
        current_code="rice",
        current_name="大米",
        current_active_from="2025-01",
        current_quotes={"2025-01": 10, "2025-02": 11, "2025-03": 12},
        candidates=[
            _candidate("noodles", {"2025-03": 50, "2025-04": 51}),
            _candidate("bread", {"2025-04": 30, "2025-05": 31}),
        ],
        target_month="2025-07",
    )
    assert plan is None


def test_candidate_with_gap_is_ignored():
    plan = solve(
        slot_key="food",
        current_code="rice",
        current_name="大米",
        current_active_from="2025-01",
        current_quotes={"2025-01": 10, "2025-02": 11, "2025-03": 12},
        candidates=[
            # April quote missing -> cannot carry the slot month by month.
            _candidate("broken", {"2025-03": 50, "2025-05": 52}),
            _candidate("corn", {"2025-03": 80, "2025-04": 81, "2025-05": 82}),
        ],
        target_month="2025-05",
    )
    assert plan is not None
    assert plan.segments[-1].item_code == "corn"


def test_switch_month_must_be_later_than_outgoing_start_month():
    # The current item started in 2025-03; a switch in 2025-03 is forbidden.
    plan = solve(
        slot_key="food",
        current_code="rice",
        current_name="大米",
        current_active_from="2025-03",
        current_quotes={"2025-03": 12},
        candidates=[_candidate("corn", {"2025-03": 80, "2025-04": 81})],
        target_month="2025-04",
    )
    assert plan is None


def test_item_switched_out_can_never_come_back():
    # noodles could theoretically be visited twice; the mask forbids it, so the
    # only viable chain is noodles -> bread reaching the target.
    plan = solve(
        slot_key="food",
        current_code="rice",
        current_name="大米",
        current_active_from="2025-01",
        current_quotes={"2025-01": 10, "2025-02": 11, "2025-03": 12},
        candidates=[
            _candidate("noodles", {"2025-03": 50, "2025-04": 51, "2025-05": 52}),
            _candidate("bread", {"2025-05": 30, "2025-06": 31, "2025-07": 32}),
            _candidate("loop", {"2025-07": 90, "2025-08": 91}),
        ],
        target_month="2025-08",
    )
    assert plan is not None
    codes = [segment.item_code for segment in plan.segments]
    assert len(codes) == len(set(codes))
    assert codes == ["rice", "noodles", "bread", "loop"]
