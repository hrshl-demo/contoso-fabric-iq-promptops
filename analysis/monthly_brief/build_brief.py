"""Monthly Business Brief: a PDF about the Contoso retail bank, built offline from the gold layer.

    python -m analysis.monthly_brief.build_brief --gold out/gold --out out/reports/monthly-brief.pdf [--summary summary.md]

Built by the prompt reports.monthly-brief. Deterministic: the same gold tables give the same PDF in the same environment. It reads
six gold tables of the kit's full tier, makes no network call and never touches Fabric or Azure. Money is INR crore (1 crore =
10,000,000 rupees), a reporting aggregate and not exact paise.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

PROMPT_ID = "reports.monthly-brief"
TITLE = "Contoso Bank - Monthly Business Brief"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
HORIZON = 3
OUTLIER_Z = 3.5
PAGES = 6

INK, MUTED, GRID = "#1f2933", "#616e7c", "#d9dee3"
GOOD, WARN, BAD, INFO = "#1b7f4b", "#b7791f", "#c0392b", "#2b6cb0"
PALETTE = ["#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#56B4E9"]       # Okabe-Ito: distinguishable without colour vision
FILL_GOOD, FILL_WARN, FILL_BAD = "#dff3e6", "#fdf0d5", "#fbdcd8"

REQUIRED = {
    "dim_business_unit": ["bu_id", "name", "short_name", "portfolio_crore_inr", "customers", "nim_pct", "yoy_growth_pct",
                          "target_attainment_pct", "health_status"],
    "fact_monthly_metric": ["bu_id", "month", "portfolio_crore_inr", "revenue_crore_inr", "new_customers", "cases_opened", "cases_resolved"],
    "dim_campaign": ["name", "bu_id", "status", "channel", "reach", "engaged", "converted", "conversion_pct", "revenue_attributed_crore_inr"],
    "dim_region": ["region_id", "city", "branches", "customers", "deposits_crore_inr", "advances_crore_inr", "yoy_growth_pct",
                   "nps_score", "digital_adoption_pct"],
    "fact_risk_alert": ["alert_id", "title", "severity", "bu_id", "raised_at"],
    "fact_fraud_signal": ["signal_id", "label", "detected_last_30d", "blocked_last_30d", "trend"],
}


class BriefError(RuntimeError):
    """The gold tables are missing or do not have the expected columns."""


# --------------------------------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------------------------------
def load(gold: Path) -> dict[str, pd.DataFrame]:
    data: dict[str, pd.DataFrame] = {}
    for table, columns in REQUIRED.items():
        f = Path(gold) / f"{table}.parquet"
        if not f.is_file():
            raise BriefError(f"{f} is missing; build the full tier first (python -m contoso_onto.cli build --tier full)")
        df = pd.read_parquet(f)
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise BriefError(f"{table}: missing column(s) {', '.join(missing)}")
        data[table] = df.reset_index(drop=True)
    return data


def prompt_version(root: Optional[Path] = None) -> str:
    """The version in the frontmatter of the prompt that owns this report, so the PDF says which prompt produced it."""
    base = root or Path(__file__).resolve().parents[2]
    f = base / "prompts" / "reports" / "monthly-brief.md"
    m = re.search(r"^version:\s*(\S+)\s*$", f.read_text(encoding="utf-8"), re.M) if f.is_file() else None
    return m.group(1) if m else "unknown"


# --------------------------------------------------------------------------------------------------
# statistics: a straight-line trend with a prediction band, and a robust outlier test on what the line does not explain
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Trend:
    fitted: np.ndarray
    slope: float
    sigma: float
    forecast: np.ndarray
    lower: np.ndarray
    upper: np.ndarray
    outliers: np.ndarray


def robust_outliers(residuals: np.ndarray, threshold: float = OUTLIER_Z) -> np.ndarray:
    """Modified z-score (median and MAD, Iglewicz and Hoaglin): a month is unusual when it is more than `threshold` away."""
    med = np.median(residuals)
    mad = np.median(np.abs(residuals - med))
    if mad == 0:
        return np.zeros(len(residuals), dtype=bool)
    return np.abs(0.6745 * (residuals - med) / mad) > threshold


def analyse(y: np.ndarray, horizon: int = HORIZON, z: float = 1.96) -> Trend:
    """Ordinary least squares on the month index; the band is the usual approximate 95% prediction interval."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n < 4:
        raise BriefError(f"a trend needs at least 4 months, got {n}")
    x = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    fitted = intercept + slope * x
    resid = y - fitted
    sigma = float(np.sqrt(resid @ resid / (n - 2)))
    xf = np.arange(n, n + horizon, dtype=float)
    pred = intercept + slope * xf
    sxx = float(((x - x.mean()) ** 2).sum())
    se = sigma * np.sqrt(1.0 + 1.0 / n + (xf - x.mean()) ** 2 / sxx)
    return Trend(fitted, float(slope), sigma, pred, pred - z * se, pred + z * se, robust_outliers(resid))


