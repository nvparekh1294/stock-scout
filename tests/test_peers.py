"""tests/test_peers.py — unit + integration cases for peer auto-discovery,
forward estimates, and the fresh-brief render.

    python -m pytest tests/test_peers.py

No LLM spend and no network (requests + llm.call are monkeypatched with fixtures).

Covers:
  - fundamentals.company_facts_metrics / peer_metric_row derive the right margins
    + multiples from a fixture company-facts blob, and gate loss-making multiples
    to NOT FOUND;
  - estimates.forward_eps picks the nearest FUTURE fiscal-year consensus EPS from
    a fixture payload and fails open;
  - peers.sic_shortlist parses the browse-edgar atom ∩ listed tickers;
  - peers.select_peers turns a Haiku JSON reply into a validated peer set and
    falls back honestly; discover_peers caches within TTL (one Haiku call);
  - INTEGRATION: a populated peer_metrics store yields a POPULATED comps table and
    key-number cards with forward P/E filled — deterministically, no LLM.

All tickers, company names, CIKs, and consensus figures below are invented.
"""

from __future__ import annotations

from scout import comps, estimates, fundamentals, gather, peers, visuals
from scout.db import Database


# ── fixtures ────────────────────────────────────────────────────────────────
def _dur(start, end, val, fp="FY", form="10-K"):
    return {"start": start, "end": end, "val": val, "fp": fp, "form": form}


def _inst(end, val, fp="FY", form="10-K"):
    return {"end": end, "val": val, "fp": fp, "form": form}


def _facts_fixture(rev_latest=1200, ni=150, da=50, debt=100):
    gaap = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            _dur("2024-01-01", "2024-12-31", 1000),
            _dur("2025-01-01", "2025-12-31", rev_latest)]}},
        "GrossProfit": {"units": {"USD": [_dur("2025-01-01", "2025-12-31", 300)]}},
        "OperatingIncomeLoss": {"units": {"USD": [_dur("2025-01-01", "2025-12-31", 200)]}},
        "NetIncomeLoss": {"units": {"USD": [_dur("2025-01-01", "2025-12-31", ni)]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
            _dur("2025-01-01", "2025-12-31", 180)]}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
            _dur("2025-01-01", "2025-12-31", 30)]}},
        "StockholdersEquity": {"units": {"USD": [_inst("2025-12-31", 500)]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [_inst("2025-12-31", 40)]}},
    }
    if da is not None:
        gaap["DepreciationDepletionAndAmortization"] = {"units": {"USD": [
            _dur("2025-01-01", "2025-12-31", da)]}}
    if debt is not None:
        gaap["LongTermDebtNoncurrent"] = {"units": {"USD": [_inst("2025-12-31", debt)]}}
    dei = {"EntityCommonStockSharesOutstanding": {"units": {"shares": [_inst("2025-12-31", 100)]}}}
    return {"entityName": "FIXTURE CO", "facts": {"us-gaap": gaap, "dei": dei}}


# ── fundamentals ────────────────────────────────────────────────────────────
def test_fundamentals_margins_and_multiples(monkeypatch):
    monkeypatch.setattr(fundamentals, "_facts", lambda cik: _facts_fixture())
    m = fundamentals.company_facts_metrics(1)
    assert abs(m["rev_growth"] - 0.2) < 1e-9, m["rev_growth"]
    assert abs(m["gm"] - 0.25) < 1e-9
    assert abs(m["om"] - (200 / 1200)) < 1e-9
    assert abs(m["ebitda"] - 250) < 1e-9              # 200 op + 50 D&A
    assert abs(m["fcf"] - 150) < 1e-9                 # 180 ocf - 30 capex
    assert abs(m["de"] - 0.2) < 1e-9                  # 100 / 500
    row = fundamentals.peer_metric_row(1, price=20.0, fwd_eps=2.0, fund=m)
    assert abs(row["ps"] - (2000 / 1200)) < 1e-9      # mcap 20*100 / rev
    assert abs(row["fwd_pe"] - 10.0) < 1e-9           # 20 / 2
    assert abs(row["ev_ebitda"] - (2060 / 250)) < 1e-9  # (2000+100-40)/250


