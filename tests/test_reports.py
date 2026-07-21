"""tests/test_reports.py — the plain-English conclusion layer (2026-07-21).

Covers the rework that made the three verdicts mean different things and gave a
non-trading reader a clear takeaway:
  • verdict → plain display-label mapping (machine tokens NEVER renamed),
  • conviction rendered on the standardized 1–5 scale (N/5), tolerant of legacy,
  • parsing of the new Bottom line / Watching for / deep-dive fields, and that a
    legacy brief WITHOUT those fields still parses and renders (fields omitted).

    python -m pytest tests/test_reports.py

(All tickers/figures below are invented, not derived from any real security.)
"""

from __future__ import annotations

from scout import reports


# ── verdict display labels ───────────────────────────────────────────────────
def test_verdict_label_maps_the_three_verdicts():
    assert reports.verdict_label("UNDERWRITE").startswith("COMPELLING")
    assert reports.verdict_label("WATCH").startswith("NOT YET")
    assert reports.verdict_label("PASS").startswith("NO EDGE")


def test_verdict_label_quick_take_candidate():
    assert reports.verdict_label("UNDERWRITE-CANDIDATE").startswith("WORTH A DEEPER LOOK")


def test_verdict_label_unknown_and_empty_pass_through():
    assert reports.verdict_label("REVIEW") == "REVIEW"   # unknown token unchanged
    assert reports.verdict_label(None) == "—"


def test_verdict_cell_appends_machine_token_for_provenance():
    cell = reports._verdict_cell("UNDERWRITE")
    assert "COMPELLING" in cell and "(UNDERWRITE)" in cell


def test_internal_verdict_tokens_are_not_renamed():
    # parse_header must still return the RAW machine token — persistence and the
    # _STATUS_FOR_VERDICT / _DECISION_BY_VERDICT maps depend on it.
    body = "## AAA — quick take as of 2026-07-20\n- Read: UNDERWRITE\n"
    assert reports.parse_header(body)["verdict"] == "UNDERWRITE"


# ── conviction on the 1–5 scale ──────────────────────────────────────────────
def test_conviction_bare_number_becomes_n_over_5():
    assert reports._conviction_display("3") == "3/5"


def test_conviction_keeps_explicit_denominator():
    assert reports._conviction_display("4/5") == "4/5"
    assert reports._conviction_display("4 / 5") == "4/5"


def test_conviction_legacy_1_to_10_passes_through_untouched():
    # A legacy brief written on the old 1–10 scale must never mis-render as "7/5".
    assert reports._conviction_display("7") == "7"


def test_conviction_none_is_dash():
    assert reports._conviction_display(None) == "—"


# ── new conclusion fields ────────────────────────────────────────────────────
_BODY_NEW = """## HLXR — quick take as of 2026-07-20
- Stage & direction: 1 re-rating
- Read: WATCH
- Conviction: 3/5
- Watching for: Q3 FY2027 revenue growth ≥ 25% YoY, ~Nov 2026

3 most decision-relevant facts: invented, dated 2026-07-20.

Worth a full deep-dive? No — the edge is not yet confirmed by a printed number.
Bottom line: The evidence leans slightly positive but nothing is proven yet. The biggest reason is a fast-growing order book. The view changes if those orders convert to sales slowly.
"""

_BODY_LEGACY = """## OLDX — quick take as of 2026-01-10
- Read: PASS
- Conviction: 2/5

Nothing provable here as of 2026-01-10.
"""


def test_parse_conclusion_fields_reads_all_three():
    f = reports.parse_conclusion_fields(_BODY_NEW)
    assert f["bottom_line"].startswith("The evidence leans slightly positive")
    assert f["watching_for"].startswith("Q3 FY2027 revenue growth")
    assert f["deep_dive"].startswith("No — the edge is not yet confirmed")


def test_parse_conclusion_fields_tolerant_when_absent():
    f = reports.parse_conclusion_fields(_BODY_LEGACY)
    assert f == {"bottom_line": None, "watching_for": None, "deep_dive": None}


