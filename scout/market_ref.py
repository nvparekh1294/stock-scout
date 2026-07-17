"""scout/market_ref.py — read-only ticker/company resolution.

resolve_ticker(query) verifies a symbol or company name against two sources
before Scout discusses it:
  1. SEC EDGAR company_tickers.json — authoritative for US-listed SEC filers
     (gives CIK + official name). Required.
  2. Alpaca assets reference endpoint — enrichment (exchange, tradable, status).
     Best-effort; read-only GET, never an order (the project design).

Alpaca keys are read-only and sourced from this app's own .env. Never printed.
"""

from __future__ import annotations

import json
import os
import time

import requests

from .config import REPO_ROOT, edgar_user_agent, load_env

CACHE_DIR = REPO_ROOT / "scout" / "_cache"
TICKERS_CACHE = CACHE_DIR / "company_tickers.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CACHE_TTL = 24 * 3600


def _sec_headers() -> dict:
    # Fails closed (RuntimeError) until edgar.user_agent is set — SEC requires it.
    return {"User-Agent": edgar_user_agent()}


def _company_tickers() -> dict:
    """SEC ticker→CIK map, cached locally for a day."""
    CACHE_DIR.mkdir(exist_ok=True)
    fresh = TICKERS_CACHE.exists() and (time.time() - TICKERS_CACHE.stat().st_mtime) < CACHE_TTL
    if not fresh:
        r = requests.get(TICKERS_URL, headers=_sec_headers(), timeout=20)
        r.raise_for_status()
        TICKERS_CACHE.write_text(r.text)
    return json.loads(TICKERS_CACHE.read_text())


def _edgar_lookup(query: str) -> dict | None:
    """Exact ticker match first, then a name (substring) match."""
    data = _company_tickers()
    q = query.strip()
    qu = q.upper()
    # exact ticker
    for row in data.values():
        if row.get("ticker", "").upper() == qu:
            return {"ticker": row["ticker"], "cik": int(row["cik_str"]),
                    "title": row["title"], "match": "ticker"}
    # name substring (return the first/shortest reasonable match)
    ql = q.lower()
    name_hits = [row for row in data.values() if ql in row.get("title", "").lower()]
    if name_hits:
        best = min(name_hits, key=lambda r: len(r["title"]))
        return {"ticker": best["ticker"], "cik": int(best["cik_str"]),
                "title": best["title"], "match": "name",
                "other_matches": [h["ticker"] for h in name_hits[:5] if h is not best]}
    return None


def _alpaca_creds() -> tuple[str, str, str] | None:
    """Read-only Alpaca market-data keys from this app's own environment (.env).
    Returns None when unset, so callers degrade gracefully (EDGAR still works)."""
    load_env()
    key = os.getenv("ALPACA_API_KEY", "").strip()
    sec = os.getenv("ALPACA_SECRET_KEY", "").strip() or os.getenv("ALPACA_API_SECRET", "").strip()
    base = os.getenv("ALPACA_BASE_URL", "").strip()
    if not (key and sec):
        return None
    return key, sec, (base or "https://paper-api.alpaca.markets/v2")


def _alpaca_asset(ticker: str) -> dict | None:
    creds = _alpaca_creds()
    if not creds:
        return None
    key, sec, base = creds
    try:
        r = requests.get(f"{base}/assets/{ticker}",
                         headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
                         timeout=15)
        if r.status_code != 200:
            return None
        a = r.json()
        return {"symbol": a.get("symbol"), "name": a.get("name"),
                "exchange": a.get("exchange"), "tradable": a.get("tradable"),
                "status": a.get("status"), "class": a.get("class")}
    except Exception:
        return None


def resolve_ticker(query: str) -> dict:
    """Return {resolved, ticker, name, cik, exchange, sources, note, ...}.

    Identity flags (stale-queue-ticker fix). Seven modules consume
    resolve_ticker and none used to check whether the symbol was still a
    real, current SEC filer / a still-trading security. Two failure shapes drove
    this: PHTN fell out of SEC's registry and the market-data source now serves
    that ticker as a DIFFERENT active ETF — a silent reassignment; ARJT (an
    acquired issuer) is gone from EDGAR and the market-data source reports it
    inactive/untradable, yet resolve still said resolved=True and downstream
    fetched n/a forever. The
    new keys let every caller SEE identity trouble; `resolved` semantics are
    UNCHANGED (False only when BOTH sources miss):
      - sec_filer : True iff EDGAR matched (authoritative US-filer registry).
      - active    : True/False from Alpaca (status=="active" AND tradable);
                    None when there is no Alpaca data at all.
      - identity_warning : None, or a plain-English caution (see below)."""
    edgar = None
    try:
        edgar = _edgar_lookup(query)
    except Exception as e:
        edgar = None
        edgar_err = str(e)
    ticker = (edgar or {}).get("ticker") or query.strip().upper()
    alpaca = _alpaca_asset(ticker)

    if not edgar and not alpaca:
        return {"resolved": False, "query": query,
                "sec_filer": False, "active": None, "identity_warning": None,
                "note": "Not found in SEC EDGAR (company_tickers) and not a "
                        "listed Alpaca asset. Cannot confirm this is a real "
                        "listed security — treat as unresolved."}
    sources = []
    if edgar:
        sources.append("SEC EDGAR company_tickers")
    if alpaca:
        sources.append("Alpaca assets")

    # Identity flags. sec_filer: did the authoritative EDGAR registry match?
    # active: does Alpaca still list it as a currently-trading security? (None
    # when Alpaca returned nothing at all — absence of data, not a "no".)
    sec_filer = edgar is not None
    active = None
    if alpaca is not None:
        active = bool(alpaca.get("status") == "active" and alpaca.get("tradable"))

    # Build the plain-English caution. Order matters: inactive/delisted first
    # (the hardest fact — it does not trade), then the reassignment caution.
    warnings = []
    if active is False:
        warnings.append("inactive/delisted on exchange — no longer trades "
                        f"(status={alpaca.get('status')})")
    if not sec_filer and alpaca is not None:
        warnings.append("not in SEC EDGAR's ticker registry — ticker may have "
                        "been reassigned to a different security (Alpaca lists: "
                        f"{alpaca.get('name')}); verify identity before use")
    identity_warning = "; ".join(warnings) if warnings else None

    return {
        "resolved": True,
        "ticker": (alpaca or {}).get("symbol") or (edgar or {}).get("ticker"),
        "name": (edgar or {}).get("title") or (alpaca or {}).get("name"),
        "cik": (edgar or {}).get("cik"),
        "exchange": (alpaca or {}).get("exchange"),
        "tradable": (alpaca or {}).get("tradable"),
        "status": (alpaca or {}).get("status"),
        "match": (edgar or {}).get("match"),
        "other_name_matches": (edgar or {}).get("other_matches"),
        "sources": sources,
        "sec_filer": sec_filer,
        "active": active,
        "alpaca_name": (alpaca or {}).get("name"),
        "identity_warning": identity_warning,
    }


if __name__ == "__main__":
    import sys
    print(json.dumps(resolve_ticker(sys.argv[1] if len(sys.argv) > 1 else "AAPL"), indent=2))
