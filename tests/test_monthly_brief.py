import re
import subprocess
import sys

import pytest

pytest.importorskip("matplotlib")
pytest.importorskip("pandas")
pytest.importorskip("pyarrow")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from analysis.monthly_brief import build_brief as bb  # noqa: E402
from tools import export_fabric as ef  # noqa: E402
from tools.promptlib import ROOT, parse  # noqa: E402


def test_forecast_on_a_perfect_line():
    t = bb.analyse(10 + 2 * np.arange(12))
    assert t.slope == pytest.approx(2.0)
    assert t.forecast == pytest.approx([34.0, 36.0, 38.0])
    assert t.upper - t.lower == pytest.approx(np.zeros(3), abs=1e-6)
    assert not t.outliers.any()


def test_forecast_band_is_centred_and_widens_with_the_horizon():
    rng = np.random.default_rng(7)
    y = 100 + 3 * np.arange(12) + rng.normal(0, 2, 12)
    t = bb.analyse(y, horizon=3)
    slope, intercept = np.polyfit(np.arange(12), y, 1)
    assert t.forecast == pytest.approx(intercept + slope * np.arange(12, 15))
    assert (t.upper + t.lower) / 2 == pytest.approx(t.forecast)
    width = t.upper - t.lower
    assert (width > 0).all() and width[0] < width[1] < width[2]


def test_a_trend_needs_at_least_four_months():
    with pytest.raises(bb.BriefError, match="at least 4"):
        bb.analyse(np.array([1.0, 2.0, 3.0]))


def test_outliers_flag_a_spike_and_nothing_in_a_smooth_series():
    y = 50 + np.arange(12) * 1.5
    assert not bb.robust_outliers(y - np.polyval(np.polyfit(np.arange(12), y, 1), np.arange(12))).any()
    noisy = y + np.array([0.1, -0.1, 0.05, -0.05, 0.1, -0.1, 0.05, -0.05, 0.1, -0.1, 0.05, -0.05])
    noisy[4] += 12
    assert list(np.flatnonzero(bb.analyse(noisy).outliers)) == [4]


def test_a_flat_residual_series_has_no_outliers():
    assert not bb.robust_outliers(np.zeros(12)).any()


def test_month_helpers():
    assert bb.add_months("2025-12", 1) == "2026-01"
    assert bb.add_months("2026-01", -1) == "2025-12"
    assert bb.add_months("2026-09", 3) == "2026-12"
    assert bb.label("2025-10") == "Oct 25"
    assert bb.crore(28000) == "28,000.0"
    assert bb.share(1, 4) == 25.0 and bb.share(1, 0) == 0.0


def test_spread_keeps_order_and_separates_labels():
    out = bb.spread([10.0, 10.1, 10.2, 20.0], 1.0)
    assert out[3] == 20.0
    assert sorted(out) == out and all(b - a >= 1.0 - 1e-9 for a, b in zip(out, out[1:]))
    assert bb.spread([3.0, 1.0, 2.0], 0.5) == [3.0, 1.0, 2.0]


def piv_with_a_shared_dip():
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    base = 100 + 2 * np.arange(12.0)
    cols = {}
    for k, bu in enumerate(("BU-A", "BU-B", "BU-C")):
        y = base * (1 + 0.1 * k) + np.array([0.1, -0.1] * 6)
        y[2] -= 15 * (1 + 0.1 * k)
        cols[bu] = y
    cols["BU-D"] = base + np.array([0.1, -0.1] * 6)
    return pd.DataFrame(cols, index=months)


def test_unusual_months_are_grouped_by_month():
    out = bb.unusual_months(piv_with_a_shared_dip(), {"BU-A": "Alpha", "BU-B": "Beta", "BU-C": "Gamma", "BU-D": "Delta"})
    assert out == ["Mar 25 (3 units)"]


def test_unusual_months_name_the_unit_when_only_one_is_flagged():
    p = piv_with_a_shared_dip()[["BU-A", "BU-D"]]
    assert bb.unusual_months(p, {"BU-A": "Alpha", "BU-D": "Delta"}) == ["Mar 25 (Alpha)"]


def test_headline_units_counts_units_below_target():
    bu = pd.DataFrame({"name": ["A", "B", "C"], "portfolio_crore_inr": [100.0, 50.0, 10.0], "target_attainment_pct": [105.0, 90.0, 99.5]})
    h = bb.headline_units(bu)
    assert "INR 160.0 crore" in h and "A is the largest at INR 100.0 crore" in h
    assert "2 of 3 units are below target: B (90.0%), C (99.5%)" in h
    assert "Every unit is at or above its target" in bb.headline_units(bu.assign(target_attainment_pct=[100, 100, 101]))