# --------------------------------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------------------------------
def add_months(month: str, k: int) -> str:
    idx = int(month[:4]) * 12 + int(month[5:7]) - 1 + k
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def label(month: str) -> str:
    return f"{MONTHS[int(month[5:7]) - 1]} {month[2:4]}"


def crore(x: float) -> str:
    return f"{x:,.1f}"


def share(num: float, den: float) -> float:
    return 100.0 * num / den if den else 0.0


def wrap(text: str, width: int) -> str:
    return textwrap.fill(str(text), width)


def day_label(ts: pd.Timestamp) -> str:
    return f"{ts.day:02d} {MONTHS[ts.month - 1]} {ts.year}"


# --------------------------------------------------------------------------------------------------
# headlines: one plain-English sentence or two above each page, computed from the data
# --------------------------------------------------------------------------------------------------
def headline_units(bu: pd.DataFrame) -> str:
    top = bu.sort_values("portfolio_crore_inr", ascending=False).iloc[0]
    s = (f"The {len(bu)} business units hold INR {crore(bu.portfolio_crore_inr.sum())} crore; {top['name']} is the largest at "
         f"INR {crore(top.portfolio_crore_inr)} crore. ")
    below = bu[bu.target_attainment_pct < 100].sort_values("target_attainment_pct")
    if below.empty:
        return s + "Every unit is at or above its target."
    names = ", ".join(f"{r['name']} ({r.target_attainment_pct:.1f}%)" for _, r in below.iterrows())
    return s + f"{len(below)} of {len(bu)} units are below target: {names}."


def unusual_months(piv: pd.DataFrame, names: dict[str, str]) -> list[str]:
    """Flagged months, most widespread first: 'Dec 25 (3 units)' or 'Oct 25 (Digital & Payments)'."""
    by_month: dict[str, list[str]] = {}
    for bu_id in piv.columns:
        t = analyse(piv[bu_id].to_numpy(float))
        for m, flag in zip(piv.index, t.outliers):
            if flag:
                by_month.setdefault(m, []).append(names.get(bu_id, bu_id))
    return [f"{label(m)} ({len(by_month[m])} units)" if len(by_month[m]) > 1 else f"{label(m)} ({by_month[m][0]})"
            for m in sorted(by_month, key=lambda k: (-len(by_month[k]), k))]


def headline_revenue(months: list[str], total: np.ndarray, trend: Trend, unusual: list[str]) -> str:
    fut = [add_months(months[-1], k) for k in range(1, HORIZON + 1)]
    s = (f"Monthly revenue grew {share(total[-1] - total[0], total[0]):.1f}% from {label(months[0])} to {label(months[-1])}, reaching "
         f"INR {crore(total[-1])} crore. If the straight-line trend holds, {label(fut[0])} to {label(fut[-1])} averages about "
         f"INR {crore(float(trend.forecast.mean()))} crore a month (95% range {crore(float(trend.lower.min()))} to {crore(float(trend.upper.max()))}).")
    if unusual:
        s += " Unusual months: " + "; ".join(unusual) + "."
    return s


def headline_growth(piv_portfolio: pd.DataFrame, mm: pd.DataFrame, names: dict[str, str]) -> str:
    first, last = piv_portfolio.iloc[0], piv_portfolio.iloc[-1]
    growth = {b: share(last[b] - first[b], first[b]) for b in piv_portfolio.columns}
    best, worst = max(growth, key=growth.get), min(growth, key=growth.get)
    opened, resolved = mm.cases_opened.sum(), mm.cases_resolved.sum()
    return (f"Portfolios grew {share(last.sum() - first.sum(), first.sum()):.1f}% over the year to INR {crore(last.sum())} crore: "
            f"{names[best]} grew fastest ({growth[best]:+.1f}%) and {names[worst]} {'shrank' if growth[worst] < 0 else 'grew least'} ({growth[worst]:+.1f}%). "
            f"Service teams resolved {share(resolved, opened):.1f}% of the {opened:,} cases opened in the period.")


def conversion_on_reach(ca: pd.DataFrame) -> pd.Series:
    """The semantic model's Campaign Conversion measure: converted divided by reach."""
    return 100.0 * ca.converted / ca.reach


def headline_campaigns(ca: pd.DataFrame) -> str:
    c = conversion_on_reach(ca)
    best = ca.loc[c.idxmax()]
    top_rev = ca.loc[ca.revenue_attributed_crore_inr.idxmax()]
    return (f"Across {len(ca)} campaigns, {share(ca.converted.sum(), ca.reach.sum()):.1f}% of the people reached converted. "
            f"{best['name']} converts best ({c.max():.1f}% of reach) and {top_rev['name']} attributes the most revenue "
            f"(INR {crore(top_rev.revenue_attributed_crore_inr)} crore). {int((ca.status == 'live').sum())} are live now.")


