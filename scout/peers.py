"""scout/peers.py — peer auto-discovery + peer-metrics population (Task 1, built
2026-07-13). Makes the comps table real: it was rendering an empty NOT-FOUND
scaffold because nothing selected peer symbols or populated peer_metrics.

Design (per the follow-on brief, honoring underwriter.md:59):
  - Peer SELECTION is a research direction and MAY use the model. The primary
    signal is DETERMINISTIC: the subject's SEC SIC industry code → same-SIC
    listed companies (EDGAR browse-edgar) → a cheap Haiku call picks the 3–5
    most economically comparable, with a one-line rationale each. The model only
    PROPOSES the peer set; it never supplies a metric.
  - Peer METRICS are 100% deterministic (scout/fundamentals.py): fundamentals
    from EDGAR company-facts XBRL, multiples from an Alpaca price + a Nasdaq
    forward EPS. Every cell NOT FOUND when not derivable — never approximated.
  - Both the peer set and the metrics are CACHED (system_flags + peer_metrics)
    with a ~7-day TTL, so re-gathers reuse them and a peer is priced once.

Known limitation (documented, honest): EDGAR browse-edgar matches an EXACT
4-digit SIC. Real-economy peers can sit in sibling SIC codes (e.g. NRDX is SIC
1700 Construction–Special Trade, while Quanta is 1731 Electrical Work), so the
exact-SIC shortlist is a starting universe, not the whole comp set. The Haiku
step picks the best comparables that ARE in it. No order/execution code.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import requests

from . import fundamentals, llm
from .comps import upsert_peer_metrics
from .estimates import forward_eps
from .market_ref import _company_tickers, _sec_headers

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
BROWSE = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&SIC={sic}"
          "&type=10-K&dateb=&owner=include&count=100&output=atom")
_PEERS_FLAG = "peers:{sym}"
TTL_DAYS = 7


# ── deterministic SIC shortlist ────────────────────────────────────────────
def subject_sic(cik: int) -> tuple[str | None, str | None]:
    try:
        r = requests.get(SUBMISSIONS.format(cik=cik), headers=_sec_headers(), timeout=20)
        r.raise_for_status()
        j = r.json()
        return (str(j.get("sic") or "") or None, j.get("sicDescription") or None)
    except Exception:
        return None, None


def sic_shortlist(sic: str, exclude_cik: int, limit: int = 40) -> list[dict]:
    """Same-SIC LISTED companies (browse-edgar ∩ company_tickers), as
    [{symbol, cik, name}]. Only tickered filers survive — comps need a price."""
    if not sic:
        return []
    try:
        r = requests.get(BROWSE.format(sic=sic), headers=_sec_headers(), timeout=25)
        if r.status_code != 200:
            return []
        ciks = [int(c) for c in re.findall(r"<cik>(\d+)</cik>", r.text)]
    except Exception:
        return []
    try:
        ct = _company_tickers()
    except Exception:
        return []
    cik2t = {int(v["cik_str"]): (v["ticker"], v["title"]) for v in ct.values()}
    out = []
    for c in ciks:
        if c == exclude_cik or c not in cik2t:
            continue
        tick, title = cik2t[c]
        out.append({"symbol": tick.upper(), "cik": c, "name": title})
        if len(out) >= limit:
            break
    return out


# ── model-proposed peer selection (Haiku, logged) ──────────────────────────
def _parse_peer_json(text: str, valid: set[str]) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol", "")).upper().strip()
        if sym in valid and sym not in {o["symbol"] for o in out}:
            out.append({"symbol": sym, "rationale": str(item.get("rationale", ""))[:200]})
    return out[:5]


def select_peers(db, symbol: str, name: str, sic_desc: str,
                 shortlist: list[dict], monthly_budget: float | None = None) -> tuple[list[dict], float]:
    """Haiku picks the 3–5 most economically comparable names FROM the
    deterministic shortlist. Returns (peers, cost_usd). Model proposes only —
    each peer's metrics come from filings later. Falls back to the first names
    (flagged) if the model can't be called or returns nothing."""
    valid = {c["symbol"] for c in shortlist}
    if not shortlist:
        return [], 0.0
    listing = "\n".join(f"- {c['symbol']}: {c['name']}" for c in shortlist)
    prompt = (
        f"You are selecting equity comparables for {symbol} ({name}), whose SEC "
        f"industry classification is \"{sic_desc}\". From the candidate list "
        f"below (all same-SIC listed filers), choose the 3 to 5 MOST economically "
        f"comparable — similar business model, end-markets, and scale where you "
        f"can tell from the name. Exclude shells, SPACs, and obviously unrelated "
        f"names. Return ONLY a JSON array of objects "
        f"{{\"symbol\": \"TICK\", \"rationale\": \"one line\"}}. No prose.\n\n"
        f"Candidates:\n{listing}")
    cost = 0.0
    try:
        r = llm.call(f"{symbol}-peer-select", "haiku",
                     [{"role": "user", "content": prompt}], max_tokens=600,
                     db=db, monthly_budget=monthly_budget)
        cost = r["usd"]
        peers = _parse_peer_json(r["text"], valid)
        if peers:
            by_sym = {c["symbol"]: c for c in shortlist}
            for p in peers:
                p["cik"] = by_sym[p["symbol"]]["cik"]
                p["name"] = by_sym[p["symbol"]]["name"]
            return peers, cost
    except Exception:
        pass
    # honest fallback — same-SIC, but flagged as an unranked default
    fallback = [{"symbol": c["symbol"], "cik": c["cik"], "name": c["name"],
                 "rationale": f"same SIC ({sic_desc}); model ranking unavailable"}
                for c in shortlist[:4]]
    return fallback, cost