def test_fundamentals_missing_debt_is_not_found(monkeypatch):
    monkeypatch.setattr(fundamentals, "_facts", lambda cik: _facts_fixture(debt=None))
    m = fundamentals.company_facts_metrics(1)
    assert m["de"] is None, "no debt tag → D/E NOT FOUND, never asserted zero"
    row = fundamentals.peer_metric_row(1, 20.0, 2.0, fund=m)
    assert row["ev_ebitda"] is None, "EV/EBITDA needs debt+cash → NOT FOUND"


def test_fundamentals_lossmaker_multiples_not_found(monkeypatch):
    # negative EPS / EBITDA must not produce a misleading negative multiple
    monkeypatch.setattr(fundamentals, "_facts", lambda cik: _facts_fixture(ni=-50))
    m = fundamentals.company_facts_metrics(1)
    row = fundamentals.peer_metric_row(1, 20.0, fwd_eps=-1.0, fund=m)
    assert row["fwd_pe"] is None, "negative fwd EPS → fwd P/E NOT FOUND"


# ── estimates ───────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


def test_forward_eps_picks_nearest_future_fy(monkeypatch):
    payload = {"data": {"yearlyForecast": {"rows": [
        {"fiscalEnd": "Jan 2026", "consensusEPSForecast": 7.0, "noOfEstimates": 3},   # past
        {"fiscalEnd": "Jan 2027", "consensusEPSForecast": 9.4, "noOfEstimates": 2},   # nearest future
        {"fiscalEnd": "Jan 2028", "consensusEPSForecast": 11.2, "noOfEstimates": 2}]}}}
    monkeypatch.setattr(estimates.requests, "get", lambda *a, **k: _Resp(payload))
    fe = estimates.forward_eps("NRDX", as_of="2026-07-13")
    assert fe["fwd_eps"] == 9.4 and fe["fy_end"] == "Jan 2027", fe
    assert fe["source"].startswith("nasdaq"), fe


def test_forward_eps_fails_open(monkeypatch):
    monkeypatch.setattr(estimates.requests, "get", lambda *a, **k: _Resp({}, status=403))
    assert estimates.forward_eps("NRDX") is None


# ── peer selection ──────────────────────────────────────────────────────────
_ATOM = """<feed><entry><content><company-info>
<cik>0000700100</cik><sic>7372</sic></company-info></content></entry>
<entry><content><company-info><cik>0000700200</cik><sic>7372</sic>
</company-info></content></entry>
<entry><content><company-info><cik>0000700300</cik><sic>7372</sic>
</company-info></content></entry></feed>"""


class _Atom:
    def __init__(self, text):
        self.text, self.status_code = text, 200


def test_sic_shortlist_parses_atom_and_intersects_tickers(monkeypatch):
    # Public build fails EDGAR closed until a UA is configured; simulate a
    # configured instance (the fail-closed path is covered by test_failclose).
    monkeypatch.setattr(peers, "_sec_headers",
                        lambda: {"User-Agent": "Test Runner research test@example.com"})
    monkeypatch.setattr(peers.requests, "get", lambda *a, **k: _Atom(_ATOM))
    monkeypatch.setattr(peers, "_company_tickers", lambda: {
        "0": {"cik_str": 700100, "ticker": "NRDX", "title": "NORVANCE GRID CORP"},
        "1": {"cik_str": 700200, "ticker": "VANM", "title": "Vanam Systems"},
        "2": {"cik_str": 700300, "ticker": "KOSM", "title": "Kosmic Software"}})
    sl = peers.sic_shortlist("7372", exclude_cik=700100)
    syms = {c["symbol"] for c in sl}
    assert syms == {"VANM", "KOSM"}, syms  # subject excluded, both listed kept


def test_select_peers_parses_haiku_json(monkeypatch):
    shortlist = [{"symbol": "VANM", "cik": 1, "name": "Vanam Systems"},
                 {"symbol": "KOSM", "cik": 2, "name": "Kosmic Software"},
                 {"symbol": "ZZTOP", "cik": 3, "name": "Shell Co"}]

    def fake_call(task, tier, messages, max_tokens, **kw):
        return {"text": '[{"symbol":"VANM","rationale":"infrastructure software"},'
                        '{"symbol":"KOSM","rationale":"building analytics"}]',
                "usd": 0.001, "stop_reason": "end_turn"}
    monkeypatch.setattr(peers.llm, "call", fake_call)
    db = Database(db_url="")
    peers_out, cost = peers.select_peers(db, "NRDX", "Norvance", "Software",
                                         shortlist)
    assert [p["symbol"] for p in peers_out] == ["VANM", "KOSM"], peers_out
    assert peers_out[0]["cik"] == 1 and peers_out[0]["rationale"]
    assert cost == 0.001
    db.close()