# ── short-brief rendering ─────────────────────────────────────────────────────
def test_short_brief_shows_plain_label_conviction_and_conclusion():
    out = reports.render_short_brief("HLXR", "2026-07-20", "quick", _BODY_NEW,
                                     "checker ok", pack_name="", n_evidence=5)
    assert "NOT YET — interesting, but no clear edge today (WATCH)" in out
    assert "**Conviction:** 3/5" in out
    assert "Conviction scale:" in out                     # legend caption present
    assert "> **Bottom line:** The evidence leans slightly positive" in out
    assert "> **Watching for:** Q3 FY2027 revenue growth" in out
    assert "> **Worth a full deep-dive?** No — the edge" in out


def test_short_brief_legacy_still_renders_without_new_fields():
    out = reports.render_short_brief("OLDX", "2026-01-10", "quick", _BODY_LEGACY,
                                     "checker ok")
    assert "NO EDGE — nothing here suggests it's mispriced (PASS)" in out
    assert "**Bottom line:**" not in out                  # nothing fabricated
    assert "**Watching for:**" not in out


# ── full-brief rendering ─────────────────────────────────────────────────────
_UNDERWRITE_NEW = """## NRDX — underwrite as of 2026-07-14
Stage: 1 — early recognition (re-rating) · Conviction: 4/5 (2-4yr hold) · Verdict: UNDERWRITE

Thesis in three sentences: invented, dated 2026-07-14.

Suggested sizing posture: initiate small, ~0.5% of NAV.
Bottom line: On balance the evidence leans positive, if one thing proves true. The biggest reason is a committed order book growing faster than estimates assume. The view flips if the backlog converts slowly.
"""


def test_full_brief_shows_plain_label_and_conviction_and_bottom_line():
    out = reports.render_full_brief("NRDX", "2026-07-14", _UNDERWRITE_NEW,
                                    "Independent verdict: agree.\n\nThe adversary "
                                    "reviewed the pack and brief and broadly agrees "
                                    "with the underwrite for the reasons stated.",
                                    "checker ok", pack_name="", n_evidence=9)
    assert "COMPELLING" in out and "(UNDERWRITE)" in out
    # Tightened: conviction must land in its OWN header cell (a single-line header
    # must not let the Stage cell swallow conviction+verdict).
    assert "| **Conviction (2–4yr)** | 4/5 |" in out
    assert "| **Stage** | 1 — early recognition (re-rating) |" in out
    assert "Conviction scale:" in out
    assert "> **Bottom line:** On balance the evidence leans positive" in out


def test_single_line_header_round_trips_cleanly():
    # The deployed model may emit Stage · Conviction · Verdict on ONE line. Each
    # field must parse into its own cell — conviction is not None, verdict parses,
    # and the Stage cell does not swallow the rest of the line.
    body = ("## ZZZ — underwrite as of 2026-07-20\n"
            "Stage: 1 — early recognition (re-rating) · Conviction: 4/5 (2-4yr hold)"
            " · Verdict: UNDERWRITE\n\nThesis: invented, dated 2026-07-20.\n")
    h = reports.parse_header(body)
    assert h["verdict"] == "UNDERWRITE"
    assert reports._conviction_display(h["conviction"]) == "4/5"
    assert h["stage"] == "1 — early recognition (re-rating)"
    assert "Conviction" not in h["stage"] and "Verdict" not in h["stage"]


def test_full_brief_legacy_without_bottom_line_still_renders():
    legacy = ("## LEG — underwrite as of 2026-01-01\nStage: 2 · "
              "Conviction (1-10) for a 2-4yr hold: 6 · Verdict: WATCH\n\n"
              "Thesis: invented.\nSuggested sizing posture: small.\n")
    out = reports.render_full_brief("LEG", "2026-01-01", legacy,
                                    "Independent verdict: the adversary reviewed "
                                    "the pack and brief and agrees it is a WATCH, "
                                    "not an underwrite, for the reasons stated.",
                                    "checker ok")
    assert "NOT YET" in out and "(WATCH)" in out
    assert "6" in out                                     # legacy conviction shown
    assert "> **Bottom line:**" not in out
