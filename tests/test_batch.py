"""scout/test_batch.py — unit cases for the Batch API fan-out llm.call_batch
(Task 1). Plain-Python asserts, no pytest, no network: the Anthropic client and
time.sleep are monkeypatched.

    scout/.venv/bin/python -m scout.test_batch

Covers the full lifecycle: a happy-path batch (results keyed by custom_id, in
request order, cost logged at the 50% batch rate), a hard-timeout → cancel →
sync-fallback path, a partial batch (one item errors → that item falls back to
sync), and the pre-submit budget refusal.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from scout import db as dbmod
from scout import llm
from scout.db import Database


# ── fakes ───────────────────────────────────────────────────────────────────
class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, i=1000, o=500, cr=0, cc=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = cr
        self.cache_creation_input_tokens = cc


class _Msg:
    def __init__(self, text, stop="end_turn"):
        self.content = [_Block(text)]
        self.usage = _Usage()
        self.stop_reason = stop


class _ResInner:
    def __init__(self, typ, msg=None):
        self.type = typ
        self.message = msg


class _Result:
    def __init__(self, cid, typ, msg=None):
        self.custom_id = cid
        self.result = _ResInner(typ, msg)


class _Batch:
    def __init__(self, bid="batch_1", status="in_progress"):
        self.id = bid
        self.processing_status = status


class _FakeBatches:
    def __init__(self, script):
        self.s = script
        self._polls = 0

    def create(self, requests):
        self.s["create_calls"] += 1
        self.s["created"] = list(requests)
        return _Batch(status="in_progress")

    def retrieve(self, bid):
        seq = self.s["statuses"]
        st = seq[min(self._polls, len(seq) - 1)]
        self._polls += 1
        return _Batch(bid=bid, status=st)

    def results(self, bid):
        out = []
        for i, req in enumerate(self.s["created"]):
            cid = req["custom_id"]
            if self.s["mode"] == "one_error" and i == 0:
                out.append(_Result(cid, "errored"))
            else:
                out.append(_Result(cid, "succeeded", _Msg(f"BATCH:{cid}")))
        # deliberately shuffle so the ordering logic (key by custom_id) is tested
        return list(reversed(out))

    def cancel(self, bid):
        self.s["cancelled"].append(bid)


class _FakeMessages:
    def __init__(self, script):
        self.batches = _FakeBatches(script)
        self.s = script

    def create(self, **kwargs):
        self.s["sync_calls"] += 1
        return _Msg("SYNC-OK")


class _FakeClient:
    def __init__(self, script):
        self.messages = _FakeMessages(script)


def _install(script):
    """Point llm at a fake client + no-op sleep/key. Returns an undo callable."""
    client = _FakeClient(script)
    saved = (llm.anthropic.Anthropic, llm._require_api_key, llm.time.sleep)
    llm.anthropic.Anthropic = lambda *a, **k: client
    llm._require_api_key = lambda: "test-key"
    llm.time.sleep = lambda *_a, **_k: None

    def undo():
        (llm.anthropic.Anthropic, llm._require_api_key, llm.time.sleep) = saved
    return undo


def _script(mode="all_success", statuses=("ended",)):
    return {"create_calls": 0, "sync_calls": 0, "created": None,
            "cancelled": [], "statuses": list(statuses), "mode": mode}


def _tmpdb(tmp):
    dbmod.LOCALDB_DIR = Path(tmp)
    d = Database(db_url="")
    d.apply_schema()
    return d


def _reqs(n):
    return [{"task": f"walk-{i}", "model_tier": "opus",
             "messages": [{"role": "user", "content": f"Theme {i}"}],
             "max_tokens": 100, "system": "SYS"} for i in range(n)]


# ── tests ────────────────────────────────────────────────────────────────────
def test_batch_happy_path_order_and_cost():
    _orig = dbmod.LOCALDB_DIR
    s = _script(statuses=("ended",))
    undo = _install(s)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = _tmpdb(tmp)
            out = llm.call_batch(_reqs(3), db=db, monthly_budget=1000.0)
            assert s["create_calls"] == 1, "exactly one batch submitted"
            assert s["sync_calls"] == 0, "no sync fallback on a clean batch"
            # results come back reversed from the API but must be re-ordered:
            assert [r["text"] for r in out] == ["BATCH:r0", "BATCH:r1", "BATCH:r2"], out
            assert all(r["via"] == "batch" for r in out)
            # 50% batch rate: opus 1000 in / 500 out = 0.0175 full → 0.00875
            assert abs(out[0]["usd"] - 0.00875) < 1e-9, out[0]["usd"]
            rows = db.select("api_costs")
            assert len(rows) == 3 and all("(batch)" in r["task"] for r in rows)
            db.close()
    finally:
        undo()
        dbmod.LOCALDB_DIR = _orig


def test_batch_timeout_falls_back_to_sync():
    _orig = dbmod.LOCALDB_DIR
    s = _script(statuses=("in_progress",))     # never ends
    undo = _install(s)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = _tmpdb(tmp)
            out = llm.call_batch(_reqs(2), db=db, monthly_budget=1000.0,
                                 hard_timeout=0)     # times out immediately
            assert s["create_calls"] == 1
            assert s["cancelled"] == ["batch_1"], "timed-out batch must be cancelled"
            assert s["sync_calls"] == 2, "both items served synchronously"
            assert all(r["via"] == "sync-fallback" for r in out), out
            assert all(r["text"] == "SYNC-OK" for r in out)
            # sync path logs at FULL rate (no batch discount): 0.0175
            assert abs(out[0]["usd"] - 0.0175) < 1e-9, out[0]["usd"]
            db.close()
    finally:
        undo()
        dbmod.LOCALDB_DIR = _orig


def test_batch_partial_error_falls_back_for_that_item_only():
    _orig = dbmod.LOCALDB_DIR
    s = _script(mode="one_error", statuses=("ended",))
    undo = _install(s)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = _tmpdb(tmp)
            out = llm.call_batch(_reqs(3), db=db, monthly_budget=1000.0)
            assert s["sync_calls"] == 1, "only the errored item retries synchronously"
            assert out[0]["via"] == "sync-fallback" and out[0]["text"] == "SYNC-OK"
            assert out[1]["via"] == "batch" and out[1]["text"] == "BATCH:r1"
            assert out[2]["via"] == "batch"
            db.close()
    finally:
        undo()
        dbmod.LOCALDB_DIR = _orig


def test_batch_refuses_over_budget_before_submit():
    _orig = dbmod.LOCALDB_DIR
    s = _script(statuses=("ended",))
    undo = _install(s)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = _tmpdb(tmp)
            raised = False
            try:
                llm.call_batch(_reqs(2), db=db, monthly_budget=0.0)
            except llm.BudgetExceeded:
                raised = True
            assert raised, "an over-budget batch must raise BudgetExceeded"
            assert s["create_calls"] == 0, "must refuse BEFORE submitting the batch"
            db.close()
    finally:
        undo()
        dbmod.LOCALDB_DIR = _orig


def test_empty_requests_returns_empty():
    assert llm.call_batch([]) == []