def test_discover_peers_caches_within_ttl(monkeypatch):
    calls = {"n": 0}

    def fake_call(task, tier, messages, max_tokens, **kw):
        calls["n"] += 1
        return {"text": '[{"symbol":"VANM","rationale":"x"}]', "usd": 0.002,
                "stop_reason": "end_turn"}
    monkeypatch.setattr(peers, "subject_sic", lambda cik: ("7372", "Software"))
    monkeypatch.setattr(peers, "sic_shortlist",
                        lambda sic, cik, limit=40: [{"symbol": "VANM", "cik": 700200,
                                                     "name": "Vanam Systems"}])
    monkeypatch.setattr(peers.llm, "call", fake_call)
    db = Database(db_url="")
    db.apply_schema()
    db.delete("system_flags", {"flag": "peers:NRDX"})
    d1 = peers.discover_peers(db, "NRDX", 700100, "Norvance")
    d2 = peers.discover_peers(db, "NRDX", 700100, "Norvance")  # cache hit
    assert calls["n"] == 1, "second discover must hit cache, not call Haiku again"
    assert d1["peers"] and d2["peers"] and d2["cost_usd"] == 0.0
    db.delete("system_flags", {"flag": "peers:NRDX"})
    db.close()


# ── integration: fresh-brief render, no LLM ─────────────────────────────────
def test_integration_populated_comps_and_cards(monkeypatch):
    db = Database(db_url="")
    db.apply_schema()
    for s in ("NRDX", "VANM", "KOSM"):
        db.delete("peer_metrics", {"symbol": s})
    # subject + peers priced deterministically from the fixture (no network)
    monkeypatch.setattr(fundamentals, "_facts", lambda cik: _facts_fixture())
    monkeypatch.setattr(fundamentals, "price_latest", lambda sym: 20.0)
    monkeypatch.setattr(estimates.requests, "get", lambda *a, **k: _Resp(
        {"data": {"yearlyForecast": {"rows": [
            {"fiscalEnd": "Jan 2027", "consensusEPSForecast": 2.0,
             "noOfEstimates": 2}]}}}))
    priced = peers.populate_metrics(
        db, [{"symbol": "NRDX", "cik": 700100}, {"symbol": "VANM", "cik": 700200},
             {"symbol": "KOSM", "cik": 700300}], as_of="2026-07-13")
    assert set(priced) == {"NRDX", "VANM", "KOSM"}, priced

    # comps table renders POPULATED rows (not a NOT-FOUND scaffold)
    table = comps.comps_table_md("NRDX", db, peer_symbols=["VANM", "KOSM"])
    assert "no peers are cached" not in table, "peers ARE cached → real table"
    assert "**VANM**" in table and "**KOSM**" in table
    assert "10.0×" in table, "forward P/E must be populated (20/2)"

    # key-number cards: forward P/E filled from the cached peer row, no LLM
    monkeypatch.setattr(visuals, "weekly_closes",
                        lambda sym: {"closes": [("2026-07-06", 20.0)], "high": 22.0,
                                     "low": 15.0, "latest": 20.0, "asof": "2026-07-10"})
    # consensus still shows NOT FOUND at this point (comps store carries no
    # consensus cell), so _live_enrich falls through to gather._consensus_snapshot
    # — patch its requests.get too so the fall-through runs without leaving the
    # process. Fixture a deterministic consensus.
    monkeypatch.setattr(gather.requests, "get", lambda *a, **k: _Resp(
        {"data": {"consensusOverview": {
            "priceTarget": 25.0, "lowPriceTarget": 20.0, "highPriceTarget": 30.0,
            "buy": 3, "hold": 1, "sell": 0}}}))
    metrics, _ = visuals.build_metrics("NRDX", brief_text="", db=db)
    assert metrics["fwd_pe"] == 10.0, metrics.get("fwd_pe")
    assert metrics.get("fwd_pe_src") == "comps store"
    assert metrics.get("consensus") == "PT $25.0", metrics.get("consensus")
    cards = visuals.key_number_cards(metrics)
    assert "10.0×" in cards, "fwd P/E card must show the figure, not NOT FOUND"
    for s in ("NRDX", "VANM", "KOSM"):
        db.delete("peer_metrics", {"symbol": s})
    db.close()
