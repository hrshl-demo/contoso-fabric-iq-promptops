"""Answer comparison used by `verify --answers`, and the assessment helpers."""
import pytest

from contoso_onto import assess as AS
from contoso_onto.oracles import compare, numbers_in


@pytest.mark.parametrize("text,vals", [
    ("INR 4,85,000.50", [485000.5]), ("₹20,000 and 7.5 lakh", [20000.0, 750000.0]), ("1.2 crore", [12000000.0]),
    ("about 20k", [20000.0]), ("Rs. 1,008,300 on 2026-09-30", [1008300.0, 2026.0, 9.0, 30.0]),
])
def test_numbers_in_understands_indian_formats(text, vals):
    assert numbers_in(text) == vals


def test_compare_numbers_strings_and_lists():
    exp = {"balance_inr": 1008300.0, "as_on": "2026-09-30", "who": ["Customer"]}
    assert compare(exp, "Rhea has INR 10,08,300 as on 2026-09-30. Only the customer.")["pass"]
    assert compare(exp, "Rhea has 10.08 lakh as on 2026-09-30. Only the Customer.")["pass"]
    r = compare(exp, "Rhea has INR 10,00,000 as on 2026-09-30. Customer")
    assert not r["pass"] and r["missing"] == [1008300.0]
    assert not compare("revoked", "It is active")["pass"]
    assert compare("temporarily_locked_after_dispute", "The card is temporarily locked after dispute")["pass"]


def test_money_tolerance_is_tight_for_small_values():
    assert not compare(8.5, "the rate is 9.5%")["pass"]
    assert compare(8.5, "the rate is 8.5%")["pass"]
    assert compare(48.33, "about 48.3 percent")["pass"]


def test_pick_key_prefers_the_file_name_over_other_unique_ids():
    rows = [{"accountId": "a1", "cardId": "c1", "ownerId": "x"}]
    assert AS.pick_key("releases/r/cards.json", rows) == "cardId"
    assert AS.pick_key("releases/r/accounts.json", rows) == "accountId"
    assert AS.pick_key("releases/r/bank/crm-cases.json::cases", [{"caseId": "k1", "buId": "b"}, {"caseId": "k2", "buId": "b"}]) == "caseId"
    assert AS.pick_key("releases/r/x.json", [{"ownerId": "o"}]) is None


def test_kind_and_money_detection():
    assert AS.kind("2026-09-30") == "date" and AS.kind("2026-09-30T10:00:00+05:30") == "timestamp" and AS.kind(None) == "null"
    assert AS.is_money({"amountMinor": 1, "currency": "INR"}) and not AS.is_money({"amountMinor": 1, "other": 2, "x": 3})


def test_identity_collision_and_overlap_detection():
    colls = {"releases/a/personas.json": [{"ownerId": "p"}], "releases/b/personas.json": [{"ownerId": "p"}],
             "releases/a/accounts.json": [{"ownerId": "p", "accountId": "a1", "postedBalance": {"amountMinor": 500, "currency": "INR"}}],
             "releases/a/goals.json": [{"ownerId": "p", "goalId": "g1", "current": {"amountMinor": 500, "currency": "INR"}}]}
    assert AS.identity_collisions(colls)[0]["ownerId"] == "p"
    ov = AS.overlap_candidates(colls)
    assert ov and ov[0]["records"] == ["accounts:a1.postedBalance", "goals:g1.current"]
