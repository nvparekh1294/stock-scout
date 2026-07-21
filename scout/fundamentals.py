"""scout/fundamentals.py — deterministic company fundamentals from SEC EDGAR
XBRL company-facts, plus valuation multiples from a live Alpaca price
(Task 1 peer-metrics population, built 2026-07-13).

NO LLM. Every number here traces to a primary source (EDGAR company-facts, URL
below) or an Alpaca price quote — never model memory (the project design, the
no-facts-beyond-the-pack peer rule). A metric that isn't derivable from the
filing stays None → the comps renderer shows NOT FOUND for it. Nothing is ever
approximated silently.

company_facts_metrics(cik) → the raw + margin fundamentals (latest fiscal year).
peer_metric_row(cik, price, fwd_eps) → a dict aligned to the peer_metrics store
    columns (rev_growth, gm, om, ebitda_margin, net_income, fcf, de, ps, fwd_pe,
    ev_ebitda) with source_url + doc_date, cells None where NOT derivable.

Contains NO order/execution code. Read-only GETs only.
"""

from __future__ import annotations

from datetime import date

import requests

from .market_ref import _alpaca_creds, _sec_headers

COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
DATA_BASE = "https://data.alpaca.markets/v2"

# Concept fallbacks — companies tag the same line item under different us-gaap
# names; we try each in order and use the first that resolves.
_REVENUE = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet", "SalesRevenueGoodsNet")
_DEP_AMORT = ("DepreciationDepletionAndAmortization",
              "DepreciationAmortizationAndAccretionNet",
              "DepreciationAndAmortization", "Depreciation")
_CASH = ("CashAndCashEquivalentsAtCarryingValue",
         "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")
_SHARES_DEI = ("EntityCommonStockSharesOutstanding",)
_SHARES_GAAP = ("CommonStockSharesOutstanding",
                "WeightedAverageNumberOfDilutedSharesOutstanding")
# Reported diluted EPS for the fiscal year (a filed line item — not computed —
# so trailing P/E traces to the filing, never to net_income ÷ shares guesswork).
_EPS_DILUTED = ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted")


def _facts(cik: int) -> dict | None:
    try:
        r = requests.get(COMPANYFACTS.format(cik=cik), headers=_sec_headers(), timeout=30)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _concept(facts: dict, name: str, ns: str = "us-gaap") -> list | None:
    d = ((facts.get("facts") or {}).get(ns) or {}).get(name)
    if not d:
        return None
    units = d.get("units") or {}
    # take the first unit (USD or shares) — a concept has exactly one relevant unit
    for u in ("USD", "shares"):
        if u in units:
            return units[u]
    return next(iter(units.values()), None)


def _first_concept(facts: dict, names, ns: str = "us-gaap"):
    for n in names:
        arr = _concept(facts, n, ns)
        if arr:
            return arr
    return None


def _annual_fy(arr: list) -> list:
    """Full-fiscal-year duration points (fp=FY, ~365-day span), deduped by end
    date, oldest→newest. Used for income-statement / cash-flow concepts."""
    if not arr:
        return []
    seen, out = {}, []
    for x in arr:
        if x.get("fp") != "FY" or not x.get("start") or not x.get("end"):
            continue
        try:
            span = (date.fromisoformat(x["end"]) - date.fromisoformat(x["start"])).days
        except Exception:
            continue
        if not (350 <= span <= 380):
            continue
        seen[x["end"]] = x  # later wins for a duplicated end
    return [seen[k] for k in sorted(seen)]


def _latest_instant(arr: list):
    """Latest balance-sheet (instant) value by end date. Used for equity, debt,
    cash, shares."""
    if not arr:
        return None
    pts = [x for x in arr if x.get("end") and x.get("val") is not None]
    if not pts:
        return None
    return max(pts, key=lambda x: x["end"])


def _sum_present(facts: dict, names) -> float | None:
    """Sum the latest-instant values of the given concepts that ARE present.
    Returns None if none are present (so a genuinely-absent line stays NOT
    FOUND — we never assert zero)."""
    total, found = 0.0, False
    for n in names:
        pt = _latest_instant(_concept(facts, n))
        if pt is not None:
            total += float(pt["val"])
            found = True
    return total if found else None


