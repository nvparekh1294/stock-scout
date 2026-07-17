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
