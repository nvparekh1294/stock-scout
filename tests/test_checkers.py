"""tests/test_checkers.py — deterministic unit cases for the brief checkers.

    python -m pytest tests/test_checkers.py

Covers the four consistency checks:
  - stale-deadline: citation dates never flag; genuine deadlines do (both dirs);
  - scenario arithmetic: EPS-range × multiple → price-range math is parsed and an
    inconsistent range FLAGS while a consistent one PASSES;
  - trigger-vs-scenario: a buy entry above the valuation ceiling FLAGS, while a
    real brief's citations/pullbacks do not (the false-positive class);
  - temporal-claim + format-completeness.

Every ticker is invented and every figure below is a made-up test input; where a
line asserts a computed result it is recomputed from those inputs.
"""

from __future__ import annotations

from scout import checkers


def _arith(text: str) -> dict:
    return checkers.check_arithmetic(text)


def _mismatch_exprs(res: dict) -> list[str]:
    return [c["expr"] for c in res["flags"]]


# ── stale-deadline (citation vs deadline) ──────────────────────────────────
def test_stale_deadline_citation_does_not_flag():
    # A dated CITATION inside a break condition must not read as a lapsed deadline.
    text = (
        "## Break conditions\n"
        "- Backlog declines from the $1,840.0M level reported as of 2026-01-31 "
        "(8-K filed 2026-03-26) — would falsify the growth assumption.\n"
        "- Q1 FY2027 print (filed 2026-06-04, not yet reviewed) shows awards below pace.\n"
        "\n## Entry triggers\n"
        "- Extract and confirm Q1 FY2027 actuals (10-Q/8-K filed 2026-06-04).\n"
    )
    res = checkers.check_stale_deadlines(text, as_of="2026-07-12")
    assert res["passed"], f"citation dates should NOT flag, got: {res['flags']}"


def test_stale_deadline_genuine_deadline_flags():
    text = (
        "## Break conditions\n"
        "- If the Delta Ridge permit is not secured by 2026-03-31, the base case breaks.\n"
        "\n## Entry triggers\n"
        "- Enter only before 2026-05-01 while the discount persists.\n"
    )
    res = checkers.check_stale_deadlines(text, as_of="2026-07-12")
    assert not res["passed"], "past deadlines (by/before DATE) should FLAG"
    assert len(res["flags"]) == 2, f"expected 2 lapsed deadlines, got {res['flags']}"


def test_stale_deadline_future_deadline_ok():
    text = ("## Break conditions\n"
            "- If guidance is not raised by 2026-12-31, revisit the thesis.\n")
    res = checkers.check_stale_deadlines(text, as_of="2026-07-12")
    assert res["passed"], "a FUTURE deadline must not flag"


def test_stale_deadline_month_year_deadline_flags():
    text = ("## Break conditions\n"
            "- The contract must close before March 2026 or the catalyst lapses.\n")
    res = checkers.check_stale_deadlines(text, as_of="2026-07-12")
    assert not res["passed"], "a past month-year deadline should FLAG"


# ── scenario arithmetic (EPS range × multiple → price range) ────────────────
# Invented inputs; the bear HIGH endpoint is deliberately inconsistent
# (5 × 17 = 85, not the stated 102) so it must FLAG; base and bull are consistent.
NRDX_BEAR = ("- **Bear:** margins revert, EPS reverts toward ~$4–5, multiple "
             "compresses to a typical peer 17x → implied price ≈ $68–$102 "
             "(near the 52-week low).")
NRDX_BASE = ("- **Base:** EPS ~$9–10, multiple ~25x (growth premium, moderated) "
             "→ implied price ≈ $225–$250 — below the current level.")
NRDX_BULL = ("- **Bull:** margins hold, EPS ~$11–12, multiple sustains ~38x → "
             "implied price ≈ $418–$456, roughly the 52-week high.")


def test_scenario_bear_line_flags():
    res = _arith(NRDX_BEAR)
    assert not res["passed"], "bear line 5×17=85 not 102 — should FLAG"
    # the high endpoint is the inconsistent one
    assert any("high endpoint" in c["expr"] and c["status"] == "MISMATCH"
               for c in res["flags"]), _mismatch_exprs(res)