# ── cache (system_flags, TTL) ──────────────────────────────────────────────
def _flag(sym: str) -> str:
    return _PEERS_FLAG.format(sym=sym.upper())


def _read_cache(db, symbol: str) -> dict | None:
    row = db.select_one("system_flags", {"flag": _flag(symbol)})
    if not row or not row.get("value"):
        return None
    try:
        payload = json.loads(row["value"])
    except Exception:
        return None
    chosen = payload.get("chosen_at")
    if chosen:
        try:
            if date.fromisoformat(chosen) < date.today() - timedelta(days=TTL_DAYS):
                return None  # stale
        except Exception:
            pass
    return payload


def _write_cache(db, symbol: str, payload: dict) -> None:
    # system_flags keys on `flag` (no serial id), so delete+insert is the
    # portable upsert on both backends (JSON update() keys on id; Postgres
    # update() uses WHERE id — neither fits a flag-keyed row).
    payload = dict(payload, chosen_at=date.today().isoformat())
    db.delete("system_flags", {"flag": _flag(symbol)})
    db.insert("system_flags", {"flag": _flag(symbol), "value": json.dumps(payload)})


def discover_peers(db, symbol: str, cik: int, name: str,
                   monthly_budget: float | None = None,
                   force: bool = False) -> dict:
    """Return {peers:[{symbol,cik,name,rationale}], sic, sic_desc, source,
    cost_usd}. Cached per symbol with a TTL; only spends the Haiku call when the
    cache is cold or `force`."""
    symbol = symbol.upper()
    if not force:
        cached = _read_cache(db, symbol)
        if cached and cached.get("peers"):
            return dict(cached, cost_usd=0.0, cached=True)
    sic, sic_desc = subject_sic(cik)
    shortlist = sic_shortlist(sic or "", cik)
    peers, cost = select_peers(db, symbol, name, sic_desc or "", shortlist,
                               monthly_budget=monthly_budget)
    payload = {"peers": peers, "sic": sic, "sic_desc": sic_desc,
               "source": "EDGAR SIC shortlist + Haiku selection",
               "shortlist_n": len(shortlist)}
    if peers:
        _write_cache(db, symbol, payload)
    return dict(payload, cost_usd=cost, cached=False)


# ── peer-metrics population (deterministic) ────────────────────────────────
def _needs_refresh(db, sym: str, as_of: str) -> bool:
    row = db.select_one("peer_metrics", {"symbol": sym.upper()})
    if not row:
        return True
    asof = row.get("asof")
    if not asof:
        return True
    try:
        return date.fromisoformat(str(asof)[:10]) < date.fromisoformat(as_of) - timedelta(days=TTL_DAYS)
    except Exception:
        return False


def populate_metrics(db, entries: list[dict], as_of: str) -> list[str]:
    """entries: [{symbol, cik}] (subject + peers). For each with a stale/missing
    peer_metrics row, derive metrics from EDGAR + Alpaca + Nasdaq and upsert.
    Deterministic (NO LLM). Returns the symbols actually (re)priced.

    Every entry that reaches the upsert here has just had its fundamentals
    freshly refetched and its whole metrics row rederived from scratch — a full
    recompute, not a partial patch. So we tell upsert_peer_metrics that: any
    _STORE_KEYS cell peer_metric_row didn't come back with (e.g. D/E when the
    latest 10-K dropped the debt tag) is genuinely underivable today and must
    be NULLed, not left showing whatever value a prior week's row happened to
    carry (that would date-launder a stale multiple under today's `as_of`)."""
    priced = []
    for e in entries:
        sym, cik = e["symbol"].upper(), e.get("cik")
        if not cik or not _needs_refresh(db, sym, as_of):
            continue
        fund = fundamentals.company_facts_metrics(cik)
        if not fund:
            continue
        price = fundamentals.price_latest(sym)
        fe = forward_eps(sym, as_of)
        fwd = fe.get("fwd_eps") if fe else None
        row = fundamentals.peer_metric_row(cik, price, fwd, fund=fund)
        if not row:
            continue
        metrics = {k: v for k, v in row.items()
                   if k not in ("source_url", "doc_date") and v is not None}
        upsert_peer_metrics(db, sym, metrics, source_url=row.get("source_url"),
                            doc_date=row.get("doc_date"), asof=as_of,
                            full_recompute=True)
        priced.append(sym)
    return priced


def ensure_peers(db, symbol: str, cik: int, name: str, as_of: str | None = None,
                 monthly_budget: float | None = None) -> dict:
    """One call for the gatherer: discover peers (cached) and populate the
    peer_metrics store for the subject + peers (cached). Returns
    {peer_symbols, peers, sic, sic_desc, priced, cost_usd}."""
    from datetime import date as _date
    as_of = as_of or _date.today().isoformat()
    disc = discover_peers(db, symbol, cik, name, monthly_budget=monthly_budget)
    peers = disc.get("peers", [])
    entries = [{"symbol": symbol.upper(), "cik": cik}] + \
              [{"symbol": p["symbol"], "cik": p.get("cik")} for p in peers]
    priced = populate_metrics(db, entries, as_of)
    return {"peer_symbols": [p["symbol"] for p in peers], "peers": peers,
            "sic": disc.get("sic"), "sic_desc": disc.get("sic_desc"),
            "priced": priced, "cost_usd": disc.get("cost_usd", 0.0)}


if __name__ == "__main__":
    import sys
    from .db import Database
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    from .market_ref import resolve_ticker
    res = resolve_ticker(sym)
    db = Database()
    db.apply_schema()
    out = ensure_peers(db, res["ticker"], res["cik"], res.get("name"))
    print(json.dumps(out, indent=1, default=str))
