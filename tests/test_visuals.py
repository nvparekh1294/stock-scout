"""tests/test_visuals.py — unit cases for brief-metric extraction (audit #4).
Plain asserts, no network:

    python -m pytest tests/test_visuals.py

The regression this pins: a TTM-EPS card once showed the PRIOR year because the
pack wrote "diluted EPS $4.10→$6.80" and a single-figure regex grabbed the first
$-value ($4.10) instead of the current-period $6.80. Yesterday's pack phrased it
"$6.80 diluted EPS" and parsed correctly — the fix must yield $6.80 for BOTH.
(All figures below are invented, not derived from any real security.)
"""

from __future__ import annotations

from scout import visuals


def test_progression_takes_current_period():
    # "$A→$B" reports the current period on the right.
    t = "sharp FY2026 margin expansion and EPS growth (diluted EPS $4.10→$6.80)."
    assert visuals._ttm_eps_from_text(t) == 6.80


def test_value_first_phrasing_yesterdays_pack():
    assert visuals._ttm_eps_from_text("NRDX earned $6.80 diluted EPS in FY2026.") == 6.80


def test_both_pack_phrasings_agree():
    a = visuals._ttm_eps_from_text("diluted EPS $4.10→$6.80")
    b = visuals._ttm_eps_from_text("$6.80 diluted EPS")
    assert a == b == 6.80, (a, b)


def test_vs_comparison_takes_current_side():
    # "$A vs $B" reports the current period on the LEFT.
    assert visuals._ttm_eps_from_text("Q1 diluted EPS $4.05 vs $2.10 PY.") == 4.05


def test_explicit_ttm_label_preferred():
    assert visuals._ttm_eps_from_text("TTM EPS $6.80 from the pack.") == 6.80
    assert visuals._ttm_eps_from_text("Reported EPS (ttm) of $6.80.") == 6.80


def test_forward_fiscal_year_eps_is_not_taken_as_ttm():
    # "implied FY2027 EPS ~$9.50" is a FORWARD estimate — the trailing figure in
    # the same body ($6.80) must win, not the forward year.
    t = ("EPS growth (diluted EPS $4.10→$6.80). Current price implies forward "
         "EPS ≈ $9.50 for implied FY2027 EPS ~$9.50.")
    assert visuals._ttm_eps_from_text(t) == 6.80


def test_no_eps_returns_none_never_invents():
    assert visuals._ttm_eps_from_text("No earnings figure was disclosed.") is None


def test_parse_metrics_sets_ttm_eps():
    m = visuals.parse_metrics_from_brief("diluted EPS $4.10→$6.80; P/S 9.2x")
    assert m.get("ttm_eps") == 6.80, m


# ── split-adjustment + PRICE-card date (2026-07-21 split-cliff fix) ───────────
class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


def _patch_bars(monkeypatch, weekly_payload, daily_payload):
    """Swap creds + requests.get so weekly_closes runs offline. Captures every
    call's params; returns the capture list. The 1Week request is answered with
    weekly_payload, the 1Day request with daily_payload."""
    monkeypatch.setattr(visuals, "_alpaca_creds", lambda: ("k", "s", "acct"))
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params or {})
        if (params or {}).get("timeframe") == "1Day":
            return _FakeResp(daily_payload)
        return _FakeResp(weekly_payload)

    monkeypatch.setattr(visuals.requests, "get", fake_get)
    return calls


def test_weekly_and_daily_bars_request_split_adjustment(monkeypatch):
    # Weekly bar stamped week-open Monday (2026-07-13); daily bar on the true
    # trading date (2026-07-17) — a split-unadjusted feed would also mix the range.
    weekly = {"bars": [{"t": "2026-07-06T00:00:00Z", "c": 60.0, "h": 61, "l": 59},
                       {"t": "2026-07-13T00:00:00Z", "c": 64.0, "h": 66, "l": 63}]}
    daily = {"bars": [{"t": "2026-07-17T00:00:00Z", "c": 65.08, "h": 66, "l": 64}]}
    calls = _patch_bars(monkeypatch, weekly, daily)
    series = visuals.weekly_closes("NFLX")
    # every bars request must carry adjustment=split (else split cliff / mixed range)
    assert calls, "no bars request captured"
    assert all(c.get("adjustment") == "split" for c in calls), calls
    # and both timeframes were fetched
    assert {c.get("timeframe") for c in calls} == {"1Week", "1Day"}, calls
    # PRICE = latest DAILY close, price_asof = its true date (NOT the Monday stamp)
    assert series["price"] == 65.08, series
    assert series["price_asof"] == "2026-07-17", series
    assert series["asof"] == "2026-07-13", series  # weekly series date unchanged


def test_build_metrics_price_asof_comes_from_daily_bar(monkeypatch):
    weekly = {"bars": [{"t": "2026-07-13T00:00:00Z", "c": 64.0, "h": 66, "l": 63}]}
    daily = {"bars": [{"t": "2026-07-17T00:00:00Z", "c": 65.08, "h": 66, "l": 64}]}
    _patch_bars(monkeypatch, weekly, daily)
    metrics, _series = visuals.build_metrics("NFLX", "")
    assert metrics["price"] == 65.08, metrics
    assert metrics["price_asof"] == "2026-07-17", metrics  # daily date, not Monday