def test_scenario_bull_line_passes():
    res = _arith(NRDX_BULL)
    assert res["passed"], f"bull line 11×38=418, 12×38=456 — should PASS: {_mismatch_exprs(res)}"
    # and it WAS actually evaluated (not silently skipped)
    assert any("EPS×multiple" in c["expr"] for c in res["checks"]), \
        "bull scenario math must be parsed, not skipped"


def test_scenario_base_line_passes():
    res = _arith(NRDX_BASE)
    assert res["passed"], f"base line 9×25=225, 10×25=250 — should PASS: {_mismatch_exprs(res)}"


def test_scenario_all_three_together():
    # The three lines in one body: exactly one MISMATCH (the bear high endpoint).
    res = _arith(NRDX_BEAR + "\n" + NRDX_BASE + "\n" + NRDX_BULL)
    scen = [c for c in res["checks"] if "EPS×multiple" in c["expr"]]
    assert len(scen) == 6, f"3 lines × 2 endpoints = 6 scenario checks, got {len(scen)}"
    mism = [c for c in scen if c["status"] == "MISMATCH"]
    assert len(mism) == 1, _mismatch_exprs(res)
    # the sole inconsistency is the bear high endpoint: 5×17=85, stated 102
    assert abs(mism[0]["computed"] - 85.0) < 1e-6 and abs(mism[0]["stated"] - 102.0) < 1e-6, mism


# ── format completeness ────────────────────────────────────────────────────
def test_format_completeness_missing_premortem_flags():
    # A standard brief missing only the Pre-mortem.
    text = (
        "## NRDX — standard dive\n"
        "**Thesis in three sentences.** ...\n"
        "**What is still unpriced (cited).** nothing provable.\n"
        "**Variant view vs. consensus.** NOT FOUND.\n"
        "**4–5 most decision-relevant facts (dated, cited).** ...\n"
        "**Valuation (reverse-DCF framing).** ...\n"
        "**Break conditions (falsifiable, dated).** ...\n"
        "**Entry triggers if WATCH.** ...\n"
    )
    res = checkers.check_format_completeness(text, checkers.REQUIRED_SECTIONS["standard"])
    assert not res["passed"], "missing Pre-mortem must FLAG"
    assert any("Pre-mortem" in f for f in res["flags"]), res["flags"]
    assert len(res["flags"]) == 1, f"only Pre-mortem should be missing: {res['flags']}"


def test_format_completeness_all_present_passes():
    text = (
        "**Thesis in three sentences.** ...\n"
        "**What is still unpriced.** ...\n"
        "**Variant view vs. consensus.** ...\n"
        "**4–5 most decision-relevant facts.** ...\n"
        "**Valuation (reverse-DCF framing).** ...\n"
        "**Break conditions.** ...\n"
        "**Entry triggers.** ...\n"
        "**Pre-mortem.** ...\n"
    )
    res = checkers.check_format_completeness(text, checkers.REQUIRED_SECTIONS["standard"])
    assert res["passed"], f"all sections present should PASS: {res['flags']}"


def test_format_completeness_skipped_without_list():
    res = checkers.check_format_completeness("anything", None)
    assert res["passed"], "no requirement list → skipped/pass"


# ── trigger-vs-scenario (false-positive surface + true positives) ──────────
# A real-shaped brief: valuation multiple range, 52-week citations, a backlog
# figure on the base cue line, and a legitimate pullback entry — none may flag.
NRDX_VAL_AND_TRIGGERS = (
    "Valuation (reverse-DCF framing).\n"
    "A base case that assumes multiple compression toward 22–28x points to fair "
    "value closer to $250–$280 — i.e., I am more bearish than consensus.\n"
    "Bear: EPS flat ~$6.40, multiple ~14x. Price ~= $6.40 x 14 = ~$90 (-64%).\n"
    "Base: EPS grows to ~$8.10, multiple ~25x. Price ~= $8.10 x 25 = ~$203 (-19%).\n"
    "Bull: EPS ~$9.30, multiple holds ~38x. Price ~= $9.30 x 38 = ~$353-390 (+55%).\n"
    "\n## Entry triggers if WATCH\n"
    "- Confirm Q1 FY2027 actuals (10-Q/8-K filed 2026-06-04) with backlog "
    "$1,840M intact — re-price against the base case (~$203) before entry.\n"
    "- A pullback toward the $250-280 base-case band (from the 52-wk range "
    "$118.40 - $402.75) offers a better margin of safety.\n"
)


