"""scout/test_brief_freshness.py — unit cases for the stale-brief attachment
decision logic (Task 1, 2026-07-13). Plain-Python asserts (no pytest, no LLM
spend, no network — html rendering is stubbed):

    scout/.venv/bin/python -m scout.test_brief_freshness

The NRDX incident: the relay silently attached briefs/NRDX_standard_2026-07-12
(yesterday's body) under a live 07-13 header, so the cards said "consensus PT
$540" while the body said "Consensus: NOT FOUND". These tests pin the fix:
  - assess_freshness flags a brief older than today as stale, today's as fresh;
  - latest_brief picks the newest by AS-OF DATE, so a newer quick take wins over
    an older standard brief (the lexical-sort bug that let 07-12 standard beat
    07-13 quick);
  - send_brief on a stale brief returns a cost-consent ask and attaches NOTHING;
  - with allow_stale=true it attaches, clearly labeled historical;
  - a same-day (fresh) brief attaches normally, no gate.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from scout import agent_tools, reports
from scout.agent_tools import ToolContext
from scout.db import Database


def _brief(dir_: Path, name: str) -> Path:
    p = dir_ / name
    p.write_text(f"# {name.split('_')[0]} — Brief\n\nBody.\n")
    return p


class _Ctx(ToolContext):
    def __init__(self):
        # JSON-fallback DB, no Postgres, no network.
        db = Database(db_url="")
        db.apply_schema()
        super().__init__(db)


def _with_temp_briefs(fn):
    """Run fn(tmpdir) with reports.BRIEFS_DIR pointed at a temp dir and
    html_for_brief stubbed (no Alpaca/visuals network)."""
    orig_dir = reports.BRIEFS_DIR
    orig_html = reports.html_for_brief
    with tempfile.TemporaryDirectory() as d:
        dd = Path(d)
        reports.BRIEFS_DIR = dd
        reports.html_for_brief = lambda p, db=None: Path(p).with_suffix(".html")
        try:
            return fn(dd)
        finally:
            reports.BRIEFS_DIR = orig_dir
            reports.html_for_brief = orig_html


# ── assess_freshness ────────────────────────────────────────────────────────
def test_assess_freshness_stale_when_older_than_today():
    r = reports.assess_freshness("NRDX_standard_2026-07-12.md", today="2026-07-13")
    assert r["as_of"] == "2026-07-12" and r["stale"] is True, r


def test_assess_freshness_fresh_when_same_day():
    r = reports.assess_freshness("NRDX_standard_2026-07-13.md", today="2026-07-13")
    assert r["stale"] is False, r


def test_assess_freshness_rerender_date_is_ignored():
    # A re-render suffix must NOT be read as the as-of date.
    r = reports.assess_freshness(
        "NRDX_standard_2026-07-12_rerendered-2026-07-13.md", today="2026-07-13")
    assert r["as_of"] == "2026-07-12" and r["stale"] is True, r


# ── latest_brief date ordering ──────────────────────────────────────────────
def test_latest_brief_picks_newest_by_date_not_lexical_tier():
    def body(dd):
        _brief(dd, "NRDX_standard_2026-07-12.md")
        newer = _brief(dd, "NRDX_quick_2026-07-13.md")
        got = reports.latest_brief("NRDX")
        # 'quick' sorts before 'standard' lexically; the newer DATE must win.
        assert got == newer, got
        # tier filter still finds the older standard brief on request
        assert reports.latest_brief("NRDX", tier="standard").name == \
            "NRDX_standard_2026-07-12.md"
    _with_temp_briefs(body)


def test_latest_brief_excludes_compare_files():
    def body(dd):
        _brief(dd, "NRDX_vs_VBRG_compare_2026-07-14.md")
        real = _brief(dd, "NRDX_standard_2026-07-12.md")
        assert reports.latest_brief("NRDX") == real
    _with_temp_briefs(body)


# ── send_brief gate ─────────────────────────────────────────────────────────
def test_send_brief_stale_asks_consent_and_attaches_nothing():
    def body(dd):
        _brief(dd, "NRDX_standard_2026-07-12.md")  # older than any real "today"
        ctx = _Ctx()
        msg = agent_tools.dispatch("send_brief", {"symbol": "NRDX"}, ctx)
        assert ctx.send_documents == [], "stale brief must NOT be attached"
        assert "won't silently send a stale brief" in msg, msg
        assert "2026-07-12" in msg and "Nothing spent" in msg, msg
        assert "allow_stale=true" in msg, msg
        ctx.db.close()
    _with_temp_briefs(body)


def test_send_brief_allow_stale_attaches_labeled_historical():
    def body(dd):
        b = _brief(dd, "NRDX_standard_2026-07-12.md")
        ctx = _Ctx()
        msg = agent_tools.dispatch(
            "send_brief", {"symbol": "NRDX", "allow_stale": True}, ctx)
        assert len(ctx.send_documents) == 1, ctx.send_documents
        assert ctx.send_documents[0] == str(b.with_suffix(".html"))
        assert "SAVED" in msg and "historical" in msg and "2026-07-12" in msg, msg
        ctx.db.close()
    _with_temp_briefs(body)


def test_send_brief_fresh_attaches_normally():
    from datetime import date
    def body(dd):
        today = date.today().isoformat()
        b = _brief(dd, f"NRDX_standard_{today}.md")
        ctx = _Ctx()
        msg = agent_tools.dispatch("send_brief", {"symbol": "NRDX"}, ctx)
        assert ctx.send_documents == [str(b.with_suffix(".html"))], ctx.send_documents
        assert "tap it to read" in msg, msg
        ctx.db.close()
    _with_temp_briefs(body)


def test_send_brief_no_brief_offers_underwrite():
    def body(dd):
        ctx = _Ctx()
        msg = agent_tools.dispatch("send_brief", {"symbol": "ZZZZ"}, ctx)
        assert "No saved brief" in msg and ctx.send_documents == []
        ctx.db.close()
    _with_temp_briefs(body)
