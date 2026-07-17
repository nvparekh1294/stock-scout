"""tests/test_gather_recency.py — unit cases for the hardened recency gate
(audit #4). No network, no LLM spend (the one extract path that would call the
model is stubbed):

    python -m pytest tests/test_gather_recency.py

The incident: a standard brief shipped with "latest quarter ... zero extracted
content", even though the recency gate was supposed to force the latest quarter
into the pack. Root cause: the gate only checked that a doc with the latest
quarter's URL/date was present, so an empty EXTRACTION (failed fetch, or a
boilerplate-only front page) still passed the check, and a failed extraction was
cached forever by the never-read-twice rule.

These tests pin the fix:
  - `_extraction_has_content` keys on real FACTS at the target date, not presence;
  - `_extract_doc` returns '' and stores NOTHING on a failed/empty fetch;
  - `_extract_doc` returns '' and stores nothing when the model yields no content;
  - a content-bearing same-day 8-K satisfies the quarter, a header-only one does not.

All figures below are invented, not derived from any real security.
"""

from __future__ import annotations

import inspect

import pytest

from scout import gather


@pytest.fixture
def monkeypatch_llm(monkeypatch):
    """A tiny setter fixture matching the private harness's callable form:
    `monkeypatch_llm(fn)` swaps gather.llm.call for `fn` (auto-restored)."""
    def _set(fn):
        monkeypatch.setattr(gather.llm, "call", fn)
    return _set


# ── a minimal in-memory stand-in for Database (no JSON, no Postgres) ──────────
class _FakeDB:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.inserted = []
        self.updated = []

    def select_one(self, table, where):
        for r in self.rows:
            if all(r.get(k) == v for k, v in (where or {}).items()):
                return r
        return None

    def insert(self, table, row):
        self.inserted.append(row)
        row = {**row, "id": len(self.rows) + 1}
        self.rows.append(row)
        return row["id"]

    def update(self, table, row_id, changes):
        self.updated.append((row_id, changes))
        for r in self.rows:
            if r.get("id") == row_id:
                r.update(changes)


# ── _extraction_has_content ──────────────────────────────────────────────────
def test_content_present_for_matching_date():
    ex = ["[8-K filed 2026-06-04] Record revenue $412.0M; backlog $3,150M; EPS $4.05."]
    assert gather._extraction_has_content(ex, "2026-06-04")


def test_header_only_extraction_is_not_content():
    # A URL/date present but empty facts must NOT satisfy the quarter.
    assert not gather._extraction_has_content(["[10-Q filed 2026-06-04] "], "2026-06-04")


def test_empty_string_extraction_is_not_content():
    assert not gather._extraction_has_content(["", None], "2026-06-04")


def test_wrong_date_is_not_content():
    ex = ["[8-K filed 2026-03-26] EPS $6.80; EBITDA margin 15.5%."]
    assert not gather._extraction_has_content(ex, "2026-06-04")


def test_same_day_8k_with_content_satisfies_quarter():
    # The 10-Q body extracted empty, but the same-day earnings 8-K carries the
    # figures — that is the path that must reach the pack.
    ex = ["[10-Q filed 2026-06-04] ",
          "[8-K filed 2026-06-04] Record revenue $412.0M; backlog $3,150M vs $3,020M."]
    assert gather._extraction_has_content(ex, "2026-06-04")


def test_verbose_refusal_is_not_content():
    # A long model refusal that CLEARS the char floor and even carries digits
    # ("10-Q", "SEC") must NOT satisfy the quarter — otherwise the recency gate
    # never fires and the pack ships stale.
    ex = ["[10-Q filed 2026-06-04] I cannot extract the requested financial "
          "figures from this document. The provided text consists almost entirely "
          "of XML metadata and structural tags from the SEC 10-Q submission, with "
          "no narrative or numeric financial content present to extract."]
    assert not gather._extraction_has_content(ex, "2026-06-04")


def test_figureless_prose_is_not_content():
    # Real length, no digit anywhere → not an extraction (every genuine one states
    # at least one figure).
    ex = ["[8-K filed 2026-06-04] The company discussed its outlook and strategy "
          "in broad qualitative terms without disclosing any specific numbers."]
    assert not gather._extraction_has_content(ex, "2026-06-04")


# ── same-quarter earnings 8-K filed a few days before the 10-Q ────────────────
def test_within_days_window():
    assert gather._within_days("2026-06-01", "2026-06-04", 10)   # 3 days apart
    assert gather._within_days("2026-06-14", "2026-06-04", 10)   # 10 days apart
    assert not gather._within_days("2026-05-20", "2026-06-04", 10)  # 15 days apart
    assert not gather._within_days("", "2026-06-04", 10)         # malformed


def test_earlier_earnings_8k_credits_quarter_via_extra_dates():
    # The 10-Q (2026-06-04) body extracted empty, but the earnings 8-K filed 3 days
    # EARLIER (2026-06-01) carries the figures. Keying only on the 10-Q date misses
    # it (false staleness); passing the 8-K date via extra_dates credits the quarter.
    ex = ["[10-Q filed 2026-06-04] ",
          "[8-K filed 2026-06-01] Record revenue $412.0M; backlog $3,150M; EPS $4.05."]
    assert not gather._extraction_has_content(ex, "2026-06-04")  # 10-Q date alone
    assert gather._extraction_has_content(ex, "2026-06-04", extra_dates=["2026-06-01"])


