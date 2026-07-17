"""scout/estimates.py — forward EPS consensus from Nasdaq's free analyst JSON
(Task 2, built 2026-07-13).

NO LLM. Fills the forward P/E the key-number cards and comps table were missing:
there was no forward-EPS source at all. Nasdaq's earnings-forecast endpoint
(same header trick that made targetprice work in gather._nasdaq_consensus)
carries a `yearlyForecast` block with next-fiscal-year consensus EPS.

Discipline (same as the targetprice integration): fail-open to None on any
schema/availability issue — never a crash, never a fabricated estimate. A
forward estimate is CONTEXT, not an expected return (the project design). Every
value is labeled with its source + access date. Read-only. No order code.
"""

from __future__ import annotations

from datetime import date

import requests

FORECAST = "https://api.nasdaq.com/api/analyst/{sym}/earnings-forecast"
_HDRS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# "Mon YYYY" fiscal-end -> month number, to compare a forecast FY-end to today.
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _fy_end_date(label: str) -> date | None:
    """'Jan 2027' -> date(2027,1,31)-ish (day is immaterial for the comparison)."""
    try:
        mon, yr = label.strip().split()
        return date(int(yr), _MONTHS[mon[:3].lower()], 28)
    except Exception:
        return None


def forward_eps(symbol: str, as_of: str | None = None) -> dict | None:
    """Next-fiscal-year consensus EPS: the nearest yearly forecast whose fiscal
    year has NOT already ended as of `as_of`. Returns a dated snapshot or None.

    {fwd_eps, fy_end, n_estimates, high, low, source, accessed} — fail-open."""
    symbol = symbol.upper().strip()
    ref = date.fromisoformat(as_of) if as_of else date.today()
    try:
        r = requests.get(FORECAST.format(sym=symbol), headers=_HDRS, timeout=12)
        if r.status_code != 200:
            return None
        rows = (((r.json() or {}).get("data") or {}).get("yearlyForecast") or {}).get("rows") or []
        best = None
        for row in rows:
            eps = row.get("consensusEPSForecast")
            fend = _fy_end_date(row.get("fiscalEnd", ""))
            if eps in (None, "", 0) or fend is None or fend < ref:
                continue
            if best is None or fend < best[0]:
                best = (fend, row)
        if not best:
            return None
        row = best[1]
        return {"fwd_eps": float(row["consensusEPSForecast"]),
                "fy_end": row.get("fiscalEnd"),
                "n_estimates": row.get("noOfEstimates"),
                "high": row.get("highEPSForecast"),
                "low": row.get("lowEPSForecast"),
                "source": "nasdaq.com analyst earnings-forecast",
                "accessed": date.today().isoformat()}
    except Exception:
        return None


if __name__ == "__main__":
    import sys, json
    print(json.dumps(forward_eps(sys.argv[1] if len(sys.argv) > 1 else "AAPL"),
                     indent=1))
