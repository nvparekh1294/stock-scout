"""scout/comps.py — deterministic peer comps table (the project design, built
2026-07-12).

NO LLM. Renders a peer comparison table (revenue growth, gross/operating/EBITDA
margin, net income, FCF, D/E, P/S, forward P/E, EV/EBITDA where computable) plus
an explicit premium/discount verdict line, from peer rows that live in the
`peer_metrics` extraction store.

Honesty spine (the project's honesty rule and the underwriter peer rule): a cell is rendered
only from cached peer data that entered via the pack/extraction store — never
model memory. A metric that isn't in the store stays NOT FOUND; the table never
invents a peer number. Reused by both the single-name brief (subject + cached
peers) and the head-to-head compare (Task 10). Contains NO order/execution code.
"""

from __future__ import annotations

import statistics

NF = "NOT FOUND"

# (key, column label, formatter kind). Order = column order in the table.
METRICS = [
    ("rev_growth", "Rev growth", "pct"),
    ("gm", "Gross margin", "pct"),
    ("om", "Op margin", "pct"),
    ("ebitda_margin", "EBITDA margin", "pct"),
    ("net_income", "Net income", "usd"),
    ("fcf", "FCF", "usd"),
    ("de", "D/E", "ratio"),
    ("ps", "P/S", "mult"),
    ("fwd_pe", "Fwd P/E", "mult"),
    ("ev_ebitda", "EV/EBITDA", "mult"),
]
_MULTIPLE_KEYS = ("ps", "fwd_pe", "ev_ebitda")  # lower = cheaper, for the verdict
_STORE_KEYS = [k for k, _, _ in METRICS]


def _fmt(kind: str, v) -> str:
    if v in (None, "", NF):
        return NF
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if kind == "pct":
        return f"{x * 100:,.1f}%"
    if kind == "usd":
        a = abs(x)
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if a >= div:
                return f"${x / div:,.1f}{suf}"
        return f"${x:,.0f}"
    if kind == "ratio":
        return f"{x:,.2f}"
    if kind == "mult":
        return f"{x:,.1f}×"
    return str(v)


def render_comps_table(subject_symbol: str, rows: list[dict]) -> str:
    """rows: [{symbol, rev_growth, gm, ...}] — the subject FIRST, then peers.
    Missing keys render NOT FOUND. Returns a markdown table + premium/discount
    verdict line."""
    subject_symbol = subject_symbol.upper()
    header = "| Company | " + " | ".join(lbl for _, lbl, _ in METRICS) + " |"
    sep = "|---|" + "|".join("---" for _ in METRICS) + "|"
    body = []
    for r in rows:
        sym = (r.get("symbol") or "?").upper()
        tag = " (subject)" if sym == subject_symbol else ""
        cells = [_fmt(kind, r.get(k)) for k, _, kind in METRICS]
        body.append(f"| **{sym}**{tag} | " + " | ".join(cells) + " |")
    verdict = premium_discount_line(subject_symbol, rows)
    return "\n".join([header, sep, *body, "", verdict])


def premium_discount_line(subject_symbol: str, rows: list[dict]) -> str:
    """Compare the subject's valuation multiples to the peer median (per metric).
    Deterministic; NOT FOUND when the subject or the peers lack a comparable."""
    subject_symbol = subject_symbol.upper()
    subject = next((r for r in rows if (r.get("symbol") or "").upper() == subject_symbol), None)
    peers = [r for r in rows if (r.get("symbol") or "").upper() != subject_symbol]
    if not subject or not peers:
        return ("**Premium/discount:** NOT FOUND — need the subject and at least "
                "one peer with cached comparable multiples.")
    verdicts = []
    for key in _MULTIPLE_KEYS:
        s = subject.get(key)
        pvals = [float(r[key]) for r in peers
                 if r.get(key) not in (None, "", NF)]
        if s in (None, "", NF) or not pvals:
            continue
        med = statistics.median(pvals)
        if med == 0:
            continue
        prem = (float(s) / med - 1) * 100
        word = "premium" if prem >= 0 else "discount"
        label = dict((k, lbl) for k, lbl, _ in METRICS)[key]
        verdicts.append(f"{label} {_fmt('mult', s)} vs peer median "
                        f"{_fmt('mult', med)} — a {abs(prem):,.0f}% {word}")
    if not verdicts:
        return ("**Premium/discount:** NOT FOUND — no valuation multiple is "
                "comparable across the subject and peers in the pack.")
    return f"**Premium/discount ({subject_symbol} vs peers):** " + "; ".join(verdicts) + "."


