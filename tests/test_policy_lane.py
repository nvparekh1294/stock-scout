"""scout/test_policy_lane.py — unit cases for the policy fast-lane message guard
(Task 3, 2026-07-13) and the classification-retry honesty fix (2026-07-13).
Plain-Python asserts (no pytest, no LLM spend, no network — _classify /
_efts_search are monkeypatched):

    scout/.venv/bin/python -m scout.test_policy_lane

This morning's bug: the classifier's INTERNAL next-step reasoning ("Retrieve and
review the full EX-10.1 exhibit text. If it specifies … escalate to
**BENEFICIARY_TRIGGER** …") was pushed to the owner verbatim and truncated
mid-word at [:400]. These tests pin the fix:
  - a bare trigger token in prose is NOT a classification (only `class: TOKEN` is);
  - clean_reason strips internal-instruction sentences;
  - format_alert builds a clean, deterministic alert from item metadata;
  - truncate_telegram cuts at a sentence boundary, never mid-word;
  - a failed / unusable generation yields a minimal deterministic alert.

A second bug (found in review): a filing whose classification failed/was
unusable was still committed to `evidence` (the never-read-twice dedup), so
the next hourly scan filtered it back out — it was never actually retried,
even though the alert promised "will retry next cycle." These tests pin the
fix: failed/unusable items are withheld from `evidence` until a usable
classification lands OR MAX_RETRIES cycles have failed, at which point an
honest terminal "giving up" alert replaces the "pending" one.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from scout import db as _db
from scout import policy_lane
from scout.db import Database

_ITEM = {
    "date": "2026-07-10",
    "company": "Norvance Grid Corp (NRDX) (CIK 0000700100)",
    "form": "8-K",
    "url": "https://www.sec.gov/Archives/edgar/data/700100/abc/",
    "query": '"contract award" "Department of"',
}


# ── parse_verdicts: structure discipline ────────────────────────────────────
def test_structured_beneficiary_line_is_usable():
    verdicts = (f"2026-07-10 | {_ITEM['url']} | class: BENEFICIARY_TRIGGER | "
                "Norvance won a $150M Department of Energy award (8-K filed 2026-07-10).")
    parsed = policy_lane.parse_verdicts([_ITEM], verdicts)
    p = parsed[0]
    assert p["usable"] and p["cls"] == "BENEFICIARY_TRIGGER", p
    assert "$150M" in p["reason"], p["reason"]


def test_bare_token_in_prose_is_not_a_classification():
    # The incident: internal reasoning that mentions the token but has no
    # structured `class:` field. It must NOT be read as a beneficiary trigger.
    verdicts = (f"2026-07-10 | {_ITEM['url']} | Retrieve and review the full "
                "EX-10.1 exhibit text. If it specifies a firm dollar amount, "
                "escalate to **BENEFICIARY_TRIGGER**; otherwise WATCH.")
    parsed = policy_lane.parse_verdicts([_ITEM], verdicts)
    assert parsed[0]["cls"] is None and not parsed[0]["usable"], parsed[0]
    assert policy_lane.classification_unusable(verdicts, parsed) is True


def test_watch_classification_is_usable_but_not_beneficiary():
    verdicts = (f"2026-07-10 | {_ITEM['url']} | class: WATCH_TRIGGER | "
                "Tariff threat only; no committed money (post, not a filing).")
    parsed = policy_lane.parse_verdicts([_ITEM], verdicts)
    assert parsed[0]["cls"] == "WATCH_TRIGGER" and parsed[0]["usable"]
    assert policy_lane.classification_unusable(verdicts, parsed) is False


# ── clean_reason: strips internal instructions ──────────────────────────────
def test_clean_reason_drops_internal_and_keeps_fact():
    r = ("Norvance won a $150M Department of Energy award (8-K filed 2026-07-10). "
         "I will retrieve EX-10.1 and, if it specifies an amount, escalate to "
         "BENEFICIARY_TRIGGER.")
    out = policy_lane.clean_reason(r)
    assert out.startswith("Norvance won a $150M"), out
    assert "escalate" not in out.lower() and "i will" not in out.lower(), out


def test_clean_reason_all_internal_falls_back_to_default():
    r = "Retrieve and review the full EX-10.1 exhibit text. Then classify."
    assert policy_lane.clean_reason(r) == policy_lane._DEFAULT_REASON


# ── format_alert: deterministic, structured ─────────────────────────────────
def test_format_alert_structure_and_ticker():
    text, tk = policy_lane.format_alert(
        _ITEM, "BENEFICIARY_TRIGGER", "Norvance won a $150M DoE award.")
    assert tk == "NRDX", tk
    assert "🏛 POLICY FAST LANE — Norvance Grid Corp" in text
    assert "Filed: 8-K · 2026-07-10" in text
    assert _ITEM["url"] in text
    assert "BENEFICIARY (official committed money)" in text
    assert "Why it matters: Norvance won a $150M DoE award." in text
    # no internal reasoning leaked
    assert "escalate to" not in text.lower()


# ── truncate_telegram: sentence boundary, never mid-word ─────────────────────
def test_truncate_short_text_unchanged():
    assert policy_lane.truncate_telegram("hi.") == "hi."


def test_truncate_cuts_at_sentence_boundary_with_note():
    body = ("First sentence is here. Second sentence is here. " + "x" * 5000)
    out = policy_lane.truncate_telegram(body, limit=60)
    assert out.endswith("(full detail in next check)"), out
    assert len(out) <= 60
    # cut landed on a sentence boundary, not mid-word
    assert "First sentence is here." in out
    assert " x" not in out  # the runaway padding was dropped cleanly


def test_truncate_never_splits_a_word():
    body = "supercalifragilistic " * 500
    out = policy_lane.truncate_telegram(body, limit=100)
    # every whitespace-split token is a whole word (no partial fragment)
    words = out.replace("(full detail in next check)", "").split()
    assert all(w == "supercalifragilistic" for w in words), out


# ── minimal_alert + failed generation fallback ──────────────────────────────
def test_minimal_alert_has_facts_and_retry_note():
    m = policy_lane.minimal_alert(_ITEM)
    assert "Norvance Grid Corp (NRDX)" in m and _ITEM["url"] in m
    assert "8-K · 2026-07-10" in m
    assert "will retry next cycle" in m


def _run_scan_with(monkey_classify, check=None):
    """Drive run_scan with canned EDGAR items and a scripted classifier, on an
    ISOLATED JSON-fallback DB (temp LOCALDB_DIR — no shared-store pollution, so
    the evidence never-read-twice dedup can't leak between cases), quick_take off
    (no research/LLM). If `check` is given, it is called as check(db, out)
    BEFORE the db is closed, so callers can assert on persisted state (evidence,
    retry counters) that `out` alone doesn't expose."""
    orig_efts, orig_classify = policy_lane._efts_search, policy_lane._classify
    orig_localdb = _db.LOCALDB_DIR
    policy_lane._efts_search = lambda q, since: (
        [dict(_ITEM)] if q == policy_lane.QUERIES[0] else [])
    policy_lane._classify = monkey_classify
    try:
        with tempfile.TemporaryDirectory() as d:
            _db.LOCALDB_DIR = Path(d)
            db = Database(db_url="")
            db.apply_schema()
            out = policy_lane.run_scan(db=db, quick_take=False)
            if check:
                check(db, out)
            db.close()
            return out
    finally:
        policy_lane._efts_search, policy_lane._classify = orig_efts, orig_classify
        _db.LOCALDB_DIR = orig_localdb


def test_run_scan_failed_generation_sends_minimal_alert():
    def _boom(db, items, as_of):
        raise RuntimeError("API credits exhausted")

    def _check(db, out):
        # The core of the 2026-07-13 retry fix: a failed cycle must NOT commit
        # the item to evidence, or "will retry next cycle" would be a lie.
        assert db.select_one("evidence", {"source_url": _ITEM["url"]}) is None
        assert policy_lane._get_retry_count(db, _ITEM["url"]) == 1

    out = _run_scan_with(_boom, check=_check)
    assert len(out["alerts"]) == 1, out
    assert "will retry next cycle" in out["alerts"][0]
    assert _ITEM["url"] in out["alerts"][0]


def test_run_scan_internal_reasoning_does_not_leak():
    # The exact failure mode: the model returns internal reasoning, no `class:`.
    def _internal(db, items, as_of):
        return ("Retrieve and review the full EX-10.1 exhibit text. If it "
                "specifies a firm dollar amount, escalate to BENEFICIARY_TRIGGER")

    def _check(db, out):
        assert db.select_one("evidence", {"source_url": _ITEM["url"]}) is None

    out = _run_scan_with(_internal, check=_check)
    # unusable → minimal alert, and the raw reasoning is NOT what got sent
    assert len(out["alerts"]) == 1, out
    assert "escalate to" not in out["alerts"][0].lower()
    assert "Analysis pending" in out["alerts"][0]


# ── real retry across cycles (2026-07-13 honesty fix) ───────────────────────
# The bug this fix closes: a failed/unusable classification was still inserted
# into `evidence`, so the next hourly scan filtered it straight back out — it
# was never actually retried, even though the alert promised it would be.
# These tests drive run_scan across MULTIPLE cycles on the SAME db to prove
# the item is genuinely re-fetched and re-classified, not silently dropped.

def test_run_scan_retries_across_cycles_then_gives_up_honestly():
    def _boom(db, items, as_of):
        raise RuntimeError("simulated classify failure")

    orig_efts, orig_classify = policy_lane._efts_search, policy_lane._classify
    orig_localdb = _db.LOCALDB_DIR
    policy_lane._efts_search = lambda q, since: (
        [dict(_ITEM)] if q == policy_lane.QUERIES[0] else [])
    policy_lane._classify = _boom
    try:
        with tempfile.TemporaryDirectory() as d:
            _db.LOCALDB_DIR = Path(d)
            db = Database(db_url="")
            db.apply_schema()

            # Cycles 1 and 2: genuinely re-scanned and re-fail; NOT in evidence.
            for cycle in (1, 2):
                out = policy_lane.run_scan(db=db, quick_take=False)
                assert out["scanned"] == 1, (cycle, out)
                assert len(out["alerts"]) == 1, (cycle, out)
                assert "will retry next cycle" in out["alerts"][0], (cycle, out)
                assert db.select_one(
                    "evidence", {"source_url": _ITEM["url"]}) is None, cycle
                assert policy_lane._get_retry_count(db, _ITEM["url"]) == cycle

            # Cycle 3 == MAX_RETRIES: terminal honest "giving up" alert, and
            # the item is FINALLY committed to evidence (retry stops here).
            out3 = policy_lane.run_scan(db=db, quick_take=False)
            assert out3["scanned"] == 1, out3
            assert len(out3["alerts"]) == 1, out3
            a = out3["alerts"][0]
            assert "giving up" in a.lower(), a
            assert "will retry" not in a.lower(), a
            assert db.select_one("evidence", {"source_url": _ITEM["url"]}) is not None
            assert policy_lane._get_retry_count(db, _ITEM["url"]) == 0  # cleared

            # Cycle 4: never-read-twice dedup now suppresses it for good — no
            # more scans, no more alerts (proves retry has a real ceiling, no
            # spam past MAX_RETRIES).
            out4 = policy_lane.run_scan(db=db, quick_take=False)
            assert out4["scanned"] == 0, out4
            assert out4["alerts"] == [], out4

            db.close()
    finally:
        policy_lane._efts_search, policy_lane._classify = orig_efts, orig_classify
        _db.LOCALDB_DIR = orig_localdb


def test_run_scan_success_after_a_failure_commits_once_and_clears_retry():
    calls = {"n": 0}

    def _flaky(db, items, as_of):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient failure")
        return (f"2026-07-10 | {_ITEM['url']} | class: WATCH_TRIGGER | "
                "No committed money; watch only.")

    orig_efts, orig_classify = policy_lane._efts_search, policy_lane._classify
    orig_localdb = _db.LOCALDB_DIR
    policy_lane._efts_search = lambda q, since: (
        [dict(_ITEM)] if q == policy_lane.QUERIES[0] else [])
    policy_lane._classify = _flaky
    try:
        with tempfile.TemporaryDirectory() as d:
            _db.LOCALDB_DIR = Path(d)
            db = Database(db_url="")
            db.apply_schema()

            out1 = policy_lane.run_scan(db=db, quick_take=False)
            assert out1["scanned"] == 1, out1
            assert db.select_one("evidence", {"source_url": _ITEM["url"]}) is None
            assert policy_lane._get_retry_count(db, _ITEM["url"]) == 1

            # A successful cycle 2 commits to evidence right away (no need to
            # keep retrying something that just worked) and clears the counter.
            out2 = policy_lane.run_scan(db=db, quick_take=False)
            assert out2["scanned"] == 1, out2  # proves it was genuinely re-fetched
            assert out2["alerts"] == [], out2  # clean WATCH → stays silent
            assert db.select_one("evidence", {"source_url": _ITEM["url"]}) is not None
            assert policy_lane._get_retry_count(db, _ITEM["url"]) == 0

            db.close()
    finally:
        policy_lane._efts_search, policy_lane._classify = orig_efts, orig_classify
        _db.LOCALDB_DIR = orig_localdb


def test_run_scan_clean_beneficiary_produces_clean_alert():
    def _ben(db, items, as_of):
        return (f"2026-07-10 | {_ITEM['url']} | class: BENEFICIARY_TRIGGER | "
                "Norvance won a $150M Department of Energy award (8-K filed 2026-07-10).")
    out = _run_scan_with(_ben)
    assert len(out["alerts"]) == 1, out
    a = out["alerts"][0]
    assert "BENEFICIARY (official committed money)" in a
    assert "$150M" in a and "consent-gated" in a


def test_run_scan_clean_watch_stays_silent():
    def _watch(db, items, as_of):
        return (f"2026-07-10 | {_ITEM['url']} | class: WATCH_TRIGGER | "
                "No committed money; watch only.")
    out = _run_scan_with(_watch)
    assert out["alerts"] == [], out
