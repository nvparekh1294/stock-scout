"""scout/test_triage.py — the triage_radar tool + triage.run_triage (Task 2).

Plain-Python asserts. The llm layer and all data helpers are monkeypatched, so
NO model spend, NO network, NO Telegram sends:

    scout/.venv/bin/python -m scout.test_triage

Covers:
  - triage_radar registered in TOOL_SCHEMAS and dispatch runs end-to-end with the
    llm + data layers mocked;
  - a per-ticker snapshot failure degrades to "n/a" and never aborts the triage;
  - the single synthesis call goes through llm.call (cost logged), on the cheap
    quick/sonnet tier, sync;
  - delivery is queued on ctx.send_documents exactly like run_radar's test;
  - the snapshot table handed to the model (and saved in the memo) carries the
    RUN DATE;
  - an empty queue triages to a no-spend memo (no llm.call at all).
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

from scout import agent_tools, triage
from scout.agent_tools import ToolContext, dispatch


class _DB:
    """Stand-in db holding a constraints table; supports select(order_by)."""
    def __init__(self, constraints):
        self._c = constraints

    def apply_schema(self):
        pass

    def select(self, table, where=None, order_by=None):
        rows = list(self._c) if table == "constraints" else []
        if where:
            rows = [r for r in rows
                    if all(str(r.get(k)) == str(v) for k, v in where.items())]
        if order_by:
            rows = sorted(rows, key=lambda r: (r.get(order_by) is None,
                                               r.get(order_by)))
        return rows


def _constraint(cid, theme, tickers, tier=1):
    return {"id": cid, "theme": theme, "tier": tier, "status": "candidate",
            "confirmed_by_owner": False,
            "description": f"{theme} bottleneck sentence | tickers: {tickers}"}


# A default synthesis body >=200 chars so the normal path does NOT trip the
# empty/short-retry guard (_synthesis_incomplete retries anything under 200 chars).
_GOOD_MEMO = (
    "# Radar triage — synthesized\n"
    "## Distinct stories (ranked)\n"
    "### 1. AI infrastructure compute bottleneck\n"
    "The binding constraint is advanced-packaging and HBM supply. Vaxel Compute (VXEL) "
    "captures it most directly; forward read: capacity stays tight as clusters "
    "keep scaling (hypothesis).\n"
    "## Suggested next quick takes\n"
    "- VXEL — most direct capture of the constraint.\n")


def _patch(record, *, price=100.0, shares=1_000_000, filing=("10-Q", "2026-06-30"),
           fail_tickers=(), llm_seq=None, company=None, stub_html=True,
           resolve_overrides=None):
    """Monkeypatch llm.call + every data helper triage touches. Records EVERY
    synthesis call (record["calls"]) and the last one's fields. Returns a
    restore() thunk. `fail_tickers` raise in every helper to exercise the
    per-ticker fail-open path. `llm_seq` is an optional list of result dicts
    returned in order (later calls fall back to _GOOD_MEMO) — used to drive the
    truncation-retry / empty-after-retry paths. `company` overrides the resolved
    name. `stub_html` swaps reports.html_for_brief for a stub; pass False to
    exercise the real markdown→HTML converter. `resolve_overrides` maps a symbol
    to a dict merged into that ticker's resolve_ticker result — used to drive the
    identity-flag shapes (inactive ARJT / reassigned PHTN) through the snapshot."""
    from scout import fundamentals, gather, market_ref, reports

    orig = {
        "llm": triage.llm.call,
        "resolve": market_ref.resolve_ticker,
        "price": fundamentals.price_latest,
        "facts": fundamentals.company_facts_metrics,
        "lq": gather._latest_quarterly,
        "html": reports.html_for_brief,
        "root": triage.REPO_ROOT,
    }
    seq = list(llm_seq or [])
    calls = record.setdefault("calls", [])

    def fake_llm(task, tier, messages, max_tokens, system=None, db=None, **kw):
        record["task"] = task
        record["tier"] = tier
        record["system"] = system
        record["user"] = messages[0]["content"]
        record["max_tokens"] = max_tokens
        calls.append({"task": task, "tier": tier, "max_tokens": max_tokens})
        idx = len(calls) - 1
        if idx < len(seq):
            return seq[idx]
        return {"text": _GOOD_MEMO, "usage": {}, "usd": 0.12,
                "model": "claude-sonnet-5", "stop_reason": "end_turn"}

    overrides = resolve_overrides or {}

    def fake_resolve(sym):
        if sym in fail_tickers:
            raise RuntimeError("resolve boom")
        base = {"resolved": True, "cik": 111, "name": company or sym,
                "sec_filer": True, "active": True, "status": "active",
                "identity_warning": None, "alpaca_name": company or sym}
        base.update(overrides.get(sym, {}))
        return base

    def fake_price(sym):
        if sym in fail_tickers:
            raise RuntimeError("price boom")
        return price

    def fake_facts(cik):
        return {"shares": shares}

    def fake_lq(cik):
        return {"form": filing[0], "date": filing[1]}

    triage.llm.call = fake_llm
    market_ref.resolve_ticker = fake_resolve
    fundamentals.price_latest = fake_price
    fundamentals.company_facts_metrics = fake_facts
    gather._latest_quarterly = fake_lq
    if stub_html:
        reports.html_for_brief = lambda p, db=None: Path("/tmp/triage_test.html")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "briefs").mkdir()
    triage.REPO_ROOT = tmp

    def restore():
        triage.llm.call = orig["llm"]
        market_ref.resolve_ticker = orig["resolve"]
        fundamentals.price_latest = orig["price"]
        fundamentals.company_facts_metrics = orig["facts"]
        gather._latest_quarterly = orig["lq"]
        reports.html_for_brief = orig["html"]
        triage.REPO_ROOT = orig["root"]

    return restore


# ── registration ─────────────────────────────────────────────────────────────
def test_triage_radar_registered_in_schema():
    schema = next((t for t in agent_tools.TOOL_SCHEMAS
                   if t["name"] == "triage_radar"), None)
    assert schema is not None, "triage_radar not registered"
    desc = schema["description"].lower()
    assert "queue" in desc and "forward" in desc, desc
    assert "0.10" in desc, desc                        # cost band stated
    assert "never" in desc and ("quick take" in desc or "underwrite" in desc), desc
    # optional max_stories param, no required params
    assert "max_stories" in schema["input_schema"]["properties"], schema
    assert not schema["input_schema"].get("required"), schema


# ── dispatch end-to-end, sync, cheap tier, cost via llm.call, delivery queued ──
def test_dispatch_runs_and_delivers():
    record = {}
    restore = _patch(record)
    ctx = ToolContext(_DB([_constraint(1, "AI infrastructure", "VXEL,ZYXA"),
                           _constraint(2, "Space", "LNCH")]))
    try:
        result = dispatch("triage_radar", {}, ctx)
    finally:
        restore()
    # the ONE synthesis call went through llm.call, on the cheap sonnet tier
    assert record["tier"] == "sonnet", record
    assert record["task"] == "radar-triage", record
    # delivery queued on the context exactly like run_radar
    assert ctx.send_documents == ["/tmp/triage_test.html"], ctx.send_documents
    # result reports the queue size, ticker count, and cost, and the no-spend rule
    assert "2 candidate" in result and "3 ticker" in result, result
    assert "0.12" in result, result
    assert "Nothing was spent" in result, result


def test_snapshot_table_contains_run_date_and_prices():
    record = {}
    restore = _patch(record, price=250.0, shares=2_000_000)
    ctx = ToolContext(_DB([_constraint(1, "AI infrastructure", "VXEL")]))
    try:
        dispatch("triage_radar", {}, ctx)
    finally:
        restore()
    user = record["user"]
    today = date.today().isoformat()
    assert f"fetched {today}" in user, user            # run date in the snapshot
    assert "VXEL" in user and "$250" in user, user      # price rendered
    assert "$500,000,000" in user, user                 # market cap = price×shares


# ── per-ticker failure tolerated (fail-open to n/a, whole triage still runs) ──
def test_per_ticker_failure_tolerated():
    record = {}
    restore = _patch(record, fail_tickers={"BADX"})
    ctx = ToolContext(_DB([_constraint(1, "AI infrastructure", "VXEL,BADX")]))
    try:
        result = dispatch("triage_radar", {}, ctx)
    finally:
        restore()
    # did NOT abort — the tool completed and delivered
    assert "triage done" in result.lower(), result
    assert ctx.send_documents == ["/tmp/triage_test.html"], ctx.send_documents
    # the failed ticker shows n/a in the snapshot, the good one shows a price
    # (rows are now markdown pipe-table lines: "| SYM | company | price | ... |")
    user = record["user"]
    bad_line = next(l for l in user.splitlines() if l.startswith("| BADX "))
    assert "n/a" in bad_line, bad_line
    good_line = next(l for l in user.splitlines() if l.startswith("| VXEL "))
    assert "$100" in good_line, good_line


def test_snapshot_direct_fail_open():
    # unit-level: a helper raising for a ticker yields all-n/a, never an exception.
    restore = _patch({}, fail_tickers={"ZZZZ"})
    try:
        snap = triage._ticker_snapshot("ZZZZ")
    finally:
        restore()
    assert snap == {"symbol": "ZZZZ", "name": "n/a", "price": "n/a",
                    "market_cap": "n/a", "latest_filing": "n/a",
                    "active": None, "sec_filer": None,
                    "identity_warning": None, "alpaca_name": None}, snap


# ── empty queue: no model spend at all ───────────────────────────────────────
def test_empty_queue_no_spend():
    record = {}
    restore = _patch(record)
    ctx = ToolContext(_DB([]))
    try:
        result = dispatch("triage_radar", {}, ctx)
    finally:
        restore()
    assert "empty" in result.lower(), result
    assert "task" not in record, "llm.call must NOT run on an empty queue"
    assert ctx.send_documents == ["/tmp/triage_test.html"], ctx.send_documents


# ── snapshot loop is capped for a huge deduped ticker list (review follow-up) ─
def test_snapshot_cap_enforced_with_note():
    record = {}
    restore = _patch(record)
    # 50 distinct tickers, one per constraint, well past SNAPSHOT_TICKER_CAP (40)
    constraints = [_constraint(i, "Theme", f"T{i:03d}") for i in range(1, 51)]
    ctx = ToolContext(_DB(constraints))
    try:
        dispatch("triage_radar", {}, ctx)
    finally:
        restore()
    user = record["user"]
    # only the first 40 (stable, first-seen order) got a snapshot line
    # (pipe-table rows: "| T001 | company | ... |")
    snapshot_lines = [l for l in user.splitlines() if l.startswith("| T0")]
    assert len(snapshot_lines) == triage.SNAPSHOT_TICKER_CAP, len(snapshot_lines)
    assert snapshot_lines[0].startswith("| T001 "), snapshot_lines[0]
    assert snapshot_lines[-1].startswith("| T040 "), snapshot_lines[-1]
    # T041 appears in the queue block (every constraint is still listed) but
    # must NOT have gotten a snapshot row of its own
    assert "| T041 |" not in user, user
    # the note line is present in the synthesis input
    assert "snapshot covered 40 of 50 tickers" in user, user


def test_snapshot_cap_note_absent_when_under_cap():
    record = {}
    restore = _patch(record)
    ctx = ToolContext(_DB([_constraint(1, "AI infrastructure", "VXEL,ZYXA")]))
    try:
        dispatch("triage_radar", {}, ctx)
    finally:
        restore()
    assert "snapshot covered" not in record["user"], record["user"]


def test_max_stories_flows_into_prompt():
    record = {}
    restore = _patch(record)
    ctx = ToolContext(_DB([_constraint(1, "Cybersecurity", "SENT")]))
    try:
        dispatch("triage_radar", {"max_stories": 2}, ctx)
    finally:
        restore()
    assert "at most 2 distinct" in record["user"], record["user"]


# ── Task 1: synthesis reliability — truncation retry, empty-after-retry fails ──
def _trunc(usd=0.05):
    """A truncated (max_tokens, empty-text) synthesis result — the 07-14 shape."""
    return {"text": "", "usage": {}, "usd": usd, "model": "claude-sonnet-5",
            "stop_reason": "max_tokens"}


def test_truncation_retry_at_larger_budget():
    # First call truncates (max_tokens, no text); the code must retry ONCE at the
    # 1.5x budget and adopt the recovered memo — delivered as success.
    record = {}
    good = {"text": _GOOD_MEMO, "usage": {}, "usd": 0.10,
            "model": "claude-sonnet-5", "stop_reason": "end_turn"}
    restore = _patch(record, llm_seq=[_trunc(), good])
    ctx = ToolContext(_DB([_constraint(1, "AI infrastructure", "VXEL")]))
    try:
        result = dispatch("triage_radar", {}, ctx)
    finally:
        restore()
    calls = record["calls"]
    assert len(calls) == 2, calls                                   # retried once
    assert calls[0]["max_tokens"] == triage.TRIAGE_MAX_TOKENS, calls
    assert calls[1]["max_tokens"] == triage.TRIAGE_RETRY_MAX_TOKENS, calls
    assert calls[0]["task"] == "radar-triage", calls
    assert calls[1]["task"] == "radar-triage-retry", calls
    # recovered → delivered as success, cost is the SUM of both calls
    assert "triage done" in result.lower(), result
    assert ctx.send_documents == ["/tmp/triage_test.html"], ctx.send_documents
    assert "0.15" in result, result                                 # 0.05 + 0.10


def test_empty_after_retry_returns_error_no_memo():
    # Both calls come back empty/truncated → run_triage returns an ERROR dict with
    # cost, writes NO memo file, and never emits the old fallback string.
    record = {}
    restore = _patch(record, llm_seq=[_trunc(0.05), _trunc(0.07)])
    db = _DB([_constraint(1, "AI infrastructure", "VXEL")])
    try:
        out = triage.run_triage(db)
        briefs = list((triage.REPO_ROOT / "briefs").glob("triage_*.md"))
    finally:
        restore()
    assert "error" in out and "memo" not in out and "path" not in out, out
    assert out["cost_usd"] == 0.12, out                             # 0.05 + 0.07
    assert out["retry_used"] is True, out
    assert "no synthesis returned" not in str(out).lower(), out     # old string gone
    assert briefs == [], briefs                                     # NO memo written


def test_empty_after_retry_dispatch_surfaces_failure_no_attachment():
    # The same failure at the dispatch layer: a failure message is returned to the
    # agent and NO html document is queued.
    record = {}
    restore = _patch(record, llm_seq=[_trunc(0.05), _trunc(0.07)])
    ctx = ToolContext(_DB([_constraint(1, "AI infrastructure", "VXEL")]))
    try:
        result = dispatch("triage_radar", {}, ctx)
        docs = list(ctx.send_documents)
    finally:
        restore()
    assert "failed to produce a synthesis" in result.lower(), result
    assert "after retry" in result.lower(), result
    assert docs == [], docs                                         # NO attachment


# ── Task 2: readable memo — company column, no code fence, real <table> ────────
def test_company_column_present_and_named():
    record = {}
    restore = _patch(record, company="Vaxel Compute Inc.")
    ctx = ToolContext(_DB([_constraint(1, "AI infrastructure", "VXEL")]))
    try:
        dispatch("triage_radar", {}, ctx)
    finally:
        restore()
    user = record["user"]
    assert "| ticker | company | price |" in user, user            # header column
    row = next(l for l in user.splitlines() if l.startswith("| VXEL "))
    assert "Vaxel Compute Inc." in row, row                                # company name


def test_memo_has_pipe_table_no_code_fence_and_renders_table():
    # The memo must carry the snapshot as a markdown pipe table (NO ``` fence), and
    # reports.html_for_brief must render it to a real <table> (07-14: the fenced
    # snapshot rendered as garbage <p>```</p> lines).
    from scout import reports
    record = {}
    restore = _patch(record, company="Vaxel Compute Inc.", stub_html=False)
    db = _DB([_constraint(1, "AI infrastructure", "VXEL,ZYXA")])
    try:
        out = triage.run_triage(db)
        assert "```" not in out["memo"], out["memo"]               # no code fence
        assert "| ticker | company | price |" in out["memo"], out["memo"]
        html_path = reports.html_for_brief(out["path"])
        html = Path(html_path).read_text()
    finally:
        restore()
    assert "<table>" in html, html                                 # real table
    assert "<th>ticker</th>" in html, html
    assert "Vaxel Compute Inc." in html, html


# ── 2026-07-15 stale-ticker fix: identity-flag rendering + queue hygiene ──────
def test_snapshot_cells_render_both_identity_shapes():
    # Deterministic cell rendering — no LLM judgment. ARJT (inactive) blanks every
    # data cell; PHTN (reassigned, active non-filer) hides the fetched price and
    # attributes the row to the Alpaca-listed instrument.
    inactive = {"symbol": "ARJT", "name": "Ardent Rocketworks Holdings Inc.",
                "price": 55.0, "market_cap": 4_000_000,
                "latest_filing": "10-K 2023-02-01", "active": False,
                "sec_filer": False, "alpaca_name": "Ardent Rocketworks Holdings Inc.",
                "identity_warning": "inactive/delisted on exchange"}
    assert triage._snapshot_cells(inactive) == [
        "Ardent Rocketworks Holdings Inc. ⚠ inactive/delisted",
        "n/a (inactive/delisted)", "n/a (inactive/delisted)",
        "n/a (inactive/delisted)"], triage._snapshot_cells(inactive)

    reassigned = {"symbol": "PHTN", "name": "n/a", "price": 12.3,
                  "market_cap": "n/a", "latest_filing": "n/a", "active": True,
                  "sec_filer": False,
                  "alpaca_name": "Vega Photonics & Optical ETF",
                  "identity_warning": "not in SEC EDGAR's ticker registry"}
    cells = triage._snapshot_cells(reassigned)
    assert cells[0] == ("Vega Photonics & Optical ETF ⚠ possible ticker "
                        "reassignment — verify"), cells
    assert cells[1] == "n/a — identity unverified", cells
    assert not any("12.3" in c for c in cells), cells   # fetched price never leaks

    clean = {"symbol": "VXEL", "name": "Vaxel Compute Inc.", "price": 100,
             "market_cap": 1000, "latest_filing": "10-Q 2026-06-30", "active": True,
             "sec_filer": True, "alpaca_name": "Vaxel Compute Inc.",
             "identity_warning": None}
    assert triage._snapshot_cells(clean) == [
        "Vaxel Compute Inc.", "$100", "$1,000", "10-Q 2026-06-30"], triage._snapshot_cells(clean)


def test_header_note_warns_on_identity_suspect_rows():
    tbl = triage._snapshot_table([{"symbol": "VXEL", "name": "Vaxel Compute Inc.",
                                    "price": 100, "market_cap": 1000,
                                    "latest_filing": "n/a", "active": True,
                                    "sec_filer": True, "alpaca_name": "Vaxel Compute Inc.",
                                    "identity_warning": None}], "2026-07-15")
    assert "IDENTITY-SUSPECT" in tbl, tbl
    assert "⚠" in tbl, tbl


def test_reassigned_ticker_flagged_in_snapshot_end_to_end():
    # PHTN (non-filer but active — the ETF reassignment) flows through the whole
    # triage: its snapshot row hides the fetched price and flags reassignment, and
    # the model header carries the identity-suspect warning.
    record = {}
    restore = _patch(record, price=12.30, resolve_overrides={
        "PHTN": {"sec_filer": False, "active": True, "cik": None,
                 "name": "Vega Photonics & Optical ETF",
                 "alpaca_name": "Vega Photonics & Optical ETF",
                 "identity_warning": "not in SEC EDGAR's ticker registry — "
                 "ticker may have been reassigned"}})
    ctx = ToolContext(_DB([_constraint(1, "Autonomy hardware", "PHTN")]))
    try:
        dispatch("triage_radar", {}, ctx)
    finally:
        restore()
    user = record["user"]
    assert "IDENTITY-SUSPECT" in user, user
    row = next(l for l in user.splitlines() if l.startswith("| PHTN "))
    assert "possible ticker reassignment — verify" in row, row
    assert "n/a — identity unverified" in row, row
    assert "Vega Photonics" in row, row
    assert "12.3" not in row, row      # the fetched price is never shown as PHTN's


def test_inactive_ticker_excluded_from_snapshot_and_retired_noted():
    # ARJT (Alpaca inactive) is EXCLUDED from the snapshot fetch and surfaced in a
    # Retired-tickers memo note; the live ticker in the same constraint still gets
    # a snapshot row. DB queue rows are untouched (run_triage never writes them).
    record = {}
    restore = _patch(record, resolve_overrides={
        "ARJT": {"active": False, "sec_filer": False, "status": "inactive",
                 "name": "Ardent Rocketworks Holdings Inc."}})
    db = _DB([_constraint(1, "Space/defense", "LNCH,ARJT")])
    try:
        out = triage.run_triage(db)
    finally:
        restore()
    user = record["user"]
    assert "| ARJT |" not in user, user                 # no snapshot row fetched
    assert "| LNCH |" in user, user                     # the live one still fetched
    assert "excluded as inactive/delisted, not fetched: ARJT" in user, user
    memo = out["memo"]
    assert "## Retired tickers" in memo, memo
    assert "ARJT — inactive/delisted" in memo, memo
    assert "queue entry is stale" in memo, memo
    assert "status=inactive" in memo, memo


def test_no_double_resolve_per_ticker():
    # T4: resolve_ticker is called at most ONCE per unique queue ticker in a run
    # (resolve up front, reuse the dict in the snapshot).
    record = {}
    calls = {}
    restore = _patch(record)
    from scout import market_ref
    wrapped = market_ref.resolve_ticker

    def counting(sym):
        calls[sym] = calls.get(sym, 0) + 1
        return wrapped(sym)

    market_ref.resolve_ticker = counting
    ctx = ToolContext(_DB([_constraint(1, "AI infrastructure", "VXEL,ZYXA"),
                           _constraint(2, "AI infrastructure", "VXEL")]))
    try:
        dispatch("triage_radar", {}, ctx)
    finally:
        restore()
    assert calls.get("VXEL") == 1, calls                # deduped, resolved once
    assert calls.get("ZYXA") == 1, calls
