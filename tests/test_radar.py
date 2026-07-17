"""scout/test_radar.py — unit cases for the config-driven radar themes
(radar.config_themes / run_weekly) and the interactive run_radar agent tool.
Plain-Python asserts (no pytest, no LLM spend, no Telegram sends — the llm layer
and radar.run_weekly are monkeypatched):

    scout/.venv/bin/python -m scout.test_radar

Covers:
  - config_themes() reads radar.themes from config.yml (wired to the loaded
    config, founding theme always present) and defaults to ["AI infrastructure"]
    (behavior unchanged) when the key is missing/empty;
  - run_weekly with no explicit themes pulls from config, not a hardcoded list;
  - the run_radar tool runs SYNCHRONOUS (use_batch=False), passes themes through,
    delivers via the SAME shared helper (message text + HTML doc on ctx), and the
    monthly-cap guard still applies on the sync path.
"""

from __future__ import annotations

from scout import agent_tools, radar
from scout.agent_tools import ToolContext, dispatch


class _FakeDB:
    """Stand-in db — run_radar's radar.run_weekly is stubbed, so the tool never
    touches a real store. Only needs to be a distinct object we can identify."""
    def apply_schema(self):
        pass


# ── Task 1: config-driven themes ────────────────────────────────────────────
def test_config_themes_reads_config():
    # Don't pin the owner's exact theme list (it's expected to change over
    # time) — instead prove the wiring: config_themes() returns whatever
    # radar.themes holds in the loaded config, as a non-empty list of
    # non-empty strings, and the founding theme never silently disappears.
    themes = radar.config_themes()
    assert themes, themes
    assert all(isinstance(t, str) and t for t in themes), themes
    assert themes == radar.load_config()["radar"]["themes"], themes
    assert "AI infrastructure" in themes, themes


def test_config_themes_falls_back_when_key_missing(monkeypatch=None):
    # Missing radar section → in-code fallback (radar always has >=1 theme).
    orig = radar.load_config
    radar.load_config = lambda: {"models": {}}          # no radar key
    try:
        assert radar.config_themes() == radar.DEFAULT_THEMES
    finally:
        radar.load_config = orig


def test_config_themes_reads_edited_list():
    # Changing the list is a config-only edit — code reads whatever is there.
    orig = radar.load_config
    radar.load_config = lambda: {"radar": {"themes": ["power", "optics"]}}
    try:
        assert radar.config_themes() == ["power", "optics"]
    finally:
        radar.load_config = orig


def test_run_weekly_uses_config_when_no_themes_passed():
    # run_weekly(themes=None) must source themes from config_themes(), not a
    # hardcoded constant. We stub the expensive internals and capture the themes
    # actually walked.
    seen = {}
    orig_walk = radar.walk_themes
    orig_cfg = radar.config_themes
    radar.config_themes = lambda: ["grid storage"]

    def _fake_walk(db, themes, use_batch=True):
        seen["themes"] = themes
        seen["use_batch"] = use_batch
        return []

    radar.walk_themes = _fake_walk

    class _DB:
        def apply_schema(self): pass
        def select(self, *a, **k): return []
        def count(self, *a, **k): return 0

    # month_spend is called for the cost snapshot — stub the llm module surface.
    orig_month = radar.llm.month_spend
    radar.llm.month_spend = lambda db: 0.0
    # Redirect the memo write to a temp path so we don't litter briefs/.
    import tempfile
    from pathlib import Path
    orig_root = radar.REPO_ROOT
    tmp = Path(tempfile.mkdtemp())
    (tmp / "briefs").mkdir()
    radar.REPO_ROOT = tmp
    try:
        out = radar.run_weekly(db=_DB(), quick_takes=False)
        assert seen["themes"] == ["grid storage"], seen
        assert out["new_candidates"] == 0
    finally:
        radar.config_themes = orig_cfg
        radar.walk_themes = orig_walk
        radar.llm.month_spend = orig_month
        radar.REPO_ROOT = orig_root