def headline_ops(rg: pd.DataFrame, ra: pd.DataFrame, fs: pd.DataFrame, names: dict[str, str]) -> str:
    top = rg.loc[rg.deposits_crore_inr.idxmax()]
    rate = fs.blocked_last_30d / fs.detected_last_30d
    weakest = fs.loc[rate.idxmin()]
    high = ra[ra.severity == "high"]
    alert = (f"{len(high)} high-severity risk alert is open ({names.get(high.iloc[0].bu_id, high.iloc[0].bu_id)}). " if len(high) == 1
             else f"{len(high)} high-severity risk alerts are open. ")
    return (f"{top.city} holds the most deposits (INR {crore(top.deposits_crore_inr)} crore). {alert}"
            f"Of {int(fs.detected_last_30d.sum()):,} fraud attempts detected in the last 30 days, {share(fs.blocked_last_30d.sum(), fs.detected_last_30d.sum()):.1f}% were blocked; "
            f"{weakest['label'].lower()} are the hardest to stop ({100 * rate.loc[weakest.name]:.1f}% blocked, trend {weakest['trend']}).")


# --------------------------------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------------------------------
def style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 8.5, "axes.edgecolor": GRID, "axes.labelcolor": MUTED, "axes.titlesize": 9.5,
        "axes.titleweight": "bold", "axes.titlecolor": INK, "axes.spines.top": False, "axes.spines.right": False,
        "xtick.color": MUTED, "ytick.color": MUTED, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
        "axes.axisbelow": True, "pdf.fonttype": 42, "figure.dpi": 100, "savefig.dpi": 100,
    })


def frame(title: str, headline: str, page: int, version: str) -> Figure:
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.text(0.05, 0.955, "CONTOSO BANK  ·  MONTHLY BUSINESS BRIEF", fontsize=8, color=MUTED, weight="bold")
    fig.text(0.05, 0.915, title, fontsize=18, weight="bold", color=INK, va="center")
    fig.text(0.05, 0.875, wrap(headline, 128), fontsize=10, color=INK, va="top", linespacing=1.35)
    fig.text(0.05, 0.022, f"Source: gold layer of the Contoso Bank ontology demo (synthetic data). Prompt {PROMPT_ID} {version}.", fontsize=7, color=MUTED)
    fig.text(0.95, 0.022, f"Page {page} of {PAGES}", fontsize=7, color=MUTED, ha="right")
    return fig


def draw_table(ax, cols: list[str], rows: list[list[str]], widths: list[float], row_in: float, fills=None, aligns=None, fontsize: float = 8,
               header_in: Optional[float] = None) -> None:
    """A table drawn in axes coordinates; widths sum to 1, row_in and header_in are heights in inches, fills[r][c] an optional cell colour.
    Header text may hold a line break."""
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    height_in = ax.get_position().height * ax.figure.get_figheight()
    rh, hh = row_in / height_in, (header_in or row_in) / height_in
    edges = np.r_[0, np.cumsum(widths)]
    aligns = aligns or ["left"] * len(cols)
    for r in range(len(rows) + 1):
        h = hh if r == 0 else rh
        top = 1 - (0 if r == 0 else hh + (r - 1) * rh)
        for c in range(len(cols)):
            fill = "#e4e9ee" if r == 0 else (fills[r - 1][c] if fills and fills[r - 1][c] else ("#ffffff" if r % 2 else "#f7f9fb"))
            ax.add_patch(Rectangle((edges[c], top - h), widths[c], h, facecolor=fill, edgecolor=GRID, linewidth=0.5))
            x = edges[c] + (0.008 if aligns[c] == "left" else widths[c] - 0.008 if aligns[c] == "right" else widths[c] / 2)
            ax.text(x, top - h / 2, cols[c] if r == 0 else rows[r - 1][c], ha=aligns[c], va="center", fontsize=fontsize,
                    weight="bold" if r == 0 else "normal", color=INK, linespacing=1.15)


