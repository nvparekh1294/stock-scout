"""scout/test_query_db.py — the query_db honesty + pagination rework (Task 1).

Plain-Python asserts (no pytest, no LLM, no network — a tiny in-memory fake db):

    scout/.venv/bin/python -m scout.test_query_db

Covers the owner-reported failure (agent_tools.py:292 used a blind [:3500] slice
that silently truncated a 30-row query mid-JSON, with no offset and no notice):
  - a >3500-char result never cuts a row mid-way AND appends an honest _meta line;
  - offset pages forward correctly and the pages are contiguous + complete;
  - the constraints table renders COMPACT with the FULL ticker list preserved;
  - a small, complete result is unchanged (a plain JSON array, no _meta).
"""

from __future__ import annotations

import json

from scout import agent_tools
from scout.agent_tools import ToolContext, dispatch


class _DB:
    """Minimal stand-in: holds rows per table, supports select(where, order_by)."""
    def __init__(self, tables):
        self._t = tables

    def select(self, table, where=None, order_by=None):
        rows = list(self._t.get(table, []))
        if where:
            rows = [r for r in rows
                    if all(str(r.get(k)) == str(v) for k, v in where.items())]
        if order_by:
            rows = sorted(rows, key=lambda r: (r.get(order_by) is None,
                                               r.get(order_by)))
        return rows


def _q(tables, ti):
    return dispatch("query_db", ti, ToolContext(_DB(tables)))


# ── big result: whole rows + honest _meta, never cut mid-row ─────────────────
def test_large_result_never_cuts_row_and_has_meta():
    # 30 theses rows, each padded well past a row so the 3500-char budget bites.
    rows = [{"id": i, "symbol": "AAA", "thesis_text": "x" * 300} for i in range(1, 31)]
    out = _q({"theses": rows}, {"table": "theses"})
    body, _, meta = out.partition("\n")
    # body must be a COMPLETE, parseable JSON array (no mid-row cut)
    parsed = json.loads(body)
    assert isinstance(parsed, list) and 0 < len(parsed) < 30, len(parsed)
    # every emitted row is intact (round-trips to the original dict)
    assert parsed[0] == rows[0], parsed[0]
    # an _meta line was appended, naming the total and the next offset
    assert meta, out
    m = json.loads(meta)["_meta"]
    assert "of 30" in m and f"offset={len(parsed)}" in m, m


# ── offset pages forward, pages are contiguous and cover the whole set ────────
def test_offset_pages_correctly():
    rows = [{"id": i, "symbol": "AAA", "thesis_text": "x" * 300} for i in range(1, 31)]
    seen, offset, guard = [], 0, 0
    while True:
        guard += 1
        assert guard < 20, "pagination did not terminate"
        out = _q({"theses": rows}, {"table": "theses", "offset": offset})
        body, _, meta = out.partition("\n")
        page = json.loads(body)
        seen += [r["id"] for r in page]
        # the last page still reports its position but advertises NO next offset
        if not meta or "offset=" not in json.loads(meta)["_meta"]:
            break
        m = json.loads(meta)["_meta"]
        nxt = int(m.split("offset=")[1])
        assert nxt == offset + len(page), (nxt, offset, len(page))
        offset = nxt
    # contiguous, in order, and complete — no row dropped, none duplicated
    assert seen == list(range(1, 31)), seen


# ── constraints render compact, with the FULL ticker list ────────────────────
def test_constraints_compact_with_full_tickers():
    long_prose = "This constraint concerns a very long dependency chain " * 4
    tickers = "AAAA,BBBB,CCCC,DDDD"
    rows = [{"id": 7, "theme": "Grid storage", "tier": 2, "status": "candidate",
             "confirmed_by_owner": False,
             "description": long_prose + " | tickers: " + tickers}]
    out = _q({"constraints": rows}, {"table": "constraints"})
    assert "_meta" not in out, out                 # one small row fits
    parsed = json.loads(out)
    row = parsed[0]
    # compact field set — no raw giant description, prose capped, tickers FULL
    assert set(row) == {"id", "theme", "tier", "status", "prose", "tickers"}, row
    assert "description" not in row, row
    assert len(row["prose"]) == 140, len(row["prose"])
    assert row["tickers"] == tickers, row["tickers"]    # never truncated


def test_constraints_full_tickers_survive_even_when_paged():
    # A queue big enough to page: every row that IS emitted keeps its full tickers.
    tickers = "WWWW,XXXX,YYYY,ZZZZ"
    rows = [{"id": i, "theme": "AI infra", "tier": 1, "status": "candidate",
             "confirmed_by_owner": False,
             "description": ("prose " * 20) + " | tickers: " + tickers}
            for i in range(1, 41)]
    out = _q({"constraints": rows}, {"table": "constraints"})
    body, _, meta = out.partition("\n")
    for r in json.loads(body):
        assert r["tickers"] == tickers, r
    assert meta, "a 40-row compact queue should still page honestly"


# ── small complete result: plain JSON array, no _meta (behavior unchanged) ────
def test_small_result_unchanged_no_meta():
    rows = [{"id": 1, "symbol": "AAA", "thesis_text": "short"},
            {"id": 2, "symbol": "AAA", "thesis_text": "also short"}]
    out = _q({"theses": rows}, {"table": "theses", "symbol": "AAA"})
    assert "_meta" not in out, out
    assert json.loads(out) == rows, out
    # identical to the old json.dumps(rows) rendering
    assert out == json.dumps(rows, default=str), out


def test_empty_result_is_empty_array():
    out = _q({"theses": []}, {"table": "theses"})
    assert out == "[]", out


# ── description states row order honestly (review follow-up) ────────────────
def test_description_states_ordering():
    schema = next(t for t in agent_tools.TOOL_SCHEMAS if t["name"] == "query_db")
    desc = schema["description"].lower()
    assert "id-ascending" in desc, desc
    assert "offset=0 is the oldest" in desc, desc
    assert "cost_report" in desc and "list_ledger" in desc, desc