def test_campaign_conversion_follows_the_models_definition_converted_over_reach():
    ca = pd.DataFrame({"name": ["X", "Y"], "status": ["live", "planned"], "reach": [1000, 200], "engaged": [100, 100], "converted": [50, 40],
                       "conversion_pct": [50.0, 40.0], "revenue_attributed_crore_inr": [1.5, 9.5]})
    assert list(bb.conversion_on_reach(ca)) == [5.0, 20.0]
    h = bb.headline_campaigns(ca)
    assert "Y converts best (20.0% of reach)" in h and "Y attributes the most revenue (INR 9.5 crore)" in h and "1 are live now" in h
    assert "7.5% of the people reached converted" in h


def test_headline_ops_names_the_hardest_fraud_signal_and_high_alerts():
    rg = pd.DataFrame({"city": ["Pune", "Agra"], "deposits_crore_inr": [10.0, 99.5]})
    ra = pd.DataFrame({"severity": ["high", "info"], "bu_id": ["BU-X", "BU-Y"]})
    fs = pd.DataFrame({"label": ["Phishing", "Mule accounts"], "detected_last_30d": [100, 50], "blocked_last_30d": [95, 20], "trend": ["flat", "up"]})
    h = bb.headline_ops(rg, ra, fs, {"BU-X": "Cards"})
    assert "Agra holds the most deposits (INR 99.5 crore)" in h and "1 high-severity risk alert is open (Cards)" in h
    assert "mule accounts are the hardest to stop (40.0% blocked, trend up)" in h and "76.7% were blocked" in h


def test_load_names_a_missing_table_or_column(tmp_path):
    with pytest.raises(bb.BriefError, match="dim_business_unit.parquet is missing"):
        bb.load(tmp_path)
    for table, cols in bb.REQUIRED.items():
        pd.DataFrame({c: [1] for c in cols}).to_parquet(tmp_path / f"{table}.parquet")
    assert set(bb.load(tmp_path)) == set(bb.REQUIRED)
    pd.DataFrame({"bu_id": ["x"]}).to_parquet(tmp_path / "dim_business_unit.parquet")
    with pytest.raises(bb.BriefError, match=r"dim_business_unit: missing column\(s\) name"):
        bb.load(tmp_path)


def test_main_reports_a_bad_gold_folder(tmp_path, capsys):
    assert bb.main(["--gold", str(tmp_path / "nothing"), "--out", str(tmp_path / "x.pdf")]) == 2
    assert "is missing" in capsys.readouterr().err


def test_the_pdf_carries_the_version_of_its_prompt():
    prompt = parse((ROOT / "prompts" / "reports" / "monthly-brief.md").read_text(encoding="utf-8"), "prompts/reports/monthly-brief.md")
    assert bb.prompt_version() == prompt.version == prompt.meta["version"]
    assert bb.prompt_version(ROOT / "nowhere") == "unknown"


@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    out = tmp_path_factory.mktemp("kit-out")
    ef.run_build(ROOT, "full", out)
    return out / "gold"


def test_the_pdf_is_reproducible_and_complete(gold, tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    assert bb.main(["--gold", str(gold), "--out", str(a), "--summary", str(tmp_path / "s.md")]) == 0
    assert bb.main(["--gold", str(gold), "--out", str(b)]) == 0
    raw = a.read_bytes()
    assert raw == b.read_bytes(), "the same data must give the same PDF"
    assert raw.startswith(b"%PDF") and b"/CreationDate" not in raw
    assert len(re.findall(rb"/Type\s*/Page(?![s\w])", raw)) == bb.PAGES
    summary = (tmp_path / "s.md").read_text(encoding="utf-8")
    assert summary.startswith(f"### Monthly Business Brief ({bb.PROMPT_ID} {bb.prompt_version()})")
    for title in ("Business units", "Revenue", "Portfolio, customers and service", "Campaigns", "Regions, risk and fraud"):
        assert f"- **{title}.**" in summary


def test_the_module_runs_from_the_command_line(gold, tmp_path):
    r = subprocess.run([sys.executable, "-m", "analysis.monthly_brief.build_brief", "--gold", str(gold), "--out", str(tmp_path / "cli.pdf")],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "cli.pdf").stat().st_size > 10_000
