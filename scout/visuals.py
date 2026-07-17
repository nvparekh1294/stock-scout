"""scout/visuals.py — deterministic visual header for HTML briefs (the project design,
built 2026-07-12).

NO LLM. Pure code that renders, into every HTML brief:
  1. a 1-year weekly-close price chart as INLINE SVG (self-contained — briefs are
     emailed/downloaded, so no external images and no JavaScript), with 52-week
     high/low reference lines;
  2. key-number cards: price, 52-wk high/low, TTM EPS, P/S, forward P/E + analyst
     consensus — each carrying an access/publication date, each with explicit
     NOT FOUND handling (never a fabricated number, the project design).

Price data comes from Alpaca (read-only keys from .env, NEVER printed). Fundamentals (EPS/P-S/fwd-P-E/consensus) are best-effort parsed
from the brief body (which carries pack-sourced, dated figures); anything not
found stays NOT FOUND. Colors are theme-safe: the price line and labels use
`currentColor` (flips with the page's light/dark scheme) and reference lines use
a neutral tone legible in both. Contains NO order/execution code.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import requests

from .market_ref import _alpaca_creds

DATA_BASE = "https://data.alpaca.markets/v2"
_NEUTRAL = "#9a8c98"   # legible on both #ffffff-ish and #15151f backgrounds


# ── data ───────────────────────────────────────────────────────────────────
def weekly_closes(symbol: str) -> dict:
    """1-year weekly closes + 52-wk high/low from Alpaca (IEX). Returns
    {closes:[(date,close)], high, low, latest, asof} or {'error': ...}. Never
    raises — an unavailable feed is an honest gap, not a crash."""
    creds = _alpaca_creds()
    if not creds:
        return {"error": "Alpaca keys unavailable"}
    key, sec, _ = creds
    start = (date.today() - timedelta(days=372)).isoformat()
    try:
        r = requests.get(
            f"{DATA_BASE}/stocks/{symbol}/bars",
            params={"timeframe": "1Week", "start": start, "limit": 60, "feed": "iex"},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}, timeout=20)
        if r.status_code != 200:
            return {"error": f"Alpaca bars HTTP {r.status_code}"}
        bars = r.json().get("bars", [])
        if not bars:
            return {"error": "no weekly bars (symbol may be thin on IEX)"}
        closes = [(b["t"][:10], round(b["c"], 2)) for b in bars]
        return {"closes": closes,
                "high": round(max(b["h"] for b in bars), 2),
                "low": round(min(b["l"] for b in bars), 2),
                "latest": round(bars[-1]["c"], 2),
                "asof": bars[-1]["t"][:10]}
    except Exception as e:
        return {"error": f"Alpaca fetch failed: {e}"}


# ── SVG price chart ────────────────────────────────────────────────────────
def price_chart_svg(series: dict, width: int = 680, height: int = 220) -> str:
    """Inline SVG line chart of weekly closes with 52-wk high/low reference
    lines. Self-contained (no JS, no external refs). Theme-safe colors."""
    if "error" in series or not series.get("closes"):
        why = series.get("error", "no data") if isinstance(series, dict) else "no data"
        return (f'<div class="chart-missing">Price chart: NOT FOUND '
                f'({why}).</div>')
    closes = series["closes"]
    lo_ref, hi_ref = series["low"], series["high"]
    vals = [c for _, c in closes]
    vmin, vmax = min(min(vals), lo_ref), max(max(vals), hi_ref)
    span = (vmax - vmin) or 1.0
    padL, padR, padT, padB = 8, 82, 12, 22
    plot_w, plot_h = width - padL - padR, height - padT - padB

    def x(i):
        return padL + (plot_w * i / max(1, len(closes) - 1))

    def y(v):
        return padT + plot_h * (1 - (v - vmin) / span)

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(closes))
    area = (f"{padL:.1f},{padT + plot_h:.1f} " + pts +
            f" {x(len(closes) - 1):.1f},{padT + plot_h:.1f}")
    y_hi, y_lo, y_last = y(hi_ref), y(lo_ref), y(closes[-1][1])
    first_lbl, last_lbl = closes[0][0], closes[-1][0]

    def ref_line(yv, label, val):
        return (f'<line x1="{padL}" y1="{yv:.1f}" x2="{padL + plot_w:.1f}" '
                f'y2="{yv:.1f}" stroke="{_NEUTRAL}" stroke-width="1" '
                f'stroke-dasharray="4 3" opacity="0.8"/>'
                f'<text x="{padL + plot_w + 4:.1f}" y="{yv + 3:.1f}" '
                f'font-size="10" fill="{_NEUTRAL}">{label} ${val:,.0f}</text>')

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" aria-label="1-year weekly close price chart" '
        f'style="max-width:100%;height:auto;display:block;margin:6px 0 2px">'
        f'<polygon points="{area}" fill="currentColor" opacity="0.06"/>'
        f'{ref_line(y_hi, "52w high", hi_ref)}'
        f'{ref_line(y_lo, "52w low", lo_ref)}'
        f'<polyline points="{pts}" fill="none" stroke="currentColor" '
        f'stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{x(len(closes) - 1):.1f}" cy="{y_last:.1f}" r="3" '
        f'fill="currentColor"/>'
        f'<text x="{padL}" y="{height - 6}" font-size="10" fill="{_NEUTRAL}">'
        f'{first_lbl}</text>'
        f'<text x="{padL + plot_w:.1f}" y="{height - 6}" font-size="10" '
        f'fill="{_NEUTRAL}" text-anchor="end">{last_lbl}</text>'
        f'</svg>')


# ── key-number cards ───────────────────────────────────────────────────────
def _card(label: str, value: str, sub: str) -> str:
    return (f'<div class="kn-card"><div class="kn-label">{label}</div>'
            f'<div class="kn-value">{value}</div>'
            f'<div class="kn-sub">{sub}</div></div>')


NF = "NOT FOUND"


def key_number_cards(metrics: dict) -> str:
    """metrics: {price, price_asof, high_52w, low_52w, range_asof, ttm_eps,
    ps, fwd_pe, consensus, consensus_date, ...} — any missing value renders as
    NOT FOUND with no fabricated number."""
    def v(x, fmt=None, prefix=""):
        if x in (None, "", NF):
            return NF
        return prefix + (fmt.format(x) if fmt else str(x))

    price = v(metrics.get("price"), "{:,.2f}", "$")
    hi = v(metrics.get("high_52w"), "{:,.2f}", "$")
    lo = v(metrics.get("low_52w"), "{:,.2f}", "$")
    cards = [
        _card("Price", price, f"Alpaca IEX · {metrics.get('price_asof', NF)}"),
        _card("52-wk range", (f"{lo} – {hi}" if hi != NF or lo != NF else NF),
              f"Alpaca IEX · {metrics.get('range_asof', NF)}"),
        _card("TTM EPS", v(metrics.get("ttm_eps"), "{:,.2f}", "$"),
              metrics.get("ttm_eps_src") or "from pack"),
        _card("P/S", v(metrics.get("ps"), "{:,.1f}×"),
              metrics.get("ps_src") or "from pack"),
        _card("Fwd P/E", v(metrics.get("fwd_pe"), "{:,.1f}×"),
              metrics.get("fwd_pe_src") or "from pack"),
        _card("Analyst consensus", v(metrics.get("consensus")),
              f"as of {metrics.get('consensus_date', NF)}"),
    ]
    return '<div class="kn-cards">' + "".join(cards) + '</div>'


VISUAL_CSS = """
.kn-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
 gap:8px;margin:10px 0 14px}