# ── Bug fix: confirmation-queue rows must never truncate the ticker list ──
# Owner-reported 7/14: radar.py's queue-row rendering used to slice the WHOLE
# stored description (prose + " | tickers: ..." suffix) to 160 chars, so any
# row whose prose alone was long enough silently ate part or all of the
# ticker list. Description is the only place tickers live — there is no
# separate tickers column on the constraints table (see schema.sql) — so the
# fix must split prose from the ticker suffix before slicing, and never
# slice the suffix itself.
def test_confirmation_queue_row_never_truncates_tickers():
    long_prose = "This constraint concerns a very long dependency chain " * 4
    assert len(long_prose) > 200, len(long_prose)
    tickers = "AAAA,BBBB,CCCC,DDDD"
    description = long_prose + " | tickers: " + tickers

    class _DB:
        def apply_schema(self): pass
        def select(self, table, order_by=None):
            if table == "constraints":
                return [{"id": 7, "theme": "Grid storage", "description": description,
                         "tier": 2, "status": "candidate",
                         "confirmed_by_owner": False}]
            return []
        def count(self, *a, **k): return 0

    orig_walk = radar.walk_themes
    orig_cfg = radar.config_themes
    orig_month = radar.llm.month_spend
    radar.walk_themes = lambda db, themes, use_batch=True: []
    radar.config_themes = lambda: ["Grid storage"]
    radar.llm.month_spend = lambda db: 0.0

    import tempfile
    from pathlib import Path
    orig_root = radar.REPO_ROOT
    tmp = Path(tempfile.mkdtemp())
    (tmp / "briefs").mkdir()
    radar.REPO_ROOT = tmp
    try:
        out = radar.run_weekly(db=_DB(), quick_takes=False)
    finally:
        radar.walk_themes = orig_walk
        radar.config_themes = orig_cfg
        radar.llm.month_spend = orig_month
        radar.REPO_ROOT = orig_root

    memo = out["memo"]
    section = memo.split("## Confirmation queue")[1].split("## Quick takes")[0]
    # the full ticker list must survive intact
    assert f"| tickers: {tickers}" in section, section
    # the prose part must still be capped at 160 chars
    row_line = next(l for l in section.splitlines() if l.startswith("- [7]"))
    prose_part = row_line.split(": ", 1)[1].split(" | tickers:")[0]
    assert len(prose_part) == 160, (len(prose_part), prose_part)


# ── Task 2: the run_radar interactive tool ──────────────────────────────────
def _install_fake_run_weekly(record):
    """Replace radar.run_weekly with a stub that records its kwargs and returns a
    fixed out dict; also stub reports.html_for_brief so no real HTML is built."""
    def _fake_run_weekly(db=None, themes=None, use_batch=True, quick_takes=True):
        record["db"] = db
        record["themes"] = themes
        record["use_batch"] = use_batch
        record["quick_takes"] = quick_takes
        return {"memo": "# memo", "path": "/tmp/radar_test.md",
                "new_candidates": 2, "queue": 5}
    radar.run_weekly = _fake_run_weekly

    from scout import reports
    reports.html_for_brief = lambda p, db=None: __import__("pathlib").Path(
        "/tmp/radar_test.html")


def _run_tool(ti):
    """Dispatch run_radar with radar.run_weekly + reports stubbed. Returns
    (result_string, ctx, record)."""
    import importlib
    from scout import reports
    record = {}
    orig_run = radar.run_weekly
    orig_html = reports.html_for_brief
    _install_fake_run_weekly(record)
    ctx = ToolContext(_FakeDB())
    try:
        result = dispatch("run_radar", ti, ctx)
    finally:
        radar.run_weekly = orig_run
        reports.html_for_brief = orig_html
    return result, ctx, record


def test_run_radar_registered_in_schema():
    names = {t["name"] for t in agent_tools.TOOL_SCHEMAS}
    assert "run_radar" in names, names
    schema = next(t for t in agent_tools.TOOL_SCHEMAS if t["name"] == "run_radar")
    # description must tell the model what it is, the cost band, and config default
    desc = schema["description"].lower()
    assert "idea" in desc or "new-idea" in desc or "candidate" in desc, desc
    assert "0.05" in desc and "$1" in desc, desc
    assert "config" in desc, desc


def test_run_radar_is_synchronous_and_delivers():
    result, ctx, record = _run_tool({})
    # SYNC per the interactive policy (batch is only for scheduled work).
    assert record["use_batch"] is False, record
    # default themes → None (run_weekly then pulls config)
    assert record["themes"] is None, record
    # delivered via the shared transport: an HTML doc queued on the context
    assert ctx.send_documents == ["/tmp/radar_test.html"], ctx.send_documents
    # message text summarizes the run
    assert "2" in result and "5" in result, result


def test_run_radar_passes_explicit_themes():
    result, ctx, record = _run_tool({"themes": ["power", "optics"]})
    assert record["themes"] == ["power", "optics"], record
    assert record["use_batch"] is False, record


def test_run_radar_budget_guard_surfaces():
    # If the sync radar hits the monthly cap, run_weekly raises BudgetExceeded;
    # the tool must surface it, not swallow it into a fake success.
    from scout import reports
    orig_run = radar.run_weekly
    orig_html = reports.html_for_brief
    reports.html_for_brief = lambda p, db=None: "/tmp/x.html"

    def _boom(**kwargs):
        from scout.llm import BudgetExceeded
        raise BudgetExceeded("cap reached")

    radar.run_weekly = _boom
    ctx = ToolContext(_FakeDB())
    try:
        result = dispatch("run_radar", {}, ctx)
    finally:
        radar.run_weekly = orig_run
        reports.html_for_brief = orig_html
    assert "cap reached" in result or "udget" in result, result
    assert ctx.send_documents == [], ctx.send_documents


