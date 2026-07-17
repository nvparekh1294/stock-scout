"""scout/options_ref.py — delayed options-chain snapshot for thesis-expression
analysis (the project design, adopted into briefs 2026-07-12).

Defined-risk structures ONLY are analyzed downstream (long-dated calls to
express Stage-0/1 or binary-risk theses with capped downside; protective puts
around binary events). This module is pure DATA: it fetches a delayed chain
(Alpaca indicative feed — free; latency is irrelevant for LEAPS on multi-year
theses), picks representative LEAP strikes, and renders a dated, sourced
markdown block for the evidence pack. It computes breakevens and premium math
deterministically so the model reasons over real numbers, not vibes.

Hard lines live in the prompts and the project's design rules: sized to lose 100% of premium,
never short volatility, never undefined risk, analysis only.
Read-only market data; contains NO order/execution code.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import requests

from .market_ref import _alpaca_creds

SNAPSHOT_URL = "https://data.alpaca.markets/v1beta1/options/snapshots/{sym}"
OCC = re.compile(r"^(?P<root>[A-Z]+)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


def _parse_occ(symbol: str) -> dict | None:
    m = OCC.match(symbol)
    if not m:
        return None
    y, mo, d = m.group("ymd")[:2], m.group("ymd")[2:4], m.group("ymd")[4:6]
    return {"expiry": f"20{y}-{mo}-{d}", "type": m.group("cp"),
            "strike": int(m.group("strike")) / 1000.0}


def fetch_chain(symbol: str, opt_type: str = "C", min_days: int = 300,
                max_days: int = 900) -> list[dict]:
    """Delayed snapshots for long-dated contracts. Returns [] on any failure
    (honest gap, never a crash)."""
    creds = _alpaca_creds()
    if not creds:
        return []
    key, sec, _ = creds
    lo = (date.today() + timedelta(days=min_days)).isoformat()
    hi = (date.today() + timedelta(days=max_days)).isoformat()
    try:
        r = requests.get(
            SNAPSHOT_URL.format(sym=symbol.upper()),
            params={"feed": "indicative", "type": "call" if opt_type == "C" else "put",
                    "expiration_date_gte": lo, "expiration_date_lte": hi,
                    "limit": 500},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
            timeout=20)
        if r.status_code != 200:
            return []
        out = []
        for occ_sym, snap in (r.json().get("snapshots") or {}).items():
            meta = _parse_occ(occ_sym)
            if not meta or meta["type"] != opt_type:
                continue
            q = snap.get("latestQuote") or {}
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
            if ask <= 0:
                continue
            out.append({**meta, "symbol": occ_sym, "bid": bid, "ask": ask,
                        "mid": round((bid + ask) / 2, 2) if bid else ask,
                        "iv": snap.get("impliedVolatility"),
                        "oi": (snap.get("openInterest") or None)})
        return sorted(out, key=lambda o: (o["expiry"], o["strike"]))
    except Exception:
        return []


def _pick_candidates(chain: list[dict], spot: float) -> list[dict]:
    """Longest expiry; strikes nearest ATM, +10%, +20% (the LEAP-call ladder)."""
    if not chain or not spot:
        return []
    longest = max(o["expiry"] for o in chain)
    at_exp = [o for o in chain if o["expiry"] == longest]
    picks, seen = [], set()
    for target in (spot, spot * 1.10, spot * 1.20):
        best = min(at_exp, key=lambda o: abs(o["strike"] - target))
        if best["symbol"] not in seen:
            seen.add(best["symbol"])
            picks.append(best)
    return picks


def options_snapshot_md(symbol: str, spot: float | None) -> str:
    """Dated markdown block for the evidence pack: a LEAP-call ladder with
    deterministic breakeven/premium math, plus the nearest long-dated protective
    put. Honest gap when the chain is unavailable."""
    today = date.today().isoformat()
    if not spot:
        return ("## Options snapshot\n- NOT FOUND: no spot price available, so "
                "no expression math was computed this run.")
    calls = fetch_chain(symbol, "C")
    lines = [f"## Options snapshot (delayed indicative feed, Alpaca, fetched {today})",
             f"Spot used for the math below: ${spot:,.2f}. Defined-risk analysis "
             f"only: long calls/puts, max loss = 100% of premium, never "
             f"short volatility."]
    picks = _pick_candidates(calls, spot)
    if not picks:
        lines.append("- NOT FOUND: no long-dated call chain returned for "
                     f"{symbol} this run (thinly-optioned name, or feed gap). "
                     "State this plainly in any Expression section.")
        return "\n".join(lines)
    lines += ["", "### LEAP call ladder (longest listed expiry)",
              "| Contract | Expiry | Strike | Mid | Premium % of spot | "
              "Breakeven | BE vs spot | IV |", "|---|---|---|---|---|---|---|---|"]
    for o in picks:
        be = o["strike"] + o["mid"]
        iv = f"{float(o['iv']) * 100:.0f}%" if o.get("iv") else "n/a"
        lines.append(
            f"| {o['symbol']} | {o['expiry']} | ${o['strike']:,.2f} | "
            f"${o['mid']:,.2f} | {o['mid'] / spot * 100:.1f}% | ${be:,.2f} | "
            f"{(be / spot - 1) * 100:+.1f}% | {iv} |")
    puts = fetch_chain(symbol, "P")
    prot = None
    if puts:
        longest = max(o["expiry"] for o in puts)
        near = [o for o in puts if o["expiry"] == longest]
        prot = min(near, key=lambda o: abs(o["strike"] - spot * 0.85))
    if prot:
        lines += ["", "### Nearest long-dated protective put (~85% of spot)",
                  f"- {prot['symbol']} · expiry {prot['expiry']} · strike "
                  f"${prot['strike']:,.2f} · mid ${prot['mid']:,.2f} "
                  f"({prot['mid'] / spot * 100:.1f}% of spot)."]
    lines += ["", "*All quotes delayed/indicative; spreads on thin chains can be "
              "wide — treat mid as an estimate. This is expression data, not an "
              "order instruction.*"]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    from .gather import _alpaca_price_range
    px = _alpaca_price_range(sym)
    spot = px.get("latest_close")
    print(options_snapshot_md(sym, spot))
