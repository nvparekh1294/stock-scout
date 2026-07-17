"""scout/test_research.py — unit cases for the standard/quick retry logic in
research.underwrite. Plain-Python asserts (no pytest, no LLM spend — llm.call is
monkeypatched):

    scout/.venv/bin/python -m scout.test_research

Covers the 2026-07-13 audit fix: a brief that stops naturally (`end_turn`) but
omits a required section (the NRDX Pre-mortem case) triggers ONE completeness
retry at 1.5x budget with a strengthened instruction. Confirms:
  - a first end_turn brief missing only the Pre-mortem → retry fires ONCE → a
    complete second brief is delivered clean (checker passes, exactly 2 calls);
  - when the retry is STILL incomplete → delivered with format flags, no third
    call (the honest fallback), and the single-retry cap holds.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from scout import research
from scout.db import Database

# A minimal standard brief that PASSES every deterministic checker. It carries
# dated source markers and no $/%/× numeric claims, so citation/date/arithmetic
# all pass; the only variable under test is section completeness.
_COMPLETE = (
    "**Thesis in three sentences.** The edge is cited and dated (10-K filed "
    "2026-03-01). The pack supports a durable Stage 2 position. It is early on a "
    "recognized-but-underweighted shift.\n"
    "**What is still unpriced (cited).** The margin trajectory in the 8-K filed "
    "2026-03-26 is not reflected in the stale consensus on file.\n"
    "**Variant view vs. consensus.** NOT FOUND — no post-print consensus in the "
    "pack.\n"
    "**4-5 most decision-relevant facts (dated, cited).** Backlog was reported "
    "(8-K filed 2026-03-26). Guidance was reiterated (10-K filed 2026-03-01).\n"
    "**Valuation (reverse-DCF framing).** Framework only; the inputs are NOT "
    "FOUND in the pack.\n"
    "**Break conditions (falsifiable, dated).** Backlog declines from the level "
    "reported in the 8-K filed 2026-03-26.\n"
    "**Entry triggers if WATCH.** Confirm the next quarterly print when it is "
    "filed.\n"
    "**Pre-mortem.** The thesis is wrong if demand proves cyclical rather than "
    "structural (10-K filed 2026-03-01 lists this risk).\n"
)
# Same brief, but the model stopped before writing the Pre-mortem section.
_MISSING_PREMORTEM = _COMPLETE.rsplit("**Pre-mortem.**", 1)[0].rstrip() + "\n"


def _install_mock_llm(responses: list[dict]) -> dict:
    """Replace research.llm.call with a scripted stub. Returns a record dict with
    the list of (task, max_tokens, user_content) tuples actually called."""
    record = {"calls": [], "queue": list(responses)}

    def _fake_call(task, model_tier, messages, max_tokens, **kwargs):
        record["calls"].append({
            "task": task, "max_tokens": max_tokens,
            "user": messages[0]["content"],
            "system": kwargs.get("system")})
        resp = record["queue"].pop(0)
        return {"text": resp["text"], "usd": resp.get("usd", 0.01),
                "stop_reason": resp.get("stop_reason", "end_turn"),
                "model": "test", "usage": {}, "raw_content": [], "tool_uses": []}

    research.llm.call = _fake_call
    return record


def _run_underwrite(responses: list[dict]):
    """Run a standard underwrite with a temp pack + JSON-fallback DB and the
    given scripted LLM responses. write_brief is stubbed to avoid touching the
    real briefs/ directory. Returns (result, record)."""
    record = _install_mock_llm(responses)
    orig_write = research.reports.write_brief
    research.reports.write_brief = lambda symbol, depth, content, on_date: Path(
        "/tmp/_test_brief.md")
    try:
        with tempfile.TemporaryDirectory() as d:
            pack = Path(d) / "TST_2026-07-13.md"
            pack.write_text("## EVIDENCE PACK\n- Fact (10-K filed 2026-03-01) "
                            "https://example.com/tst-10k\n")
            db = Database(db_url="")  # JSON fallback, no Postgres
            db.apply_schema()
            result = research.underwrite(
                "TST", depth="standard", pack_path=str(pack),
                as_of="2026-07-13", db=db, monthly_budget=1000.0)
            db.close()
    finally:
        research.reports.write_brief = orig_write
    return result, record


def test_completeness_retry_fires_and_delivers_clean():
    # First brief ends via end_turn missing only the Pre-mortem → completeness
    # retry fires ONCE → second brief complete → delivered clean.
    result, record = _run_underwrite([
        {"text": _MISSING_PREMORTEM, "stop_reason": "end_turn", "usd": 0.02},
        {"text": _COMPLETE, "stop_reason": "end_turn", "usd": 0.03},
    ])
    assert len(record["calls"]) == 2, \
        f"expected exactly 2 calls (1 initial + 1 retry), got {len(record['calls'])}"
    # the retry is the completeness retry, carrying the strengthened instruction
    assert "-retry" in record["calls"][1]["task"], record["calls"][1]["task"]
    assert "omitted required sections" in record["calls"][1]["user"], \
        "retry prompt must carry the strengthened completeness instruction"
    assert "Pre-mortem" in record["calls"][1]["user"], \
        "retry prompt must name the missing section"
    # retry budget is 1.5x the initial budget
    assert record["calls"][1]["max_tokens"] == int(record["calls"][0]["max_tokens"] * 1.5)
    assert result["checker_passed"], "complete second brief must be delivered clean"
    # both call costs summed
    assert abs(result["cost_usd"] - 0.05) < 1e-9, result["cost_usd"]


def test_completeness_retry_still_incomplete_delivers_with_flags():
    # Both attempts end via end_turn missing the Pre-mortem → deliver with flags,
    # NO third call (single-retry cap holds, honest fallback).
    result, record = _run_underwrite([
        {"text": _MISSING_PREMORTEM, "stop_reason": "end_turn", "usd": 0.02},
        {"text": _MISSING_PREMORTEM, "stop_reason": "end_turn", "usd": 0.02},
    ])
    assert len(record["calls"]) == 2, \
        f"cap is ONE retry — expected 2 calls total, got {len(record['calls'])}"
    assert not result["checker_passed"], \
        "a still-incomplete brief must be delivered with checker FLAGS"
    assert abs(result["cost_usd"] - 0.04) < 1e-9, result["cost_usd"]


def test_complete_first_pass_no_retry():
    # A complete first brief must NOT trigger any retry.
    result, record = _run_underwrite([
        {"text": _COMPLETE, "stop_reason": "end_turn", "usd": 0.02},
    ])
    assert len(record["calls"]) == 1, \
        f"a complete first brief needs no retry, got {len(record['calls'])} calls"
    assert result["checker_passed"]


# ── Fix 1: empty evidence-pack section is now an explicit pointer ────────────
import re as _re


def _section_after_heading(user: str, heading: str) -> str:
    """Return the body that follows `heading` in `user`, up to the next H2/rule
    or end-of-string. Used to prove a labeled section is not left empty."""
    i = user.index(heading) + len(heading)
    rest = user[i:]
    m = _re.search(r"\n(?:## |---)", rest)
    return rest[:m.start()] if m else rest


def test_pointer_fills_every_user_turn_no_empty_section():
    # For each of the 4 templates in play, the USER turn must carry the pointer
    # text and its evidence-pack heading must NOT be immediately followed by only
    # whitespace/end-of-string (the ambiguity this fix removes).
    scalars = {"SYMBOL": "TST", "AS_OF_DATE": "2026-07-13"}
    pointer = research._EVIDENCE_POINTER
    cases = [
        ("quick_take", "## EVIDENCE PACK", {"EVIDENCE_PACK": pointer}),
        ("standard_dive", "## EVIDENCE PACK", {"EVIDENCE_PACK": pointer}),
        ("underwriter", "## NOW UNDERWRITE THIS PACK", {"EVIDENCE_PACK": pointer}),
        ("adversary", "## EVIDENCE PACK",
         {"EVIDENCE_PACK": pointer, "UNDERWRITE_BRIEF": "prior brief text"}),
    ]
    for name, heading, volatiles in cases:
        _system, user = research.build_prompt(name, scalars, volatiles)
        assert pointer in user, f"{name}: pointer text missing from user turn"
        body = _section_after_heading(user, heading)
        assert body.strip() != "", \
            f"{name}: '{heading}' is followed by only whitespace (empty section)"
        assert pointer in body, \
            f"{name}: pointer must sit directly under the '{heading}' heading"
        # Prove the OLD behavior produced exactly the empty section we fixed.
        empty_vol = dict(volatiles, EVIDENCE_PACK="")
        _s2, user_empty = research.build_prompt(name, scalars, empty_vol)
        assert _section_after_heading(user_empty, heading).strip() == "", \
            f"{name}: control case should show the empty-section regression"


def test_system_bytes_unchanged_by_pointer():
    # The returned `system` must be byte-identical whether EVIDENCE_PACK is ""
    # or the pointer — build_prompt computes/returns `system` from text[:idx]
    # BEFORE any volatile substitution, so the pack value cannot leak into it.
    scalars = {"SYMBOL": "TST", "AS_OF_DATE": "2026-07-13"}
    for name in ("quick_take", "standard_dive", "underwriter"):
        sys_empty, _ = research.build_prompt(name, scalars, {"EVIDENCE_PACK": ""})
        sys_ptr, _ = research.build_prompt(
            name, scalars, {"EVIDENCE_PACK": research._EVIDENCE_POINTER})
        assert sys_empty == sys_ptr, \
            f"{name}: system bytes differ between empty and pointer volatiles"


def test_retry_reuses_byte_identical_cached_system():
    # The cached system prefix must be the SAME object (hence byte-identical) on
    # the initial call and the completeness retry — proving the retry is a cache
    # READ of the prefix, not a fresh write.
    result, record = _run_underwrite([
        {"text": _MISSING_PREMORTEM, "stop_reason": "end_turn", "usd": 0.02},
        {"text": _COMPLETE, "stop_reason": "end_turn", "usd": 0.03},
    ])
    assert len(record["calls"]) == 2, len(record["calls"])
    init_sys = record["calls"][0]["system"]
    retry_sys = record["calls"][1]["system"]
    assert init_sys is retry_sys, \
        "retry must reuse the SAME sys_blocks object built before the first call"
    assert init_sys == retry_sys  # byte-identical content, belt and suspenders


# ── Fix 2: no cache-write premium on the reunderwrite_batch fan-out ──────────
def test_reunderwrite_batch_omits_cache_control():
    # The monthly fan-out must build system blocks WITHOUT any cache_control key
    # (per-symbol instructions never share a prefix; nothing re-reads them).
    captured = {}

    def _fake_batch(requests, db=None, monthly_budget=None):
        captured["requests"] = requests
        return [{"text": _COMPLETE, "stop_reason": "end_turn", "usd": 0.01,
                 "via": "batch"} for _ in requests]

    orig_batch = research.llm.call_batch
    orig_src = research._compare_source
    orig_write = research.reports.write_brief
    research.llm.call_batch = _fake_batch
    research._compare_source = lambda sym, db: (
        f"## EVIDENCE PACK\n- Fact (10-K filed 2026-03-01) "
        f"https://example.com/{sym.lower()}-10k\n", f"pack {sym}")
    research.reports.write_brief = lambda symbol, depth, content, on_date: Path(
        "/tmp/_test_brief.md")
    try:
        db = Database(db_url="")
        db.apply_schema()
        out = research.reunderwrite_batch(
            ["AAA", "BBB"], db=db, as_of="2026-07-13",
            monthly_budget=1000.0, use_batch=True)
        db.close()
    finally:
        research.llm.call_batch = orig_batch
        research._compare_source = orig_src
        research.reports.write_brief = orig_write

    assert len(out) == 2, out
    reqs = captured["requests"]
    assert len(reqs) == 2, reqs
    for req in reqs:
        blocks = req["system"]
        assert isinstance(blocks, list) and len(blocks) == 2, blocks
        for b in blocks:
            assert "cache_control" not in b, \
                f"reunderwrite_batch block must omit cache_control, got {b}"


def test_interactive_dive_still_caches_both_blocks():
    # Regression pin: the interactive standard dive path is UNAFFECTED — both
    # system blocks must still carry cache_control.
    result, record = _run_underwrite([
        {"text": _COMPLETE, "stop_reason": "end_turn", "usd": 0.02},
    ])
    assert len(record["calls"]) == 1, len(record["calls"])
    blocks = record["calls"][0]["system"]
    assert isinstance(blocks, list) and len(blocks) == 2, blocks
    for b in blocks:
        assert b.get("cache_control") == {"type": "ephemeral"}, \
            f"interactive dive block must keep cache_control, got {b}"
