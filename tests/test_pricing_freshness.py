"""tests/test_pricing_freshness.py — pricing-table staleness warning and
token-first cost reporting. No LLM spend, no network — dates are passed/mocked
and the JSON store is isolated to a tempdir.

Covers:
  - the staleness warning fires when `costs.pricing_as_of` is >4 months older
    than today, stays silent when fresh (boundary: exactly 4 months is fresh);
  - unset as_of → a plain "set the date" nudge; unparsable as_of → silent
    (a bad value must never crash boot);
  - the warning is message-only (no network fetch anywhere in the path);
  - boot: online_announce() appends the warning when stale, not when fresh;
  - cost_report: appends the warning when stale, and every line is TOKEN-FIRST
    ("N in / M out ≈ $X at your configured rates");
  - the shipped config.example.yml carries a pricing_as_of date.
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from scout import agent_tools
from scout import config as cfg
from scout import db as dbmod
from scout.config import pricing_staleness_warning
from scout.db import Database


def _with_as_of(monkeypatch, as_of):
    monkeypatch.setattr(cfg, "load_config",
                        lambda: {"costs": {"pricing_as_of": as_of}})


# ── the staleness rule ───────────────────────────────────────────────────────
def test_fresh_table_no_warning(monkeypatch):
    _with_as_of(monkeypatch, "2026-07")
    assert pricing_staleness_warning(today=date(2026, 7, 16)) == ""


def test_exactly_four_months_is_still_fresh(monkeypatch):
    _with_as_of(monkeypatch, "2026-03")
    assert pricing_staleness_warning(today=date(2026, 7, 16)) == ""


def test_five_months_old_warns(monkeypatch):
    _with_as_of(monkeypatch, "2026-02")
    w = pricing_staleness_warning(today=date(2026, 7, 16))
    assert "5 months old" in w, w
    assert "2026-02" in w and "pricing page" in w, w
    assert "Nothing is fetched automatically" in w, w


def test_year_boundary_math(monkeypatch):
    _with_as_of(monkeypatch, "2025-11")
    w = pricing_staleness_warning(today=date(2026, 7, 16))
    assert "8 months old" in w, w


def test_unset_as_of_nudges(monkeypatch):
    _with_as_of(monkeypatch, "")
    w = pricing_staleness_warning(today=date(2026, 7, 16))
    assert "pricing_as_of" in w, w


def test_unparsable_as_of_is_silent_never_crashes(monkeypatch):
    for bad in ("garbage", "07/2026", "2026", "20xx-07"):
        _with_as_of(monkeypatch, bad)
        assert pricing_staleness_warning(today=date(2026, 7, 16)) == "", bad


def test_shipped_example_config_has_as_of():
    import yaml
    from scout.config import EXAMPLE_CONFIG_PATH
    conf = yaml.safe_load(EXAMPLE_CONFIG_PATH.read_text())
    as_of = str(conf["costs"]["pricing_as_of"])
    assert len(as_of.split("-")) == 2 and as_of[:2] == "20", as_of


def test_warning_path_never_fetches(monkeypatch):
    """Message only, by construction: any network attempt during the staleness
    check fails the test."""
    import socket

    def boom(*a, **k):
        raise AssertionError("network call during staleness check")

    monkeypatch.setattr(socket.socket, "connect", boom)
    _with_as_of(monkeypatch, "2025-01")
    assert "months old" in pricing_staleness_warning(today=date(2026, 7, 16))


# ── boot announcement carries the warning ────────────────────────────────────
def test_online_announce_appends_warning_when_stale(monkeypatch):
    from scout import telegram_bot
    monkeypatch.setattr(cfg, "pricing_staleness_warning",
                        lambda today=None, months_threshold=4: "⚠️ STALE-TEST")
    msg = telegram_bot.online_announce()
    assert msg.endswith("⚠️ STALE-TEST"), msg
    assert telegram_bot.ADVICE_DISCLAIMER in msg    # advice-disclaimer line still present


def test_online_announce_plain_when_fresh(monkeypatch):
    from scout import telegram_bot
    monkeypatch.setattr(cfg, "pricing_staleness_warning",
                        lambda today=None, months_threshold=4: "")
    msg = telegram_bot.online_announce()
    assert "⚠️" not in msg, msg
    assert telegram_bot.ADVICE_DISCLAIMER in msg


# ── cost report: token-first + staleness footer ──────────────────────────────
def _one_row_db(tmp):
    dbmod.LOCALDB_DIR = Path(tmp)
    d = Database(db_url="")
    d.apply_schema()
    d.insert("api_costs", {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": "claude-sonnet-5", "task": "QMEM-underwrite",
        "input_tokens": 12_000, "output_tokens": 3_000,
        "cached_tokens": 0, "usd_estimate": 0.08})
    return d


def test_cost_report_is_token_first(monkeypatch):
    monkeypatch.setattr(agent_tools.config, "pricing_staleness_warning",
                        lambda: "")
    orig = dbmod.LOCALDB_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            d = _one_row_db(tmp)
            out = agent_tools.cost_report(d, days=30)
            # tokens lead, dollars follow, and the dollars are labeled estimates
            assert "12k in / 3k out ≈ $0.08 at your configured rates" in out, out
            token_pos = out.find("12k in")
            dollar_pos = out.find("$0.08")
            assert -1 < token_pos < dollar_pos, out
            # per-operation and per-model lines are token-first too
            assert "underwrite: 12k in / 3k out ≈ $0.08 (1)" in out, out
            assert "claude-sonnet-5: 12k in / 3k out ≈ $0.08 (1)" in out, out
            d.close()
    finally:
        dbmod.LOCALDB_DIR = orig


def test_cost_report_appends_staleness_warning(monkeypatch):
    monkeypatch.setattr(agent_tools.config, "pricing_staleness_warning",
                        lambda: "⚠️ STALE-TEST")
    orig = dbmod.LOCALDB_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            d = _one_row_db(tmp)
            out = agent_tools.cost_report(d, days=30)
            assert out.endswith("⚠️ STALE-TEST"), out
            d.close()
    finally:
        dbmod.LOCALDB_DIR = orig


def test_cost_report_no_warning_when_fresh(monkeypatch):
    monkeypatch.setattr(agent_tools.config, "pricing_staleness_warning",
                        lambda: "")
    orig = dbmod.LOCALDB_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            d = _one_row_db(tmp)
            out = agent_tools.cost_report(d, days=30)
            assert "STALE" not in out and "months old" not in out, out
            d.close()
    finally:
        dbmod.LOCALDB_DIR = orig