def spread(values: list[float], min_gap: float) -> list[float]:
    """Nudge label positions apart (keeping their order) so end-of-line labels do not overlap."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    pos = [values[i] for i in order]
    for k in range(1, len(pos)):
        if pos[k] - pos[k - 1] < min_gap:
            pos[k] = pos[k - 1] + min_gap
    out = [0.0] * len(values)
    for k, i in enumerate(order):
        out[i] = pos[k]
    return out


def thousands(ax, axis: str = "y") -> None:
    fmt = FuncFormatter(lambda v, _: f"{v:,.0f}")
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(fmt)


def plot_trend(ax, y: np.ndarray, t: Trend, colour: str, labels: list[str], big: bool = False) -> None:
    n, h = len(y), len(t.forecast)
    x, xf = np.arange(n), np.arange(n, n + h)
    ax.plot(x, t.fitted, color=MUTED, lw=0.9, ls="--", zorder=1)
    ax.plot(x, y, color=colour, lw=2.0 if big else 1.4, marker="o" if big else None, ms=3.5, zorder=3)
    ax.plot(np.r_[n - 1, xf], np.r_[y[-1], t.forecast], color=colour, lw=1.6, ls=":", marker="o", ms=3.5 if big else 2.2, zorder=3)
    ax.fill_between(xf, t.lower, t.upper, color=colour, alpha=0.2, lw=0, zorder=2)
    if t.outliers.any():
        ax.scatter(x[t.outliers], y[t.outliers], s=80 if big else 42, facecolors="none", edgecolors=BAD, linewidths=1.6, zorder=5)
    ax.set_xlim(-0.6, n + h - 0.4)
    ticks = list(range(n + h)) if big else [0, n - 1, n + h - 1]
    ax.set_xticks(ticks, labels=[labels[i] for i in ticks])
    thousands(ax)


def page_units(d: dict, colour: dict, version: str) -> Figure:
    bu = d["dim_business_unit"].sort_values("portfolio_crore_inr", ascending=False).reset_index(drop=True)
    fig = frame("Business units", headline_units(bu), 1, version)
    gs = GridSpec(1, 2, figure=fig, width_ratios=[1, 1.45], left=0.17, right=0.95, top=0.73, bottom=0.08, wspace=0.12)
    ax = fig.add_subplot(gs[0])
    y = np.arange(len(bu))[::-1]
    ax.barh(y, bu.portfolio_crore_inr, color=[colour[b] for b in bu.bu_id], height=0.62)
    ax.set_yticks(y, labels=list(bu["name"]))
    for yi, v in zip(y, bu.portfolio_crore_inr):
        ax.text(v, yi, f"  {crore(v)}", va="center", fontsize=8, color=INK)
    ax.set_xlim(0, bu.portfolio_crore_inr.max() * 1.25)
    ax.set_title("Portfolio, INR crore", loc="left")
    ax.grid(axis="y", visible=False)
    thousands(ax, "x")

    right = gs[1].subgridspec(2, 1, height_ratios=[1, 1], hspace=0.4)
    cols = ["Unit", "Customers", "NIM %", "YoY\ngrowth %", "Target\nattained %", "Health"]
    rows, fills = [], []
    for _, r in bu.iterrows():
        rows.append([r["name"], f"{int(r.customers):,}", f"{r.nim_pct:.2f}", f"{r.yoy_growth_pct:+.1f}", f"{r.target_attainment_pct:.1f}", r.health_status])
        t = r.target_attainment_pct
        h = {"strong": FILL_GOOD, "watch": FILL_BAD}.get(r.health_status, FILL_WARN)
        fills.append([None, None, None, FILL_BAD if r.yoy_growth_pct < 0 else None,
                      FILL_GOOD if t >= 100 else FILL_WARN if t >= 90 else FILL_BAD, h])
    ax2 = fig.add_subplot(right[0])
    draw_table(ax2, cols, rows, [0.27, 0.16, 0.1, 0.15, 0.18, 0.14], 0.3, fills, ["left", "right", "right", "right", "right", "center"], 8.2, header_in=0.42)
    ax2.set_title("Scorecard (target attained: green 100% or more, amber 90 to 100%, red under 90%)", loc="left", fontsize=8.5, color=MUTED, weight="normal")

    ax3 = fig.add_subplot(right[1])
    size = 60 + 900 * bu.portfolio_crore_inr / bu.portfolio_crore_inr.max()
    ax3.scatter(bu.target_attainment_pct, bu.yoy_growth_pct, s=size, c=[colour[b] for b in bu.bu_id], alpha=0.8, edgecolors="white", zorder=3)
    for _, r in bu.iterrows():
        ax3.annotate(r.short_name, (r.target_attainment_pct, r.yoy_growth_pct), textcoords="offset points", xytext=(8, 6), fontsize=7.5, color=INK)
    ax3.axvline(100, color=MUTED, lw=0.9, ls="--", zorder=1)
    ax3.axhline(0, color=MUTED, lw=0.9, ls="--", zorder=1)
    ax3.set_xlabel("Target attained, %  (right of the dashed line: on or above target)")
    ax3.set_ylabel("Year-on-year growth, %")
    ax3.set_title("Growth against target (bubble: portfolio)", loc="left")
    ax3.margins(0.18)
    return fig


def month_view(d: dict) -> tuple[list[str], list[str], pd.DataFrame]:
    mm = d["fact_monthly_metric"]
    months = sorted(mm.month.unique())
    return months, months + [add_months(months[-1], k) for k in range(1, HORIZON + 1)], mm


def page_revenue(d: dict, colour: dict, names: dict, shorts: dict, version: str) -> Figure:
    months, all_months, mm = month_view(d)
    labels = [label(m) for m in all_months]
    piv = mm.pivot(index="month", columns="bu_id", values="revenue_crore_inr").loc[months]
    total = piv.sum(axis=1).to_numpy(float)
    t_total = analyse(total)
    fig = frame("Revenue: trend and the next three months", headline_revenue(months, total, t_total, unusual_months(piv, names)), 2, version)
    gs = GridSpec(3, 3, figure=fig, height_ratios=[1.35, 1, 1], left=0.06, right=0.95, top=0.73, bottom=0.1, hspace=0.62, wspace=0.28)
    ax = fig.add_subplot(gs[0, :])
    plot_trend(ax, total, t_total, INFO, labels, big=True)
    ax.set_title("All business units, INR crore a month", loc="left")
    ax.legend(handles=[
        Line2D([], [], color=INFO, lw=2, marker="o", ms=3.5, label="Actual"),
        Line2D([], [], color=MUTED, lw=0.9, ls="--", label="Straight-line trend"),
        Line2D([], [], color=INFO, lw=1.6, ls=":", marker="o", ms=3.5, label="Forecast with 95% band"),
        Line2D([], [], color=BAD, lw=0, marker="o", mfc="none", ms=7, label="Unusual month")],
        loc="upper left", frameon=False, fontsize=7.5, ncol=4)
    for i, bu_id in enumerate(sorted(piv.columns)):
        a = fig.add_subplot(gs[1 + i // 3, i % 3])
        y = piv[bu_id].to_numpy(float)
        t = analyse(y)
        plot_trend(a, y, t, colour[bu_id], labels)
        a.set_title(f"{shorts[bu_id]}  ({t.slope:+.1f} a month)", loc="left", fontsize=8.5)
    return fig


def page_growth(d: dict, colour: dict, names: dict, shorts: dict, version: str) -> Figure:
    months, _, mm = month_view(d)
    labels = [label(m) for m in months]
    port = mm.pivot(index="month", columns="bu_id", values="portfolio_crore_inr").loc[months]
    fig = frame("Portfolio, customers and service load", headline_growth(port, mm, names), 3, version)
    gs = GridSpec(2, 2, figure=fig, left=0.06, right=0.95, top=0.73, bottom=0.1, hspace=0.52, wspace=0.2)
    ax = fig.add_subplot(gs[0, 0])
    idx = 100 * port / port.iloc[0]
    ends = spread([float(idx[b].iloc[-1]) for b in sorted(idx.columns)], 2.2)
    for b, end in zip(sorted(idx.columns), ends):
        ax.plot(range(len(months)), idx[b], color=colour[b], lw=1.6)
        ax.text(len(months) - 0.8, end, f" {shorts[b]}", color=colour[b], fontsize=7, va="center")
    ax.set_xticks([0, len(months) // 2, len(months) - 1], labels=[labels[0], labels[len(months) // 2], labels[-1]])
    ax.set_xlim(-0.3, len(months) + 2.2)
    ax.set_title(f"Portfolio, index ({labels[0]} = 100)", loc="left")

    ax = fig.add_subplot(gs[0, 1])
    new = mm.groupby("month").new_customers.sum().loc[months] / 1000
    ax.bar(range(len(months)), new, color="#486581", width=0.7)
    ax.set_xticks(range(0, len(months), 2), labels=[labels[i] for i in range(0, len(months), 2)])
    ax.set_title("New customers, all units, thousands", loc="left")
    ax.grid(axis="x", visible=False)

    ax = fig.add_subplot(gs[1, 0])
    opened = mm.groupby("month").cases_opened.sum().loc[months]
    resolved = mm.groupby("month").cases_resolved.sum().loc[months]
    ax.plot(range(len(months)), opened, color=WARN, lw=1.8, marker="o", ms=3, label="Opened")
    ax.plot(range(len(months)), resolved, color=GOOD, lw=1.8, marker="o", ms=3, label="Resolved")
    ax.set_xticks([0, len(months) // 2, len(months) - 1], labels=[labels[0], labels[len(months) // 2], labels[-1]])
    ax.set_title("Service cases a month, all units", loc="left")
    ax.legend(frameon=False, fontsize=7.5, loc="upper left", ncol=2)
    thousands(ax)

    ax = fig.add_subplot(gs[1, 1])
    rows, fills = [], []
    for b in sorted(port.columns):
        sub = mm[mm.bu_id == b].sort_values("month")
        rev_first, rev_last = sub.revenue_crore_inr.iloc[0], sub.revenue_crore_inr.iloc[-1]
        p = share(port[b].iloc[-1] - port[b].iloc[0], port[b].iloc[0])
        r = share(rev_last - rev_first, rev_first)
        res = share(sub.cases_resolved.sum(), sub.cases_opened.sum())
        rows.append([shorts[b], f"{p:+.1f}", f"{r:+.1f}", f"{int(sub.new_customers.sum()):,}", f"{res:.1f}"])
        fills.append([None, FILL_BAD if p < 0 else None, FILL_BAD if r < 0 else None, None, FILL_WARN if res < 95 else None])
    draw_table(ax, ["Unit", "Portfolio\nchange %", "Revenue\nchange %", "New\ncustomers", "Cases\nresolved %"], rows,
               [0.28, 0.2, 0.2, 0.17, 0.15], 0.255, fills, ["left", "right", "right", "right", "right"], 8, header_in=0.4)
    ax.set_title("Change over the period, by unit", loc="left")
    return fig


def page_campaigns(d: dict, shorts: dict, version: str) -> Figure:
    ca = d["dim_campaign"].sort_values("reach").reset_index(drop=True)
    fig = frame("Campaigns", headline_campaigns(ca), 4, version)
    top = GridSpec(1, 2, figure=fig, left=0.21, right=0.95, top=0.72, bottom=0.47, wspace=0.12)
    ax = fig.add_subplot(top[0, 0])
    y = np.arange(len(ca))
    h = 0.26
    for off, col, lab, fill in ((h, "reach", "Reach", "#9fb3c8"), (0, "engaged", "Engaged", "#486581"), (-h, "converted", "Converted", GOOD)):
        ax.barh(y + off, ca[col], h, color=fill, label=lab)
        for yi, v in zip(y + off, ca[col]):
            ax.text(v, yi, f" {int(v):,}", va="center", fontsize=6.5, color=INK)
    ax.set_xscale("log")
    ax.set_xlim(300, 2.2e6)
    ax.set_yticks(y, labels=[wrap(n, 26) for n in ca["name"]], fontsize=7.5)
    ax.set_title("People reached, engaged and converted (log scale)", loc="left")
    ax.legend(frameon=False, fontsize=7.5, loc="lower right")
    ax.grid(axis="y", visible=False)

    ax = fig.add_subplot(top[0, 1])
    c = conversion_on_reach(ca)
    ax.barh(y, c, 0.55, color=INFO)
    ax.scatter(ca.conversion_pct, y, marker="D", s=26, color=BAD, zorder=4)
    for yi, v in zip(y, c):
        ax.text(v, yi - 0.33, f"{v:.1f}%", fontsize=7, color=INFO, va="center")
    ax.set_yticks(y, labels=[])
    ax.set_xlim(0, max(ca.conversion_pct.max(), c.max()) * 1.18)
    ax.set_title("Two ways to count conversion, %", loc="left", pad=24)
    ax.text(0, 1.02, "bars: converted ÷ reach (the model's Campaign Conversion)\ndiamonds: converted ÷ engaged (source conversion_pct)",
            transform=ax.transAxes, fontsize=6.6, color=MUTED, va="bottom", linespacing=1.3)
    ax.grid(axis="y", visible=False)

    ax = fig.add_subplot(GridSpec(1, 1, figure=fig, left=0.05, right=0.95, top=0.38, bottom=0.06)[0, 0])
    rows, fills = [], []
    for _, r in ca.sort_values("revenue_attributed_crore_inr", ascending=False).iterrows():
        rows.append([wrap(r["name"], 34), r.channel, r.status, shorts.get(r.bu_id, r.bu_id), f"{int(r.reach):,}", f"{int(r.engaged):,}", f"{int(r.converted):,}",
                     f"{share(r.converted, r.reach):.1f}", f"{r.conversion_pct:.1f}", crore(r.revenue_attributed_crore_inr)])
        fills.append([None, None, FILL_GOOD if r.status == "live" else None] + [None] * 7)
    draw_table(ax, ["Campaign", "Channel", "Status", "Unit", "Reach", "Engaged", "Converted", "Conv. ÷\nreach %", "Conv. ÷\nengaged %", "Revenue,\nINR crore"], rows,
               [0.22, 0.1, 0.07, 0.11, 0.08, 0.08, 0.08, 0.08, 0.09, 0.09], 0.33, fills,
               ["left", "left", "left", "left", "right", "right", "right", "right", "right", "right"], 7.5, header_in=0.42)
    ax.set_title("All campaigns, largest revenue first", loc="left", pad=8)
    return fig


def page_ops(d: dict, shorts: dict, names: dict, version: str) -> Figure:
    rg = d["dim_region"].sort_values("deposits_crore_inr", ascending=False).reset_index(drop=True)
    ra = d["fact_risk_alert"].sort_values("raised_at", ascending=False).reset_index(drop=True)
    fs = d["fact_fraud_signal"].sort_values("detected_last_30d").reset_index(drop=True)
    fig = frame("Regions, risk and fraud", headline_ops(rg, ra, fs, names), 5, version)
    gs = GridSpec(2, 2, figure=fig, height_ratios=[1, 0.9], left=0.06, right=0.95, top=0.73, bottom=0.08, hspace=0.5, wspace=0.3)
    ax = fig.add_subplot(gs[0, 0])
    x = np.arange(len(rg))
    ax.bar(x - 0.2, rg.deposits_crore_inr, 0.4, color="#486581", label="Deposits")
    ax.bar(x + 0.2, rg.advances_crore_inr, 0.4, color="#E69F00", label="Advances")
    ax.set_xticks(x, labels=list(rg.city), rotation=30, ha="right", fontsize=7.5)
    ax.set_title("Deposits and advances by region, INR crore", loc="left")
    ax.legend(frameon=False, fontsize=7.5)
    ax.grid(axis="x", visible=False)
    thousands(ax)

    ax = fig.add_subplot(gs[0, 1])
    ax.scatter(rg.digital_adoption_pct, rg.nps_score, s=rg.customers / 1500, color=INFO, alpha=0.55, edgecolors="white")
    for _, r in rg.iterrows():
        ax.annotate(r.city, (r.digital_adoption_pct, r.nps_score), textcoords="offset points", xytext=(5, 4), fontsize=7, color=INK)
    ax.set_xlabel("Digital adoption, %")
    ax.set_ylabel("Net promoter score")
    ax.set_title("Digital adoption and satisfaction (bubble: customers)", loc="left")
    ax.margins(0.12)

    ax = fig.add_subplot(gs[1, 0])
    sev = {"high": (FILL_BAD, "High"), "caution": (FILL_WARN, "Caution"), "info": ("#dbe9f7", "Info")}
    rows, fills = [], []
    for _, r in ra.iterrows():
        rows.append([day_label(r.raised_at), sev.get(r.severity, (None, r.severity))[1], shorts.get(r.bu_id, r.bu_id), wrap(r.title, 36)])
        fills.append([None, sev.get(r.severity, (None, ""))[0], None, None])
    draw_table(ax, ["Raised", "Severity", "Unit", "Alert"], rows, [0.19, 0.14, 0.17, 0.5], 0.4, fills, ["left", "center", "left", "left"], 7.5)
    ax.set_title("Open risk alerts", loc="left")

    ax = fig.add_subplot(gs[1, 1])
    y = np.arange(len(fs))
    ax.barh(y + 0.19, fs.detected_last_30d, 0.36, color="#9fb3c8", label="Detected")
    ax.barh(y - 0.19, fs.blocked_last_30d, 0.36, color=GOOD, label="Blocked")
    arrows = {"up": "↑ rising", "down": "↓ falling", "flat": "→ flat"}
    for yi, (_, r) in zip(y, fs.iterrows()):
        ax.text(r.detected_last_30d, yi + 0.19, f"  {int(r.detected_last_30d):,} detected, {share(r.blocked_last_30d, r.detected_last_30d):.0f}% blocked, {arrows.get(r.trend, r.trend)}",
                va="center", fontsize=6.8, color=INK)
    ax.set_yticks(y, labels=[wrap(s, 20) for s in fs.label], fontsize=7.5)
    ax.set_xlim(0, fs.detected_last_30d.max() * 1.85)
    ax.set_title("Fraud attempts, last 30 days", loc="left")
    ax.legend(frameon=False, fontsize=7.5, loc="lower right")
    ax.grid(axis="y", visible=False)
    thousands(ax, "x")
    return fig


def page_method(d: dict, version: str) -> Figure:
    mm = d["fact_monthly_metric"]
    months = sorted(mm.month.unique())
    fig = frame("Method, definitions and limits", "How the numbers were produced, so every page can be checked and repeated.", 6, version)
    blocks = [
        ("Data", f"Six gold tables of the kit's full tier: dim_business_unit, fact_monthly_metric ({len(months)} months, {label(months[0])} to {label(months[-1])}), "
                 "dim_campaign, dim_region, fact_risk_alert and fact_fraud_signal. All data is synthetic. Money is INR crore (1 crore = 10,000,000 rupees), "
                 "a reporting aggregate and not exact paise. The figures are as published in the seed, not restated."),
        ("Definitions", "Revenue and portfolio are the monthly figures per business unit. Campaign conversion follows the semantic model's measure "
                        "Campaign Conversion: converted divided by reach. The source column conversion_pct divides converted by engaged, which gives larger "
                        "numbers; page 4 shows both and labels them. The model has no measure for the other pages' ratios, so they are plain sums and shares "
                        "of the table columns."),
        ("Forecast", f"A straight line (ordinary least squares) is fitted to each series and extended {HORIZON} months. The shaded band is the usual approximate 95% "
                     f"prediction interval. Limits: only {len(months)} points per series, no seasonality, no campaigns or events, and the band assumes steady, "
                     "normally distributed noise. Treat it as an illustration of direction, not a plan."),
        ("Unusual months", f"A month is marked when it is more than {OUTLIER_Z} robust standard deviations from the straight line (median and MAD of the residuals, "
                           "the modified z-score). The same month flagged in several units points to something bank-wide, such as a seasonal dip; a run of flagged "
                           "months in one unit usually means its level moved. Limit: with twelve points the test can miss a real change or flag a normal swing, "
                           "so it points to months worth a look."),
        ("Repeat it", "python -m contoso_onto.cli build --repo ../seed --tier full --out ../out  (from kit/), then "
                      "python -m analysis.monthly_brief.build_brief --gold out/gold. The same data gives the same PDF."),
    ]
    y = 0.80
    for head, body in blocks:
        fig.text(0.05, y, head, fontsize=10.5, weight="bold", color=INK, va="top")
        text = wrap(body, 150)
        fig.text(0.05, y - 0.03, text, fontsize=8.8, color=INK, va="top", linespacing=1.4)
        y -= 0.03 + 0.027 * (text.count("\n") + 1) + 0.035
    return fig


# --------------------------------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------------------------------
def build_pages(data: dict[str, pd.DataFrame], version: str) -> list[Figure]:
    style()
    bu = data["dim_business_unit"]
    ids = sorted(bu.bu_id)
    colour = {b: PALETTE[i % len(PALETTE)] for i, b in enumerate(ids)}
    names = dict(zip(bu.bu_id, bu["name"]))
    shorts = dict(zip(bu.bu_id, bu.short_name))
    return [page_units(data, colour, version), page_revenue(data, colour, names, shorts, version), page_growth(data, colour, names, shorts, version),
            page_campaigns(data, shorts, version), page_ops(data, shorts, names, version), page_method(data, version)]


def write_pdf(figs: list[Figure], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"Title": TITLE, "Author": "contoso-fabric-iq-promptops", "Subject": "Synthetic data only", "Creator": "analysis/monthly_brief",
            "Producer": "matplotlib", "CreationDate": None}
    with PdfPages(path, metadata=meta) as pdf:
        for fig in figs:
            pdf.savefig(fig)
            plt.close(fig)


def headlines(data: dict[str, pd.DataFrame]) -> list[tuple[str, str]]:
    bu = data["dim_business_unit"]
    names = dict(zip(bu.bu_id, bu["name"]))
    mm = data["fact_monthly_metric"]
    months = sorted(mm.month.unique())
    piv = mm.pivot(index="month", columns="bu_id", values="revenue_crore_inr").loc[months]
    total = piv.sum(axis=1).to_numpy(float)
    port = mm.pivot(index="month", columns="bu_id", values="portfolio_crore_inr").loc[months]
    ra = data["fact_risk_alert"].sort_values("raised_at", ascending=False)
    return [("Business units", headline_units(bu)),
            ("Revenue", headline_revenue(months, total, analyse(total), unusual_months(piv, names))),
            ("Portfolio, customers and service", headline_growth(port, mm, names)),
            ("Campaigns", headline_campaigns(data["dim_campaign"])),
            ("Regions, risk and fraud", headline_ops(data["dim_region"], ra, data["fact_fraud_signal"], names))]


def summary_markdown(data: dict[str, pd.DataFrame], pdf: Path, version: str) -> str:
    raw = pdf.read_bytes()
    lines = [f"### Monthly Business Brief ({PROMPT_ID} {version})", "",
             f"PDF: {PAGES} pages, {len(raw) / 1024:,.0f} KB, sha256 `{hashlib.sha256(raw).hexdigest()[:16]}`", ""]
    lines += [f"- **{title}.** {text}" for title, text in headlines(data)]
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", required=True, help="the kit's gold folder (out/gold of a full-tier build)")
    ap.add_argument("--out", default="out/reports/monthly-brief.pdf")
    ap.add_argument("--summary", help="also write a markdown summary (headlines, size, hash) to this file")
    a = ap.parse_args(argv)
    try:
        data = load(Path(a.gold))
        version = prompt_version()
        out = Path(a.out)
        write_pdf(build_pages(data, version), out)
        if a.summary:
            Path(a.summary).write_text(summary_markdown(data, out, version), encoding="utf-8", newline="\n")
    except BriefError as ex:
        print(f"monthly_brief: {ex}", file=sys.stderr)
        return 2
    print(f"monthly_brief: wrote {out} ({out.stat().st_size / 1024:,.0f} KB, {PAGES} pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