def test_shared_delivery_helper_used_by_both_paths():
    # radar.prepare_delivery is the single source of the message text + html path
    # both the scheduled Monday job and the interactive tool render from.
    assert hasattr(radar, "prepare_delivery"), "shared helper missing"
    record = {}
    from scout import reports
    orig_run = radar.run_weekly
    orig_html = reports.html_for_brief
    _install_fake_run_weekly(record)
    try:
        d = radar.prepare_delivery(_FakeDB(), themes=["x"], use_batch=False)
    finally:
        radar.run_weekly = orig_run
        reports.html_for_brief = orig_html
    assert record["use_batch"] is False and record["themes"] == ["x"], record
    assert "message" in d and "html_path" in d, d
    assert d["out"]["new_candidates"] == 2, d


# ── Task 3: the theme walk is date-aware + forward-framed ────────────────────
# Owner requirement 2026-07-14: narrowing must reason from the RUN DATE forward,
# not from training-era memories of what was once tight. The forward-framing must
# be injected WITHOUT changing the strict-JSON output contract enqueue_candidates
# parses (theme/description/tier/tickers).
def test_walk_prompt_is_date_aware_and_forward_framed():
    req = radar._walk_request("power", today="2026-07-14")
    sys = req["system"]
    assert "2026-07-14" in sys, sys
    low = sys.lower()
    assert "forward" in low, sys                 # reason forward from today
    assert "1–3 years" in sys or "1-3 years" in sys, sys
    assert "breaks next" in low, sys             # what breaks next as it scales
    assert "possibly-outdated" in low or "outdated" in low, sys


def test_walk_prompt_defaults_to_today():
    from datetime import date
    sys = radar._walk_request("optics")["system"]
    assert date.today().isoformat() in sys, sys


def test_walk_output_contract_unchanged():
    # The strict-JSON contract block must survive byte-for-byte, and the walk
    # parser must still read a sample reply — downstream parsing must not break.
    sys = radar.walk_system("2026-07-14")
    contract = ('Output STRICT JSON only, no prose outside it:\n'
                '{"constraints": [{"theme": ..., "description": "one sentence",\n'
                '  "tier": 1, "why_early_candidate": "one sentence",\n'
                '  "tickers": ["ABC", "DEF"]}, ...]}\n'
                'Max 6 constraints, max 4 tickers each.')
    assert contract in sys, sys
    parsed = radar._parse_walk(
        '{"constraints": [{"theme": "power", "description": "grid interconnect",'
        ' "tier": 1, "why_early_candidate": "lead times", "tickers": ["GRDX"]}]}')
    assert parsed and parsed[0]["tickers"] == ["GRDX"], parsed


# ── 2026-07-15 stale-ticker fix (T4 radar half): drop inactive at the seam ────
# The Opus walk proposes tickers from training-era memory, so it occasionally
# emits symbols that no longer trade (ARJT, acquired 2023). enqueue_candidates —
# the clean seam where the walk's ticker lists are parsed before storage — must
# drop Alpaca-inactive tickers, fail-OPEN (only an explicit active==False drops;
# a resolve error keeps the ticker), and never touch the constraint text.
def test_enqueue_drops_inactive_tickers_fail_open():
    from scout import market_ref
    orig = market_ref.resolve_ticker

    def fake(sym):
        if sym == "ARJT":
            return {"resolved": True, "active": False, "status": "inactive"}
        if sym == "BOOM":
            raise RuntimeError("resolve down")      # error → fail-open, KEEP
        return {"resolved": True, "active": True, "status": "active"}

    market_ref.resolve_ticker = fake

    class _DB:
        def __init__(self):
            self.inserted = []

        def select(self, table, order_by=None):
            return []

        def insert(self, table, row):
            self.inserted.append(row)
            return len(self.inserted)

    db = _DB()
    try:
        added = radar.enqueue_candidates(db, [
            {"theme": "Space/defense", "description": "reusable launch cadence",
             "tier": 1, "tickers": ["LNCH", "ARJT", "BOOM"]}])
    finally:
        market_ref.resolve_ticker = orig
    stored = db.inserted[0]["description"]
    assert "ARJT" not in stored, stored                 # inactive dropped
    assert "tickers: LNCH,BOOM" in stored, stored       # active + fail-open kept
    assert added[0]["tickers"] == ["LNCH", "BOOM"], added


def test_enqueue_helper_only_drops_explicit_false():
    # Unit-level: unknown/None active never drops (fail-open); only False drops.
    from scout import market_ref
    orig = market_ref.resolve_ticker
    market_ref.resolve_ticker = lambda s: (
        {"active": False} if s == "DEAD" else {"active": None})
    try:
        assert radar._drop_inactive_tickers(["A", "DEAD", "B"]) == ["A", "B"]
    finally:
        market_ref.resolve_ticker = orig