# ── extraction-store cache (peer data enters ONCE, reused across briefs) ────
def upsert_peer_metrics(db, symbol: str, metrics: dict, source_url: str | None = None,
                        doc_date: str | None = None, asof: str | None = None,
                        full_recompute: bool = False) -> None:
    """Cache one company's comparable metrics. Re-running updates in place
    (UNIQUE(symbol)) so a peer is never re-extracted.

    Two write modes, chosen by `full_recompute`:
    - Partial (default, False): only writes metric keys actually supplied — a
      partial re-extract (e.g. one new tag from a follow-up fetch) must not
      wipe a peer's previously cached cells to NULL.
    - Full recompute (True): the caller freshly derived the ENTIRE metrics row
      from freshly-fetched source data (a full fundamentals refetch), so any
      _STORE_KEYS cell it did NOT come back with is genuinely underivable now
      and gets written as an explicit NULL. Without this, a multiple that was
      derivable last week but isn't today would keep sitting in the row under
      a fresh `asof` — date-laundering a stale number as current (review
      finding, 2026-07-13). Honesty spine (the project design): a stale cell
      surviving under today's date is a fabrication by omission."""
    symbol = symbol.upper()
    if full_recompute:
        # Every store key gets an explicit value (possibly None) — a genuine
        # full snapshot of what's derivable right now, not a fabricated
        # carry-over of a prior run's numbers.
        row = {k: metrics.get(k) for k in _STORE_KEYS}
    else:
        row = {k: metrics[k] for k in _STORE_KEYS if k in metrics}
    for k, v in (("source_url", source_url), ("doc_date", doc_date), ("asof", asof)):
        if v is not None:
            row[k] = v
    existing = db.select_one("peer_metrics", {"symbol": symbol})
    if existing:
        if row:
            db.update("peer_metrics", existing["id"], row)
    else:
        db.insert("peer_metrics", {"symbol": symbol, **row})


def peer_metrics_for(db, symbols: list[str]) -> list[dict]:
    """Fetch cached rows for the given symbols, in the order requested. Symbols
    with no cached row are returned as a bare {symbol} (all cells NOT FOUND)."""
    out = []
    for s in symbols:
        s = s.upper()
        r = db.select_one("peer_metrics", {"symbol": s})
        out.append(dict(r) if r else {"symbol": s})
    return out


def comps_table_md(subject: str, db, peer_symbols: list[str] | None = None,
                   subject_metrics: dict | None = None) -> str:
    """Markdown comps block for the evidence pack (wired into standard + full
    gathers). Reads cached peer metrics; if none are cached the table renders as
    a NOT-FOUND scaffold rather than being silently omitted."""
    subject = subject.upper()
    peer_symbols = [p.upper() for p in (peer_symbols or []) if p.upper() != subject]
    subject_row = {"symbol": subject}
    cached_subject = db.select_one("peer_metrics", {"symbol": subject})
    if cached_subject:
        subject_row = dict(cached_subject)
    if subject_metrics:
        subject_row.update({k: v for k, v in subject_metrics.items() if v not in (None, "", NF)})
        subject_row["symbol"] = subject
    rows = [subject_row] + peer_metrics_for(db, peer_symbols)

    lines = ["## Peer comps (extraction store — dated, pack-sourced only)"]
    if not peer_symbols:
        lines.append(
            "- NOT FOUND: no peers are cached for this name yet. Peer comps "
            "require each peer's own filing in the pack/extraction store "
            "(~$1–3 the first time); until then, peer cells are NOT FOUND and no "
            "premium/discount can be computed.")
    lines += ["", render_comps_table(subject, rows),
              "", "*Basis: metrics computed from XBRL filings (FY-latest); may "
              "differ from company-adjusted figures cited in the text (e.g. an "
              "8-K's reported EBITDA margin).*",
              "", "*Peer figures are cached from each peer's own cited filing "
              "(never model memory, per the no-facts-beyond-the-pack rule). "
              "NOT FOUND cells mean the metric is not in the pack.*"]
    return "\n".join(lines)


if __name__ == "__main__":
    # Offline demo (no db, no LLM): synthetic peers around a rich subject.
    demo = [
        {"symbol": "NRDX", "rev_growth": 0.19, "gm": 0.46, "om": 0.31,
         "ebitda_margin": 0.36, "net_income": 289.4e6, "fcf": 240e6,
         "de": 0.09, "ps": 6.85, "fwd_pe": 27.0, "ev_ebitda": 18.0},
        {"symbol": "PWR", "rev_growth": 0.14, "gm": 0.15, "om": 0.08,
         "ebitda_margin": 0.10, "net_income": 800e6, "fcf": 600e6,
         "de": 0.6, "ps": 1.2, "fwd_pe": 24.0, "ev_ebitda": 14.0},
        {"symbol": "MYRG", "rev_growth": 0.10, "gm": 0.11, "om": None,
         "ebitda_margin": 0.07, "net_income": 90e6, "fcf": None,
         "de": 0.2, "ps": 0.7, "fwd_pe": 18.0, "ev_ebitda": 9.0},
    ]
    print(render_comps_table("NRDX", demo))