def company_facts_metrics(cik: int) -> dict | None:
    """Latest-fiscal-year fundamentals + margins from EDGAR company-facts.
    Every field is None when the underlying tag is absent. Returns None only if
    company-facts itself can't be fetched."""
    facts = _facts(cik)
    if not facts:
        return None
    out: dict = {"source_url": COMPANYFACTS.format(cik=cik),
                 "entity": facts.get("entityName")}

    rev_pts = _annual_fy(_first_concept(facts, _REVENUE) or [])
    rev = rev_pts[-1]["val"] if rev_pts else None
    rev_prior = rev_pts[-2]["val"] if len(rev_pts) >= 2 else None
    fy_end = rev_pts[-1]["end"] if rev_pts else None
    out["revenue"] = rev
    out["fy_end"] = fy_end
    out["rev_growth"] = ((rev / rev_prior - 1) if rev and rev_prior else None)

    def latest_fy_val(names):
        pts = _annual_fy(_first_concept(facts, names) or [])
        # prefer the FY matching the revenue FY end so all cells are same-period
        if fy_end:
            for p in pts:
                if p["end"] == fy_end:
                    return p["val"]
        return pts[-1]["val"] if pts else None

    gp = latest_fy_val(("GrossProfit",))
    oi = latest_fy_val(("OperatingIncomeLoss",))
    ni = latest_fy_val(("NetIncomeLoss", "ProfitLoss"))
    da = latest_fy_val(_DEP_AMORT)
    ocf = latest_fy_val(("NetCashProvidedByUsedInOperatingActivities",
                         "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"))
    capex = latest_fy_val(("PaymentsToAcquirePropertyPlantAndEquipment",
                           "PaymentsToAcquireProductiveAssets"))

    out["gross_profit"] = gp
    out["operating_income"] = oi
    out["net_income"] = ni
    # Reported FY diluted EPS (same fiscal period as revenue when present).
    out["eps_diluted"] = latest_fy_val(_EPS_DILUTED)
    out["dep_amort"] = da
    out["gm"] = (gp / rev if gp is not None and rev else None)
    out["om"] = (oi / rev if oi is not None and rev else None)
    ebitda = (oi + da) if (oi is not None and da is not None) else None
    out["ebitda"] = ebitda
    out["ebitda_margin"] = (ebitda / rev if ebitda is not None and rev else None)
    out["fcf"] = (ocf - capex if ocf is not None and capex is not None else None)

    equity_pt = _latest_instant(_concept(facts, "StockholdersEquity"))
    equity = float(equity_pt["val"]) if equity_pt else None
    debt = _sum_present(facts, ("LongTermDebtNoncurrent", "LongTermDebtCurrent")) \
        or _sum_present(facts, ("LongTermDebt",)) \
        or _sum_present(facts, ("LongTermDebtAndCapitalLeaseObligations",
                                "LongTermDebtAndCapitalLeaseObligationsCurrent")) \
        or _sum_present(facts, ("DebtCurrent", "DebtNoncurrent"))
    out["equity"] = equity
    out["debt"] = debt
    out["de"] = (debt / equity if debt is not None and equity else None)

    cash_pt = _latest_instant(_first_concept(facts, _CASH))
    out["cash"] = float(cash_pt["val"]) if cash_pt else None

    sh_pt = _latest_instant(_first_concept(facts, _SHARES_DEI, ns="dei")) \
        or _latest_instant(_first_concept(facts, _SHARES_GAAP))
    out["shares"] = float(sh_pt["val"]) if sh_pt else None
    return out


def price_latest(symbol: str) -> float | None:
    """Latest daily close from Alpaca IEX (read-only). None on any failure."""
    creds = _alpaca_creds()
    if not creds:
        return None
    key, sec, _ = creds
    try:
        from datetime import timedelta
        start = (date.today() - timedelta(days=12)).isoformat()
        r = requests.get(f"{DATA_BASE}/stocks/{symbol}/bars",
                         params={"timeframe": "1Day", "start": start, "limit": 10,
                                 "feed": "iex", "adjustment": "split"},
                         headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
                         timeout=20)
        if r.status_code != 200:
            return None
        bars = r.json().get("bars", [])
        return round(bars[-1]["c"], 2) if bars else None
    except Exception:
        return None


def peer_metric_row(cik: int, price: float | None, fwd_eps: float | None,
                    fund: dict | None = None) -> dict | None:
    """A peer_metrics-store row (fractions for margins, USD for $ items,
    ratios/×for the rest) from EDGAR fundamentals + an Alpaca price. Cells that
    can't be derived are None (→ NOT FOUND). `fund` may be a pre-fetched
    company_facts_metrics dict to avoid a second fetch."""
    f = fund or company_facts_metrics(cik)
    if not f:
        return None
    rev = f.get("revenue")
    ebitda = f.get("ebitda")
    debt = f.get("debt")
    cash = f.get("cash")
    shares = f.get("shares")
    mcap = (price * shares) if (price and shares) else None

    # A multiple is only meaningful with a positive denominator: a loss-making
    # peer has NO P/E or EV/EBITDA (a negative "multiple" would mislead and
    # poison the peer median), so those cells stay NOT FOUND — honest, not zero.
    ps = (mcap / rev) if (mcap and rev and rev > 0) else None
    fwd_pe = (price / fwd_eps) if (price and fwd_eps and fwd_eps > 0) else None
    ev_ebitda = None
    if mcap and ebitda and ebitda > 0 and debt is not None and cash is not None:
        ev = mcap + debt - cash
        ev_ebitda = ev / ebitda

    return {
        "rev_growth": f.get("rev_growth"), "gm": f.get("gm"), "om": f.get("om"),
        "ebitda_margin": f.get("ebitda_margin"), "net_income": f.get("net_income"),
        "fcf": f.get("fcf"), "de": f.get("de"),
        "ps": ps, "fwd_pe": fwd_pe, "ev_ebitda": ev_ebitda,
        "source_url": f.get("source_url"), "doc_date": f.get("fy_end"),
    }


