from decimal import Decimal

import pytest

from app.errors import AppError
from app.weights import rebalance, to_keitaro


def offer(offer_id, *, pinned=None, state="active", **extra):
    return {
        "offer_id": offer_id,
        "state": state,
        "pinned": pinned is not None,
        "pinned_share": pinned,
        "share": 0,
        **extra,
    }


def percentages(offers):
    return {item["offer_id"]: Decimal(str(item["share"])) for item in offers}


def assert_total(offers):
    active = [item for item in offers if item["state"] == "active"]
    assert sum(Decimal(str(item["share"])) for item in active) == Decimal("100")


def test_adding_and_removing_offers_redistributes_the_whole_budget():
    offers = [offer(1)]
    assert percentages(rebalance(offers)) == {1: Decimal("100")}
    offers.append(offer(2))
    assert percentages(rebalance(offers)) == {1: Decimal("50"), 2: Decimal("50")}
    offers.append(offer(3))
    assert percentages(rebalance(offers)) == {
        1: Decimal("34"),
        2: Decimal("33"),
        3: Decimal("33"),
    }
    offers[1]["state"] = "removed"
    assert percentages(rebalance(offers)) == {
        1: Decimal("50"),
        2: Decimal("0"),
        3: Decimal("50"),
    }
    assert_total(offers)


def test_pinned_25_leaves_integer_38_and_37_shares():
    offers = rebalance([offer(7, pinned=Decimal("25")), offer(8), offer(9)])
    assert percentages(offers) == {
        7: Decimal("25"),
        8: Decimal("38"),
        9: Decimal("37"),
    }
    assert_total(offers)


def test_rounding_is_exact_and_does_not_depend_on_display_order():
    forward = rebalance([offer(10), offer(20), offer(30)])
    reverse = rebalance([offer(30), offer(20), offer(10)])
    assert percentages(forward) == percentages(reverse)
    assert [item["offer_id"] for item in reverse] == [30, 20, 10]
    assert_total(forward)
    assert_total(reverse)


def test_integer_pins_preserve_the_last_percentage_point():
    offers = rebalance([offer(1, pinned="33"), offer(2, pinned="33"), offer(3)])
    assert percentages(offers) == {
        1: Decimal("33"),
        2: Decimal("33"),
        3: Decimal("34"),
    }
    assert_total(offers)


def test_removed_pinned_offer_does_not_reserve_traffic_and_can_be_restored():
    offers = [offer(1, pinned="25", state="removed"), offer(2), offer(3)]
    rebalance(offers)
    assert percentages(offers) == {1: Decimal("0"), 2: Decimal("50"), 3: Decimal("50")}
    offers[0]["state"] = "active"
    rebalance(offers)
    assert percentages(offers) == {
        1: Decimal("25"),
        2: Decimal("38"),
        3: Decimal("37"),
    }


def test_pinning_zero_is_distinct_from_unpinned():
    offers = rebalance([offer(1, pinned=0), offer(2)])
    assert percentages(offers) == {1: Decimal("0"), 2: Decimal("100")}


@pytest.mark.parametrize("count", [1, 2, 3, 6, 7, 13, 99, 100])
def test_allowed_offer_counts_always_sum_to_100(count):
    offers = rebalance([offer(index) for index in range(1, count + 1)])
    assert_total(offers)
    assert all(Decimal(str(item["share"])) > 0 for item in offers)


@pytest.mark.parametrize("offers", [[], [offer(1, state="removed")]])
def test_empty_active_stream_is_rejected(offers):
    with pytest.raises(AppError) as error:
        rebalance(offers)
    assert error.value.status == 422


def test_excess_active_offers_are_rejected():
    with pytest.raises(AppError) as error:
        rebalance([offer(index) for index in range(1, 102)])
    assert error.value.status == 422


@pytest.mark.parametrize("pins", [("60", "50"), ("25", "25"), ("99.99",)])
def test_invalid_all_pinned_total_is_rejected(pins):
    with pytest.raises(AppError) as error:
        rebalance([offer(index, pinned=pin) for index, pin in enumerate(pins, 1)])
    assert error.value.status == 422


def test_pinned_budget_over_100_is_rejected_even_with_a_free_offer():
    with pytest.raises(AppError):
        rebalance([offer(1, pinned="70"), offer(2, pinned="40"), offer(3)])


def test_all_pinned_shares_are_allowed_when_the_total_is_exactly_100():
    offers = rebalance([offer(1, pinned="12"), offer(2, pinned="88")])
    assert percentages(offers) == {1: Decimal("12"), 2: Decimal("88")}
    assert_total(offers)


def test_keitaro_payload_contains_actual_integer_percentages_and_excludes_removed_offers():
    offers = rebalance(
        [
            offer(1, pinned="25"),
            offer(2),
            offer(3),
            offer(4, state="removed"),
        ]
    )
    payload = to_keitaro(offers)
    assert payload == [
        {"offer_id": 1, "share": 25, "state": "active"},
        {"offer_id": 2, "share": 38, "state": "active"},
        {"offer_id": 3, "share": 37, "state": "active"},
    ]
    total_weight = sum(item["share"] for item in payload)
    assert total_weight == 100
    for item in payload:
        assert Decimal(item["share"]) * 100 / total_weight == percentages(offers)[item["offer_id"]]


def test_disabled_relation_is_preserved_without_allocating_it_active_traffic():
    offers = rebalance(
        [
            offer(1),
            offer(2, state="removed", remote_disabled=True, remote_weight=73, keitaro_state="disabled"),
        ]
    )
    assert to_keitaro(offers) == [
        {"offer_id": 1, "share": 100, "state": "active"},
        {"offer_id": 2, "share": 73, "state": "disabled"},
    ]


@pytest.mark.parametrize("share", ["37.5", "-1", "100.1", "NaN", "Infinity", "not-a-number"])
def test_pinned_shares_must_be_finite_integer_percentages(share):
    with pytest.raises(AppError) as error:
        rebalance([offer(1, pinned=share), offer(2)])
    assert error.value.status == 422


def test_fractional_payload_is_rejected_instead_of_silently_truncated():
    with pytest.raises(AppError):
        to_keitaro([offer(1, share=37.5)])
