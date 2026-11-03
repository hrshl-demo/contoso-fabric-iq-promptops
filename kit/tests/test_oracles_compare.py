"""oracles.compare: how a spoken or written answer is matched against an expected value."""
from contoso_onto import oracles


def test_a_negative_expected_value_is_matched_by_its_magnitude():
    """Live finding (16 Oct 2026): net position is negative (liabilities exceed assets) and the comparer could never match it."""
    exp = {"assets_inr": 2271300.0, "net_inr": -3709792.13}
    assert oracles.compare(exp, "Assets INR 2,271,300; the difference is INR -3,709,792.13.")["pass"]
    assert oracles.compare(exp, "Assets are 22.71 lakh and liabilities exceed them by 37,09,792.13 rupees.")["pass"]      # 22.71 lakh is within 0.5 percent of 2,271,300
    assert oracles.compare(exp, "Assets 2,271,300; a shortfall of 3,709,792.13 rupees.")["pass"]
    assert oracles.compare(exp, "Assets 2,271,300; a shortfall of 4,487,792.13 rupees.") == {"pass": False, "missing": [-3709792.13]}


def test_positive_values_strings_and_indian_grouping_still_match():
    assert oracles.compare(20000.0, "Up to Rs 20,000 per action")["pass"] and not oracles.compare(20000.0, "Up to Rs 25,000 per action")["pass"]
    assert oracles.compare(1008300.0, "About 10,08,300 rupees")["pass"]
    assert oracles.compare(["locked", "dispute"], "The card is temporarily locked after a dispute")["pass"]
    assert oracles.compare("temporarily_locked", "Temporarily LOCKED") == {"pass": True, "missing": []}


def test_status_codes_and_permission_codes_become_the_words_an_answer_uses():
    assert oracles._status_words("temporarily_locked_after_dispute") == ["locked", "dispute"]
    assert oracles._status_words("active") == ["active"]
    assert oracles._code_words("view:household_budget") == "household budget" and oracles._code_words("transact") == "transact"