def _bar_close_near(symbol: str, target: str, adjustment: str) -> float | None:
    """Daily close on/just after `target` (ISO date) from Alpaca IEX with the
    given adjustment ('split' or 'raw'). None on any failure — read-only."""
    creds = _alpaca_creds()
    if not creds:
        return None
    key, sec, _ = creds
    try:
        from datetime import timedelta
        end = (date.fromisoformat(target) + timedelta(days=12)).isoformat()
        r = requests.get(f"{DATA_BASE}/stocks/{symbol}/bars",
                         params={"timeframe": "1Day", "start": target, "end": end,
                                 "limit": 5, "feed": "iex", "adjustment": adjustment},
                         headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
                         timeout=20)
        if r.status_code != 200:
            return None
        bars = r.json().get("bars", [])
        return round(bars[0]["c"], 4) if bars else None
    except Exception:
        return None


def split_since_fy(symbol: str, fy_end: str | None) -> str:
    """Detect a stock split between fy_end and today (the NFLX 10x class of
    error: today's split-adjusted price ÷ a filed pre-split EPS overstates P/E by
    the split factor). Compares the split-adjusted vs raw close at fy_end — the
    adjusted series divides historical prices by any post-date split factor, so a
    material adjusted-vs-raw gap AT that historical date means a split happened
    since. Returns 'none', 'split', or 'unknown' (detection unavailable → caller
    prints P/E with a caveat, never silently wrong)."""
    if not fy_end:
        return "unknown"
    adj = _bar_close_near(symbol, fy_end, "split")
    raw = _bar_close_near(symbol, fy_end, "raw")
    if not adj or not raw:
        return "unknown"
    ratio = raw / adj
    return "none" if 0.99 <= ratio <= 1.01 else "split"


def valuation_snapshot(cik: int, price: float | None, fund: dict | None = None,
                       symbol: str | None = None) -> dict | None:
    """Compact valuation multiples for a quick take, from EDGAR company-facts +
    a live (split-adjusted) Alpaca price. Reuses company_facts_metrics for the
    EDGAR fetch/parse — no new statement-parsing. Every derived cell is None
    (→ NOT FOUND) when its inputs are absent; a non-positive diluted EPS yields
    pe=None with pe_nm=True ("n/m — earnings negative") rather than a misleading
    negative multiple. When `symbol` is given and a P/E is about to be computed,
    a cheap split-detection guard runs (split_since_fy): a split since the FY
    filing → pe_split=True (filed EPS is pre-split, not yet restated → no number);
    detection unavailable → pe_caveat=True (print P/E with an explicit caveat).
    `fund` may be a pre-fetched company_facts_metrics dict to avoid a second EDGAR
    fetch. Returns None only if company-facts can't be fetched at all."""
    f = fund or company_facts_metrics(cik)
    if not f:
        return None
    rev = f.get("revenue")
    eps = f.get("eps_diluted")
    shares = f.get("shares")
    fy_end = f.get("fy_end")
    mcap = (price * shares) if (price and shares) else None
    # A multiple is only meaningful with a positive denominator (same discipline
    # as peer_metric_row): a loss-maker has no trailing P/E.
    ps = (mcap / rev) if (mcap and rev and rev > 0) else None
    pe, pe_nm, pe_split, pe_caveat = None, False, False, False
    if eps is not None and eps <= 0:
        pe_nm = True  # negative/zero earnings → P/E not meaningful (price-independent)
    elif price and eps is not None and eps > 0:
        # About to compute P/E → now (and only now) pay for the split guard.
        status = split_since_fy(symbol, fy_end) if symbol else "none"
        if status == "split":
            pe_split = True  # filed EPS pre-split → refuse a wrong-by-split-factor P/E
        else:
            pe = price / eps
            pe_caveat = (status == "unknown")  # detection failed → number + caveat
    return {
        "entity": f.get("entity"),
        "fy_end": fy_end,
        "price": price,
        "revenue": rev,
        "rev_growth": f.get("rev_growth"),
        "eps_diluted": eps,
        "net_income": f.get("net_income"),
        "shares": shares,
        "market_cap": mcap,
        "ps": ps,
        "pe": pe,
        "pe_nm": pe_nm,
        "pe_split": pe_split,
        "pe_caveat": pe_caveat,
        "source_url": f.get("source_url"),
    }


if __name__ == "__main__":
    import sys, json
    cik = int(sys.argv[1]) if len(sys.argv) > 1 else 320193  # AAPL (demo default)
    f = company_facts_metrics(cik)
    print(json.dumps({k: v for k, v in (f or {}).items() if k != "source_url"},
                     indent=1, default=str))