def test_trigger_no_false_positive_on_multiple_and_citations():
    res = checkers.check_trigger_consistency(NRDX_VAL_AND_TRIGGERS)
    assert res["passed"], f"the citation/pullback lines must all PASS: {res['flags']}"


def test_trigger_multiple_range_not_read_as_price_band():
    # "22–28x" must never become a $22–$28 ceiling that a $203 trigger 'exceeds'.
    res = checkers.check_trigger_consistency(NRDX_VAL_AND_TRIGGERS)
    assert not any("28.00" in f for f in res["flags"]), res["flags"]


def test_trigger_genuine_inconsistency_flags():
    # Buy trigger above the bull-case ceiling is a real inconsistency.
    text = (
        "Valuation.\n"
        "Bull: EPS ~$15, multiple ~30x -> implied price ~= $450, the top of range.\n"
        "\n## Entry triggers\n"
        "- Buy at $500 on any confirmation of the catalyst.\n"
    )
    res = checkers.check_trigger_consistency(text)
    assert not res["passed"], "buy at $500 vs bull-case high $450 must FLAG"
    assert any("$500.00" in f and "$450.00" in f for f in res["flags"]), res["flags"]


def test_trigger_entry_within_bull_range_passes():
    # An entry band below the bull ceiling is fine even if above the base case.
    text = (
        "Valuation.\n"
        "Base: implied price ~= $203.\n"
        "Bull: implied price ~= $390.\n"
        "\n## Entry triggers\n"
        "- Pullback toward $250 would offer a better entry than today.\n"
    )
    res = checkers.check_trigger_consistency(text)
    assert res["passed"], f"$250 is within the bull range ($390) — must PASS: {res['flags']}"


# ── base-band fallback true positive (no implied-price cue line) ────────────
# A brief whose scenarios carry NO implied-price cue: the ceiling must come from
# the base-case band ($205–$228), so a "Price ≤ $318.50" entry above it FLAGS.
OPTC_BRIEF = (
    "## OPTC — full underwrite\n"
    "**Valuation.**\n"
    "- **Bear** (2027E): **$115–$140** on multiple compression to 18x.\n"
    "- **Base** (2027E): **$205–$228** at a 26x through-cycle multiple.\n"
    "- **Bull** (2027E): **$300–$330** if design wins compound.\n"
    "\n## Entry triggers\n"
    "- Price ≤ $318.50 would offer an adequate margin of safety to initiate.\n"
)


def test_trigger_base_band_fallback_flags_entry_above_base():
    res = checkers.check_trigger_consistency(OPTC_BRIEF)
    assert not res["passed"], "OPTC ≤$318.50 vs base $228 must FLAG"
    assert any("318.50" in f and "228.00" in f for f in res["flags"]), res["flags"]


# A real-shaped NRDX brief that must produce ZERO flags, plus an injected buy
# above the bull-high that MUST flag (the ceiling is the $390 bull high, not the
# $1,840.0M backlog citation sitting on the base cue line).
NRDX_BRIEF = (
    "## NRDX — standard dive\n"
    "**Valuation (reverse-DCF framing).**\n"
    "- Bear: EPS flat ~$6.40, multiple ~14x. Price ≈ $90 (-64%).\n"
    "- Base: EPS grows to ~$8.10, multiple ~25x. Price ≈ $203, with backlog "
    "$1,840.0M intact (-19%).\n"
    "- Bull: EPS ~$9.30, multiple holds ~38x. Price ≈ $353–$390 (+55%).\n"
    "\n## Entry triggers if WATCH\n"
    "- A pullback toward the $250–$280 base-case band (from the 52-wk range "
    "$118.40–$402.75) offers a better margin of safety.\n"
    "- Extract and confirm Q1 FY2027 actuals (10-Q/8-K filed 2026-06-04) before "
    "re-pricing.\n"
)