# ── _extract_doc failure guards (no LLM spend on these paths) ─────────────────
def test_extract_doc_empty_text_returns_blank_stores_nothing():
    db = _FakeDB()
    doc = {"form": "10-Q", "date": "2026-06-04", "url": "u1", "text": ""}
    assert gather._extract_doc(db, "NRDX", doc) == ""
    assert db.inserted == [] and db.updated == []


def test_extract_doc_fetch_error_returns_blank_stores_nothing():
    db = _FakeDB()
    doc = {"form": "10-Q", "date": "2026-06-04", "url": "u1",
           "text": "(could not fetch u1: HTTP 404)"}
    assert gather._extract_doc(db, "NRDX", doc) == ""
    assert db.inserted == [] and db.updated == []


def test_extract_doc_thin_haiku_result_not_stored(monkeypatch_llm):
    # Real text fetched, but the model returns near-nothing → treat as failed, no store.
    monkeypatch_llm(lambda *a, **k: {"text": "  none  ", "usd": 0.0})
    db = _FakeDB()
    doc = {"form": "10-Q", "date": "2026-06-04", "url": "u1",
           "text": "NORVANCE GRID CORP cover page table of contents forward-looking ..."}
    assert gather._extract_doc(db, "NRDX", doc) == ""
    assert db.inserted == [] and db.updated == []


def test_extract_doc_real_content_is_stored(monkeypatch_llm):
    monkeypatch_llm(lambda *a, **k: {
        "text": "- Revenue $412.0M (+50% YoY)\n- Diluted EPS $4.05 vs $2.10\n"
                "- Backlog $3,150M vs $3,020M", "usd": 0.01})
    db = _FakeDB()
    doc = {"form": "8-K", "date": "2026-06-04", "url": "u9",
           "text": "Norvance reports Q1 FY2027 results ... revenue of $412.0 million ..."}
    out = gather._extract_doc(db, "NRDX", doc)
    assert out.startswith("[8-K filed 2026-06-04] ") and "3,150" in out
    assert len(db.inserted) == 1 and db.inserted[0]["source_url"] == "u9"


def test_extract_doc_existing_content_short_circuits_without_llm():
    # A prior good extraction is reused verbatim — no LLM call (would raise here).
    db = _FakeDB([{"id": 1, "source_url": "u1", "doc_type": "10-Q",
                   "doc_date": "2026-06-04",
                   "extracted_text": "- Revenue $412.0M; backlog $3,150M"}])

    def _boom(*a, **k):
        raise AssertionError("llm.call must not run when a good row exists")
    orig = gather.llm.call
    gather.llm.call = _boom
    try:
        doc = {"form": "10-Q", "date": "2026-06-04", "url": "u1", "text": "whatever"}
        out = gather._extract_doc(db, "NRDX", doc)
        assert "3,150" in out and db.inserted == []
    finally:
        gather.llm.call = orig


def test_extract_doc_force_reextracts_and_updates_legacy_empty(monkeypatch_llm):
    # A legacy empty row (from a cemented failed run) must be repairable via force.
    db = _FakeDB([{"id": 5, "source_url": "u1", "doc_type": "10-Q",
                   "doc_date": "2026-06-04", "extracted_text": ""}])
    monkeypatch_llm(lambda *a, **k: {
        "text": "- Revenue $412.0M; backlog $3,150M; EPS $4.05", "usd": 0.01})
    doc = {"form": "10-Q", "date": "2026-06-04", "url": "u1",
           "text": "Norvance Q1 FY2027 ... revenue $412.0 million ..."}
    out = gather._extract_doc(db, "NRDX", doc, force=True)
    assert "3,150" in out
    assert db.updated and db.updated[0][0] == 5  # updated the existing row, not a dup
    assert db.inserted == []


def test_extract_doc_refusal_not_stored(monkeypatch_llm):
    # The model refuses on XBRL-only text → do NOT cache the refusal (exactly what
    # poisoned the cemented rows).
    monkeypatch_llm(lambda *a, **k: {
        "text": "I cannot extract the requested financial figures from this "
                "document — it contains only XBRL taxonomy tags from the 10-Q.",
        "usd": 0.01})
    db = _FakeDB()
    doc = {"form": "10-Q", "date": "2026-06-04", "url": "u1",
           "text": "<xbrl> ... machine-readable tags ... </xbrl>"}
    assert gather._extract_doc(db, "NRDX", doc) == ""
    assert db.inserted == [] and db.updated == []


def test_extract_doc_cached_refusal_self_heals(monkeypatch_llm):
    # SELF-HEAL: a poison refusal row already in the store must NOT be served from
    # cache on an ordinary (non-force) gather — it fails the content gate, so the
    # call falls through, re-extracts fresh text, and OVERWRITES the row in place.
    db = _FakeDB([{"id": 7, "source_url": "u1", "doc_type": "10-Q",
                   "doc_date": "2026-06-04",
                   "extracted_text": "I cannot extract the requested financial "
                   "figures from this 10-Q; the text is only XBRL metadata tags."}])
    monkeypatch_llm(lambda *a, **k: {
        "text": "- Revenue $412.0M; backlog $3,150M; EPS $4.05", "usd": 0.01})
    doc = {"form": "10-Q", "date": "2026-06-04", "url": "u1",
           "text": "Norvance Q1 FY2027 ... revenue $412.0 million ..."}
    out = gather._extract_doc(db, "NRDX", doc)  # NOTE: no force=True
    assert "3,150" in out, "cached refusal must be re-extracted, not served"
    assert db.updated and db.updated[0][0] == 7  # healed in place, not duplicated
    assert db.inserted == []