.kn-card{border:1px solid #cbd0d8;border-radius:8px;padding:8px 10px;
 background:#f7f8fb}
.kn-label{font-size:.72em;text-transform:uppercase;letter-spacing:.04em;
 color:#6b6b83}
.kn-value{font-size:1.12em;font-weight:600;margin:2px 0}
.kn-sub{font-size:.68em;color:#8a8aa0}
.chart-missing{color:#8a8aa0;font-size:.85em;font-style:italic;margin:8px 0}
@media(prefers-color-scheme:dark){
 .kn-card{border-color:#3a3a52;background:#1c1c2b}
 .kn-label{color:#a0a0b8}.kn-sub{color:#7a7a92}}
:root[data-theme=dark] .kn-card{border-color:#3a3a52;background:#1c1c2b}
:root[data-theme=light] .kn-card{border-color:#cbd0d8;background:#f7f8fb}
"""


# ── brief-body metric parsing (best-effort, dated, NOT FOUND otherwise) ─────
_EPS_NUM = r"\$\s*([\d,]+(?:\.\d+)?)"


def _ttm_eps_from_text(t: str) -> float | None:
    """Latest-period diluted/TTM EPS from a brief body. Handles the progression
    and comparison phrasings the packs use, which the old single-figure regex got
    wrong (audit #4): "diluted EPS $4.20→$7.30" grabbed the PRIOR year $4.20, not
    the current $7.30. Rules, in priority order:
      0. an explicitly TTM/latest-FY-labeled figure wins;
      1. a progression "EPS $A→$B" reports the current period on the RIGHT → B;
      2. a comparison "EPS $A vs/from $B" reports current on the LEFT → A;
      3. a bare figure ("$7.30 diluted EPS" / "diluted EPS $7.30") is taken as-is.
    Returns None if no EPS figure is clearly present (never invents)."""
    def _f(s):
        try:
            return float(s.replace(",", ""))
        except (ValueError, AttributeError):
            return None
    # 0. an explicitly TRAILING-labeled figure ("TTM EPS $7.30", "EPS (ttm) of
    #    $7.30", "latest-FY diluted EPS $7.30"). A bare fiscal year is deliberately
    #    NOT a trailing label — "implied FY2028 EPS ~$9.85" is a FORWARD estimate,
    #    not TTM (audit #4 near-miss). A progression may still follow — prefer B.
    m = re.search(r"(?:TTM|trailing[-\s]twelve[-\s]month|latest[-\s]FY|FY[-\s]latest)"
                  r"\s*(?:diluted\s*)?EPS[^$\n]{0,15}" + _EPS_NUM +
                  r"(?:\s*(?:→|->|—>|➜|to)\s*" + _EPS_NUM + r")?", t, re.I)
    if m:
        return _f(m.group(2) or m.group(1))
    # 0b. "EPS (ttm) of $X" where the ttm tag trails the word EPS
    m = re.search(r"EPS\s*\(ttm\)\s*(?:of\s*)?" + _EPS_NUM, t, re.I)
    if m:
        return _f(m.group(1))
    # 1. progression: EPS $A → $B  → take B (current period)
    m = re.search(r"EPS[^$\n]{0,20}" + _EPS_NUM +
                  r"\s*(?:→|->|—>|➜)\s*" + _EPS_NUM, t, re.I)
    if m:
        return _f(m.group(2))
    # 2. comparison: EPS $A vs/from/compared-with $B  → take A (current period)
    m = re.search(r"EPS[^$\n]{0,20}" + _EPS_NUM +
                  r"\s*(?:vs\.?|versus|compared\s+(?:with|to)|from)\s*" + _EPS_NUM, t, re.I)
    if m:
        return _f(m.group(1))
    # 3. bare figure, value-first then label-first
    m = (re.search(_EPS_NUM + r"\s*(?:diluted\s*)?EPS\b", t, re.I)
         or re.search(r"(?:diluted\s*)?EPS\s*(?:\(ttm\))?\s*(?:of\s*)?" + _EPS_NUM, t, re.I))
    return _f(m.group(1)) if m else None


def parse_metrics_from_brief(text: str) -> dict:
    """Best-effort extraction of the fundamentals the header shows, from the
    brief body (which carries pack-sourced, dated figures). Never invents — a
    figure that isn't clearly present stays NOT FOUND."""
    t = re.sub(r"[*_`]", "", text or "")
    m = {}
    # trailing P/E (TTM) — "trailing P/E ≈ 58.7x" / "~59x trailing"
    pe = re.search(r"trailing\s*P/?E\s*[≈~]?\s*([\d,]+(?:\.\d+)?)\s*[x×]", t, re.I) \
        or re.search(r"[~≈]\s*([\d,]+(?:\.\d+)?)\s*[x×]\s*trailing", t, re.I)
    # forward P/E — "forward P/E 16.85" / "fwd P/E ≈ 17x"
    fpe = re.search(r"(?:forward|fwd)\s*P/?E\s*[≈~]?\s*([\d,]+(?:\.\d+)?)", t, re.I)
    # P/S — "P/S ≈ 7.4x" / "7.4x trailing sales" / "trailing P/S ≈ 7.4x"
    ps = re.search(r"P/?S\s*[≈~]?\s*([\d,]+(?:\.\d+)?)\s*[x×]", t, re.I) \
        or re.search(r"([\d,]+(?:\.\d+)?)\s*[x×]\s*(?:trailing\s*)?sales", t, re.I)
    if fpe:
        m["fwd_pe"] = float(fpe.group(1).replace(",", ""))
    if ps:
        m["ps"] = float(ps.group(1).replace(",", ""))
    eps = _ttm_eps_from_text(t)
    if eps is not None:
        m["ttm_eps"] = eps
    # consensus — the packs frequently say "consensus … NOT FOUND"
    if re.search(r"consensus[^.\n]{0,60}NOT FOUND", t, re.I):
        m["consensus"] = NF
    return m


def _live_enrich(symbol: str, metrics: dict, price, db) -> None:
    """Fill fwd P/E and analyst consensus from the SAME live sources a fresh
    gather uses (deterministic, NO LLM): the cached peer_metrics subject row
    (P/S, fwd P/E) + Nasdaq forward EPS + Nasdaq targetprice consensus. Every
    value stays dated and fail-open — a fetch/schema miss leaves NOT FOUND, never
    a fabricated number (the project design). Only fills cells still NOT FOUND, so
    a figure the brief body already carried is never overwritten."""
    today = date.today().isoformat()
    # Cached deterministic peer row (P/S, fwd P/E) — populated by the gather.
    if db is not None:
        try:
            row = db.select_one("peer_metrics", {"symbol": symbol.upper()})
        except Exception:
            row = None
        if row:
            if metrics.get("ps") in (None, "", NF) and row.get("ps") is not None:
                metrics["ps"] = float(row["ps"]); metrics["ps_src"] = "comps store"
            if metrics.get("fwd_pe") in (None, "", NF) and row.get("fwd_pe") is not None:
                metrics["fwd_pe"] = float(row["fwd_pe"]); metrics["fwd_pe_src"] = "comps store"
    # Forward P/E direct from Nasdaq fwd EPS + live price (if still missing).
    if metrics.get("fwd_pe") in (None, "", NF) and price not in (None, "", NF):
        try:
            from .estimates import forward_eps
            fe = forward_eps(symbol)
            if fe and fe.get("fwd_eps"):
                metrics["fwd_pe"] = round(float(price) / float(fe["fwd_eps"]), 1)
                metrics["fwd_pe_src"] = f"{fe['source']} · {fe.get('fy_end')} · {fe['accessed']}"
        except Exception:
            pass
    # Analyst consensus (context only, never an expected return).
    if metrics.get("consensus") in (None, "", NF):
        try:
            from .gather import _consensus_snapshot
            c = _consensus_snapshot(symbol)
            if c and "note" not in c and c.get("price_target"):
                metrics["consensus"] = f"PT ${c['price_target']}"
                metrics["consensus_date"] = c.get("accessed", today)
        except Exception:
            pass


def build_metrics(symbol: str, brief_text: str = "", db=None) -> dict:
    """Assemble the header metrics: price/52-wk from Alpaca; fundamentals parsed
    from the brief body; forward P/E + consensus from the same live/cached
    sources the gather uses (deterministic); everything else NOT FOUND. Returns a
    (metrics, series) tuple — the metrics dict and the weekly-close series the
    price chart is drawn from."""
    today = date.today().isoformat()
    series = weekly_closes(symbol)
    metrics = {"consensus": NF, "consensus_date": NF, "ttm_eps": NF, "ps": NF,
               "fwd_pe": NF}
    price = NF
    if "error" not in series:
        price = series["latest"]
        metrics.update(price=series["latest"], price_asof=series["asof"],
                       high_52w=series["high"], low_52w=series["low"],
                       range_asof=today)
    else:
        metrics.update(price=NF, price_asof=NF, high_52w=NF, low_52w=NF,
                       range_asof=NF)
    metrics.update({k: v for k, v in parse_metrics_from_brief(brief_text).items()})
    _live_enrich(symbol, metrics, price, db)
    return metrics, series


def render_visual_header(symbol: str, brief_text: str = "", db=None) -> tuple[str, str]:
    """Return (header_html, extra_css). header_html = chart + key-number cards,
    ready to inject just after the brief's <h1>."""
    metrics, series = build_metrics(symbol, brief_text, db=db)
    header = ('<div class="visual-header">'
              + price_chart_svg(series)
              + key_number_cards(metrics)
              + '</div>')
    return header, VISUAL_CSS


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    h, css = render_visual_header(sym, "")
    print(f"<style>{css}</style>{h}"[:1200])