def test_trigger_real_brief_zero_flags():
    res = checkers.check_trigger_consistency(NRDX_BRIEF)
    assert res["passed"], f"real NRDX brief must produce 0 flags: {res['flags']}"


def test_trigger_injected_buy_above_bull_flags():
    injected = NRDX_BRIEF.replace(
        "- Extract and confirm Q1 FY2027 actuals",
        "- Buy at $450 on any confirmation of the catalyst.\n"
        "- Extract and confirm Q1 FY2027 actuals", 1)
    assert injected != NRDX_BRIEF, "injection anchor not found — fixture is stale"
    res = checkers.check_trigger_consistency(injected)
    assert not res["passed"], "buy $450 above bull-high $390 must FLAG"
    assert any("450.00" in f and "390.00" in f for f in res["flags"]), res["flags"]


def test_trigger_north_of_flags_above_base_high():
    # "even north of $110" while the base case is $80–$96. The base scenario line
    # writes the EPS band ($5–6) BEFORE the price band, so this also pins that the
    # fallback reads the base HIGH as the max per-share price ($96), not the first
    # "$A–$B" range it sees (the EPS band).
    text = (
        "**Valuation.**\n"
        "- **Base:** the memory upcycle lifts through-cycle EPS ~$5–6, market pays "
        "~16× → ~$80–96.\n"
        "- **Bull:** ~$9–11 sustained, ~16× → ~$144–176.\n"
        "\n## Entry triggers\n"
        "- Buy on confirmation, even north of $110, if demand keeps growing.\n"
    )
    res = checkers.check_trigger_consistency(text)
    assert not res["passed"], "north of $110 vs base high $96 must FLAG"
    assert any("110.00" in f and "96.00" in f for f in res["flags"]), res["flags"]


def test_trigger_past_tense_narration_does_not_flag():
    # Narrative past-tense "the stock hit $640.00" in a pre-mortem is NOT an entry
    # trigger — only present-tense hits/reaches/crosses count.
    text = (
        "Valuation.\n"
        "- Base: EPS ~$8, ~25x → implied price ≈ $200.\n"
        "\n## Entry triggers\n"
        "- Pullback toward $180 offers a better entry.\n"
        "Pre-mortem: everything was priced before the stock hit $640.00 last year.\n"
    )
    res = checkers.check_trigger_consistency(text)
    assert res["passed"], f"past-tense 'hit $640.00' must not FLAG: {res['flags']}"


# ── temporal-claim date-order ──────────────────────────────────────────────
def test_temporal_immediately_following_far_dates_flags():
    # A drop on 09-08→09-11 claimed "immediately following" filings dated five
    # weeks earlier (08-01/08-05).
    text = (
        "Stock fell from $210.40 (2026-09-08) to $184.10 (2026-09-11), a ~12.5% "
        "four-session decline, immediately following the Q1 FY2027 10-Q/8-K (both "
        "filed 2026-08-01) and a further 8-K (filed 2026-08-05).")
    res = checkers.check_temporal_claims(text)
    assert not res["passed"], "5-week gap under 'immediately following' must FLAG"
    assert any("temporal claim inconsistent" in f for f in res["flags"]), res["flags"]


def test_temporal_genuine_day_after_passes():
    # A real 'the day after' with dates one day apart must not flag.
    text = ("The stock fell on 2026-07-09, the day after the 8-K "
            "(filed 2026-07-08) hit the wire.")
    res = checkers.check_temporal_claims(text)
    assert res["passed"], f"a true next-day reaction must PASS: {res['flags']}"


def test_temporal_cue_with_single_date_does_not_flag():
    # One date can't be an inconsistency — nothing to compare against.
    res = checkers.check_temporal_claims(
        "Shares dropped immediately following the release on 2026-07-09.")
    assert res["passed"], res["flags"]


def test_temporal_far_dates_without_cue_pass():
    # Distant dates are fine when no adjacency is asserted.
    res = checkers.check_temporal_claims(
        "The 8-K (filed 2026-06-04) and the July print (2026-07-13) both matter.")
    assert res["passed"], res["flags"]
