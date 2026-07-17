"""scout/gather.py — light live evidence gather for self-sufficient quick takes.

A quick take is the radar default (the project design) and must NOT depend on a curated
evidence pack. Given only a ticker, this assembles a compact, dated evidence
snippet from free sources — resolve → price/52wk (Alpaca) → latest earnings
8-K / 10-Q headline text (EDGAR) → best-effort consensus — for ONE Sonnet pass.
No Opus, no adversary.

Every network call degrades gracefully and is logged as a dated source or an
honest gap — never a crash, never a fabrication. Read-only; no order code.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import requests

from . import llm
from .config import app_name, edgar_user_agent, load_config, user_agent
from .db import Database
from .market_ref import _alpaca_creds, _sec_headers, resolve_ticker

DATA_BASE = "https://data.alpaca.markets/v2"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"


def _alpaca_price_range(symbol: str) -> dict:
    creds = _alpaca_creds()
    if not creds:
        return {"error": "Alpaca keys unavailable"}
    key, sec, _ = creds
    start = (date.today() - timedelta(days=370)).isoformat()
    try:
        r = requests.get(
            f"{DATA_BASE}/stocks/{symbol}/bars",
            params={"timeframe": "1Day", "start": start, "limit": 400, "feed": "iex"},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}, timeout=20)
        if r.status_code != 200:
            return {"error": f"Alpaca bars HTTP {r.status_code}"}
        bars = r.json().get("bars", [])
        if not bars:
            return {"error": "no bars returned (symbol may be thin on the IEX feed)"}
        highs = [b["h"] for b in bars]
        lows = [b["l"] for b in bars]
        last = bars[-1]
        recent = [(b["t"][:10], round(b["c"], 2)) for b in bars[-5:]]
        return {"latest_close": round(last["c"], 2), "asof": last["t"][:10],
                "high_52w": round(max(highs), 2), "low_52w": round(min(lows), 2),
                "recent": recent, "n_bars": len(bars)}
    except Exception as e:
        return {"error": f"Alpaca fetch failed: {e}"}


_EARNINGS_RE = re.compile(
    r"financial results|reports.{0,30}results|revenue of|net income|"
    r"earnings per share|diluted (?:net )?(?:income|eps)|gross margin", re.I)


def _edgar_latest(cik: int, scan_8ks: int = 4) -> dict:
    """Latest 8-K/10-Q/10-K for the sources list, PLUS the latest *earnings* 8-K
    (scan recent 8-Ks; the newest one is often a notes offering, not results)."""
    try:
        r = requests.get(SUBMISSIONS.format(cik=cik), headers=_sec_headers(), timeout=20)
        r.raise_for_status()
        recent = r.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accs = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        out = {"filings": []}
        for want in ("8-K", "10-Q", "10-K"):
            for i, f in enumerate(forms):
                if f == want:
                    acc = accs[i].replace("-", "")
                    url = ARCHIVE.format(cik=cik, acc=acc) + (docs[i] or "")
                    out["filings"].append({"form": f, "date": dates[i], "url": url})
                    break
        # Find the latest EARNINGS 8-K by scanning recent 8-Ks in order.
        seen = 0
        for i, f in enumerate(forms):
            if f != "8-K":
                continue
            seen += 1
            acc = accs[i].replace("-", "")
            url, text = _fetch_8k_earnings_text(cik, acc, docs[i])
            if _EARNINGS_RE.search(text):
                out["earnings_8k"] = {"date": dates[i], "url": url, "text": text}
                break
            if seen >= scan_8ks:
                # no earnings 8-K found in the scan window — keep the latest 8-K's
                # text so the model has *something* dated, flagged as non-earnings.
                out.setdefault("earnings_8k", {"date": dates[i], "url": url,
                               "text": text, "not_earnings": True})
                break
        return out
    except Exception as e:
        return {"error": f"EDGAR submissions failed: {e}"}


def _fetch_8k_earnings_text(cik: int, acc: str, primary: str) -> tuple[str, str]:
    """Best-effort: find the EX-99.1 press release in the 8-K's folder (that's
    where the headline financials live); fall back to the primary 8-K doc.
    Returns (url, truncated_text)."""
    base = ARCHIVE.format(cik=cik, acc=acc)
    ex_url = None
    try:  # list the accession folder, look for an ex-99 exhibit
        idx = requests.get(base, headers=_sec_headers(), timeout=20)
        if idx.status_code == 200:
            for m in re.finditer(r'href="([^"]+\.htm)"', idx.text, re.I):
                name = m.group(1).lower()
                if "ex99" in name or "ex-99" in name or "ex_99" in name or "991" in name:
                    ex_url = base + m.group(1).split("/")[-1]
                    break
    except Exception:
        pass
    url = ex_url or (base + (primary or ""))
    try:
        doc = requests.get(url, headers=_sec_headers(), timeout=20)
        if doc.status_code != 200:
            return url, f"(could not fetch filing text: HTTP {doc.status_code})"
        text = re.sub(r"<[^>]+>", " ", doc.text)  # strip tags
        text = re.sub(r"\s+", " ", text).strip()
        return url, text[:6000]
    except Exception as e:
        return url, f"(could not fetch filing text: {e})"


def _nasdaq_consensus(symbol: str) -> dict | None:
    """Analyst consensus from Nasdaq's free JSON endpoint (no key, not JS-rendered
    — audit fix 2026-07-12: the prior stockanalysis.com source is JS-rendered and
    the gatherer usually couldn't read it, so consensus was almost always NOT
    FOUND). Returns a dated snapshot or None. Consensus is CONTEXT only — an
    analyst target is never an expected return (the project design)."""
    try:
        r = requests.get(f"https://api.nasdaq.com/api/analyst/{symbol}/targetprice",
                         headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                         timeout=12)
        if r.status_code != 200:
            return None
        ov = ((r.json() or {}).get("data") or {}).get("consensusOverview") or {}
        pt = ov.get("priceTarget")
        if pt in (None, "", 0):
            return None
        return {"source": "nasdaq.com analyst targetprice",
                "accessed": date.today().isoformat(),
                "price_target": pt, "pe": None,
                "target_low": ov.get("lowPriceTarget"),
                "target_high": ov.get("highPriceTarget"),
                "rating": f"buy {ov.get('buy', 0)} / hold {ov.get('hold', 0)} / "
                          f"sell {ov.get('sell', 0)}"}
    except Exception:
        return None


def _fmt_consensus(c: dict) -> str:
    """One-line consensus summary for the pack — target, range, rating, source,
    access date. Analyst target is context, never an expected return."""
    parts = [f"price target {c.get('price_target')}"]
    if c.get("target_low") and c.get("target_high"):
        parts.append(f"range {c['target_low']}–{c['target_high']}")
    if c.get("rating"):
        parts.append(f"ratings {c['rating']}")
    if c.get("pe"):
        parts.append(f"PE {c['pe']}")
    return (f"{c.get('source')} (accessed {c.get('accessed')}): "
            + ", ".join(parts) + " — CONTEXT only, not an expected return")


def _consensus_snapshot(symbol: str) -> dict:
    """Best-effort live consensus. Tries Nasdaq's free JSON endpoint first (not
    JS-rendered), then the stockanalysis.com page, then honestly marks a gap."""
    nq = _nasdaq_consensus(symbol)
    if nq:
        return nq
    try:
        r = requests.get(f"https://stockanalysis.com/stocks/{symbol}/",
                         headers={"User-Agent": user_agent()}, timeout=15)
        if r.status_code == 200:
            pt = re.search(r'"priceTarget"\s*:\s*([\d.]+)', r.text)
            pe = re.search(r'"peRatio"\s*:\s*([\d.]+)', r.text)
            if pt or pe:
                return {"source": "stockanalysis.com", "accessed": date.today().isoformat(),
                        "price_target": pt.group(1) if pt else None,
                        "pe": pe.group(1) if pe else None}
    except Exception:
        pass
    return {"note": "NOT FOUND — no free live-consensus source captured this "
                    "run. Treat consensus as unavailable; consider adding a paid "
                    "consensus source."}


class Inactive(str):
    """Sentinel light_evidence returns when the ticker is inactive/delisted — a
    str subclass carrying the honest, owner-facing reason. The caller must
    surface that reason and build NO evidence pack; it must NOT treat the ticker
    as merely 'unresolvable' (2026-07-15 stale-ticker fix). isinstance(x, Inactive)
    distinguishes it from a normal (markdown, n) success and from None."""


def light_evidence(symbol: str) -> tuple[str, int] | Inactive | None:
    """Assemble a compact dated evidence snippet for a quick take. Returns
    (markdown, n_sources); an Inactive sentinel when the ticker is
    inactive/delisted (no live data exists — the queue entry is stale); or None
    if the ticker can't be resolved at all.

    When the ticker resolves but is NOT an SEC filer and Alpaca serves it as a
    (different) active security — the PHTN reassignment case — the snippet is
    still built, but its FIRST line carries the identity warning verbatim and
    every price is attributed to the ALPACA-LISTED instrument, never to the
    assumed queue company."""
    symbol = symbol.upper().strip()
    res = resolve_ticker(symbol)
    if res.get("active") is False:
        # Inactive/delisted (ARJT shape): there is NO live data to gather and the
        # queue entry is stale. Return an honest reason, build nothing.
        reason = res.get("identity_warning") or "Alpaca reports it no longer trades"
        return Inactive(f"{symbol} is inactive/delisted — no live data exists; the "
                        f"queue entry is stale ({reason}).")
    if not res.get("resolved"):
        return None
    today = date.today().isoformat()
    # Non-filer but active (PHTN reassignment): attribute all prices to the
    # Alpaca-listed instrument, and lead the snippet with the identity warning.
    non_filer = res.get("sec_filer") is False
    identity_warning = res.get("identity_warning")
    price_name = res.get("alpaca_name") or res.get("name") or symbol
    price = _alpaca_price_range(symbol)
    edgar = _edgar_latest(res.get("cik")) if res.get("cik") else {"error": "no CIK"}
    consensus = _consensus_snapshot(symbol)

    n_sources = 1  # resolve
    lines = [f"# {symbol} ({res.get('name')}) — Light Evidence Snippet (quick take)"]
    if non_filer and identity_warning:
        # FIRST line after the title, verbatim — the model and owner see it first.
        lines.append(f"> ⚠ IDENTITY WARNING: {identity_warning}")
    price_header = "## Price & 52-week range (Alpaca IEX, fetched " + today + ")"
    if non_filer:
        price_header = (f"## Price & 52-week range — prices below are for the "
                        f"ALPACA-LISTED instrument \"{price_name}\", NOT confirmed "
                        f"to be {symbol}'s intended company (see identity warning "
                        f"above) — Alpaca IEX, fetched {today}")
    lines += [f"**Compiled:** {today} (live fetch). Scope: quick-take evidence only, "
              f"dated + sourced; gaps marked honestly.",
              f"**Identity:** {res.get('name')} · {res.get('exchange') or 'exchange n/a'} · "
              f"CIK {res.get('cik')} · sources: {', '.join(res.get('sources', []))}.",
              "", price_header]
    if "error" in price:
        lines.append(f"- NOT FOUND: {price['error']}.")
    else:
        n_sources += 1
        lines += [f"- Latest close: ${price['latest_close']} (as of {price['asof']}).",
                  f"- 52-week range (trailing ~1y IEX bars): ${price['low_52w']} – "
                  f"${price['high_52w']}.",
                  "- Recent closes: " + ", ".join(f"{d} ${c}" for d, c in price["recent"]) + "."]

    lines += ["", "## Latest SEC filings (EDGAR, fetched " + today + ")"]
    if "error" in edgar:
        lines.append(f"- NOT FOUND: {edgar['error']}.")
    else:
        for f in edgar.get("filings", []):
            n_sources += 1
            lines.append(f"- {f['form']} filed {f['date']}: {f['url']}")
        k = edgar.get("earnings_8k")
        if k:
            n_sources += 1
            label = ("Latest 8-K text (filed {d}) — NOTE: no earnings 8-K found in "
                     "the recent scan window, so this may not be results; the model "
                     "should rely on price/52wk and the 10-Q reference above."
                     if k.get("not_earnings")
                     else "Latest EARNINGS 8-K (filed {d}) — headline figures, "
                          "verbatim excerpt").format(d=k["date"])
            lines += ["", f"### {label}", f"Source: {k['url']}", "", k["text"]]

    lines += ["", "## Consensus snapshot"]
    if "note" in consensus:
        lines.append(f"- {consensus['note']}")
    else:
        n_sources += 1
        lines.append("- " + _fmt_consensus(consensus))

    # Options expression data (2026-07-12) — deterministic math block.
    from .options_ref import options_snapshot_md
    lines += ["", options_snapshot_md(symbol, price.get("latest_close"))]
    if "NOT FOUND" not in lines[-1]:
        n_sources += 1

    lines += ["", "## Honest gaps",
              "- This is a LIGHT quick-take gather, not a full evidence pack: no "
              "peer comps, no multi-quarter series, no adversarial review.",
              "- Consensus is best-effort and may be NOT FOUND (above).",
              "- Only the single latest 8-K's text is included; deeper history "
              "and the 10-Q body are not parsed at this tier."]
    return "\n".join(lines), n_sources


# ── FULL evidence gather (standard/full depths) ──
# Code fetches raw filings; Haiku EXTRACTS each (cheap; stored in `evidence` so a
# document is never model-read twice); Sonnet ASSEMBLES the structured pack per
# prompts/evidence_pack.md. On-demand — never depends on the weekly radar.

def _fetch_doc_text(url: str, max_chars: int = 14000) -> str:
    try:
        r = requests.get(url, headers=_sec_headers(), timeout=25)
        if r.status_code != 200:
            return f"(could not fetch {url}: HTTP {r.status_code})"
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        return f"(could not fetch {url}: {e})"


def _docs_for_full_gather(cik: int, scan_8ks: int = 5, n_earnings: int = 3) -> list[dict]:
    """The document set for a full gather: recent EARNINGS 8-Ks (multi-quarter
    trend), the latest 10-Q, and the latest 10-K."""
    try:
        r = requests.get(SUBMISSIONS.format(cik=cik), headers=_sec_headers(), timeout=20)
        r.raise_for_status()
        recent = r.json().get("filings", {}).get("recent", {})
        forms, dates = recent.get("form", []), recent.get("filingDate", [])
        accs, prims = recent.get("accessionNumber", []), recent.get("primaryDocument", [])
        docs, seen8k = [], 0
        for i, f in enumerate(forms):
            if f == "8-K" and len([d for d in docs if d["form"] == "8-K"]) < n_earnings:
                seen8k += 1
                acc = accs[i].replace("-", "")
                url, text = _fetch_8k_earnings_text(cik, acc, prims[i])
                if _EARNINGS_RE.search(text):
                    docs.append({"form": "8-K", "date": dates[i], "url": url, "text": text})
                if seen8k >= scan_8ks:
                    pass
        for want in ("10-Q", "10-K"):
            for i, f in enumerate(forms):
                if f == want:
                    acc = accs[i].replace("-", "")
                    url = ARCHIVE.format(cik=cik, acc=acc) + (prims[i] or "")
                    docs.append({"form": want, "date": dates[i], "url": url,
                                 "text": _fetch_doc_text(url)})
                    break
        return docs
    except Exception:
        return []


def _latest_quarterly(cik: int) -> dict | None:
    """The single most-recent periodic report (10-Q or 10-K) by filing date, from
    EDGAR submissions — an independent recency gate for the pack (audit fix
    2026-07-12: e.g. a pack could ship without the company's latest quarter, one
    filed weeks earlier). Returns {form, date, url} or None."""
    try:
        r = requests.get(SUBMISSIONS.format(cik=cik), headers=_sec_headers(), timeout=20)
        r.raise_for_status()
        recent = r.json().get("filings", {}).get("recent", {})
        forms, dates = recent.get("form", []), recent.get("filingDate", [])
        accs, prims = recent.get("accessionNumber", []), recent.get("primaryDocument", [])
        best = None
        for i, f in enumerate(forms):
            if f in ("10-Q", "10-K") and (best is None or dates[i] > best["date"]):
                acc = accs[i].replace("-", "")
                url = ARCHIVE.format(cik=cik, acc=acc) + (prims[i] or "")
                best = {"form": f, "date": dates[i], "url": url}
        return best
    except Exception:
        return None


# The minimum length (chars) of an extraction's FACTS portion for it to count as
# real content. A failed fetch or a boilerplate-only page (e.g. a 10-Q cover/TOC)
# yields a near-empty Haiku result; below this floor the extraction is treated as
# failed and is NEVER stored — cementing an empty extraction is exactly what would
# let a latest quarter show "zero extracted content" in the pack (audit #4).
_MIN_FACTS_LEN = 40
# A model refusal is prose, not facts — and a VERBOSE refusal easily clears the
# 40-char floor and even carries digits ("I cannot extract … from this 10-Q …"),
# so length alone can't filter it (audit #4: two cached rows — each a verbose
# "I cannot extract…" refusal several hundred chars long — were once stored as if
# they were extraction content, poisoning the pack). A real extraction always
# states at least one figure, so we
# additionally require a digit AND reject text that opens with a refusal.
_REFUSAL = re.compile(
    r"\b(?:I\s+cannot|I\s+can'?t|I\s+am\s+unable|I'?m\s+unable|unable\s+to|"
    r"cannot\s+extract|could\s+not\s+extract|no\s+financial\s+figures|"
    r"there\s+are\s+no\s+(?:financial|dated))\b", re.I)


def _looks_like_content(facts: str, min_len: int = _MIN_FACTS_LEN) -> bool:
    """True iff `facts` is a real extraction, not an empty header, a figure-less
    blurb, or a model refusal. Requires: at least `min_len` chars, at least one
    digit (every genuine extraction states a figure), and no refusal phrasing in
    the opening (checked over the first 160 chars so a leading bullet/sentence
    before "I cannot extract…" doesn't sneak a refusal through)."""
    facts = (facts or "").strip()
    if len(facts) < min_len:
        return False
    if not any(c.isdigit() for c in facts):
        return False
    if _REFUSAL.search(facts[:160]):
        return False
    return True


def _within_days(d1: str, d2: str, days: int) -> bool:
    """True iff two ISO dates are within `days` of each other (either direction)."""
    try:
        return abs((date.fromisoformat(d1) - date.fromisoformat(d2)).days) <= days
    except (ValueError, TypeError):
        return False


def _extraction_has_content(extractions: list[str], target_date: str,
                            min_len: int = _MIN_FACTS_LEN,
                            extra_dates: list[str] | None = None) -> bool:
    """True iff some extraction dated `target_date` (or any date in `extra_dates`)
    carries real facts — not just an empty `[FORM filed DATE] ` header, a figure-
    less blurb, or a cached refusal. Each extraction is `[FORM filed DATE] FACTS`
    (see `_extract_doc`); a failed one is ''. The recency gate uses this to decide
    whether the latest filed quarter genuinely reached the pack — presence of a URL
    is not enough, and neither is a verbose refusal that clears the length floor
    (audit #4). `extra_dates` lets a same-quarter earnings 8-K filed a few days off
    the 10-Q count toward the quarter (FIX 3: press-release 8-Ks commonly file days
    BEFORE the 10-Q, so keying only on the 10-Q's date produced false staleness)."""
    markers = [f"filed {d}]" for d in ([target_date] + list(extra_dates or [])) if d]
    for ex in extractions:
        if ex and any(mk in ex for mk in markers):
            facts = ex.split("] ", 1)[-1].strip()
            if _looks_like_content(facts, min_len):
                return True
    return False


def _extract_doc(db: Database, symbol: str, doc: dict, force: bool = False) -> str:
    """Haiku-extract dated headline facts from one filing, STORED in `evidence`
    keyed by URL — a document is never fetched or model-read twice (per the project design).

    Returns '' when the filing could not be fetched or yielded no real content, so
    a failed extraction is NEVER cached as satisfied — the recency gate relies on
    this to detect a genuinely missing latest quarter instead of matching a URL
    whose stored extraction is empty. force=True re-extracts (and updates the row)
    even when a prior — possibly empty, legacy — row already exists."""
    text = doc.get("text") or ""
    existing = db.select_one("evidence", {"source_url": doc["url"]})
    # Serve a cached row only if it is not poison. We drop the length FLOOR here
    # (min_len=1) so a genuine-but-short legacy extraction is still reused without a
    # fresh Haiku call — only a refusal or a figure-less blurb is rejected.
    if (existing and existing.get("extracted_text") and not force
            and _looks_like_content(existing["extracted_text"], min_len=1)):
        return f"[{doc['form']} filed {doc['date']}] " + existing["extracted_text"]
    # A cached row that FAILS the content gate is a legacy poison row (a refusal or
    # a figure-less blurb stored before this gate existed). Do not serve it from
    # cache — fall through and re-extract so the row self-heals: the db.update below
    # overwrites it in-place (local JSON AND Railway Postgres, whichever db is
    # active), and if this run still can't extract, we return '' rather than the
    # poison so the recency gate flags the quarter as missing (audit #4).
    # A failed/empty fetch must not be model-read or stored: extracting an error
    # string wastes a Haiku call and cements an empty row for this URL forever.
    if not text or text.startswith("(could not fetch"):
        return ""
    prompt = (f"Extract the dated headline facts from this {doc['form']} (filed "
              f"{doc['date']}) VERBATIM — revenue, growth, margins, EPS, guidance, "
              f"capacity/pricing statements, customer concentration, risk factors. "
              f"Compact bullets, each with the figure. Add nothing not present.\n\n"
              + text)
    r = llm.call(f"{symbol}-extract", "haiku", [{"role": "user", "content": prompt}],
                 max_tokens=1500, db=db)
    facts = (r["text"] or "").strip()
    # No real content — a refusal, a figure-less blurb, or below the length floor.
    # Do NOT store it (that is what poisoned the cached rows); leave the URL uncached
    # so a later run (or the recency gate's forced re-extract) retries with fresh
    # text. A previously-stored poison row for this URL is left untouched here and
    # is overwritten only when a real extraction succeeds.
    if not _looks_like_content(facts):
        return ""
    row = {"symbol": symbol, "doc_date": doc["date"], "source_url": doc["url"],
           "doc_type": doc["form"], "extracted_text": facts[:4000]}
    if existing and existing.get("id") is not None:
        db.update("evidence", existing["id"], row)
    else:
        db.insert("evidence", row)
    return f"[{doc['form']} filed {doc['date']}] " + facts


def _gatherer_system(symbol: str, name: str, as_of: str) -> str:
    from .research import _load_template
    t = _load_template("evidence_pack")
    return (t.replace("{{SYMBOL}}", symbol)
             .replace("{{COMPANY}}", name or symbol)
             .replace("{{AS_OF_DATE}}", as_of)
             .replace("{{CUTOFF_CLAUSE}}", "No cutoff — this is a live on-demand gather.")
             .replace("{{EDGAR_USER_AGENT}}", edgar_user_agent()))


def full_evidence(symbol: str, db: Database, as_of: str | None = None) -> tuple[str, int, float] | None:
    """Build a full, dated evidence pack on demand for any resolvable ticker.
    Returns (pack_markdown, n_sources, gather_cost_usd) or None if unresolvable.
    Gathering cost is logged to api_costs and returned so the caller can report it."""
    from datetime import date as _date
    symbol = symbol.upper().strip()
    as_of = as_of or _date.today().isoformat()
    res = resolve_ticker(symbol)
    if not res.get("resolved") or not res.get("cik"):
        return None

    price = _alpaca_price_range(symbol)
    consensus = _consensus_snapshot(symbol)
    docs = _docs_for_full_gather(res["cik"])
    extractions = [_extract_doc(db, symbol, d) for d in docs]

    # Recency gate (audit fix 2026-07-12, hardened 2026-07-13 audit #4): confirm
    # the single most-recent 10-Q/10-K is represented in the pack BY EXTRACTED
    # CONTENT, not merely by a URL/date match. The prior gate only checked that a
    # doc with the latest quarter's URL-or-date was in `docs` — but `_docs_for_
    # full_gather` already appends the latest 10-Q/10-K by construction, so that
    # check was always satisfied even when the quarter's extraction was EMPTY
    # (failed fetch, or a 10-Q whose first pages are cover/TOC boilerplate). That
    # is the mechanism behind a latest-quarter miss: the quarter's doc can be
    # "present" but carry zero extracted figures, and a same-day 8-K's date can even
    # mask the empty 10-Q. Now: if no extraction dated at the latest quarter
    # carries real content, re-fetch + force-re-extract the 10-Q AND its same-
    # period earnings 8-K (the press release is where the headline figures live —
    # the same source the relay's light path uses); only if that still yields no
    # content do we prepend a prominent staleness warning.
    recency_warning = None
    latest_q = _latest_quarterly(res["cik"])
    if latest_q and not _extraction_has_content(extractions, latest_q["date"]):
        # Repair: (1) the 10-Q/10-K body itself, (2) the latest earnings 8-K, since
        # the headline figures (revenue/EPS/backlog) live in the 8-K press release,
        # not the front matter of the 10-Q. Force-re-extract to bypass any legacy
        # empty row cemented by an earlier failed run.
        repair_docs = [{"form": latest_q["form"], "date": latest_q["date"],
                        "url": latest_q["url"], "text": _fetch_doc_text(latest_q["url"])}]
        edgar = _edgar_latest(res["cik"])
        e8k = edgar.get("earnings_8k") if isinstance(edgar, dict) else None
        # Same-quarter earnings 8-K: count it if its filing date is within ±10 days
        # of the 10-Q's (FIX 3). The press release routinely files a few days BEFORE
        # the 10-Q, so the old `>=` test dropped exactly the filing that carries the
        # headline figures and then warned "stale" even though the quarter was fine.
        earnings_8k_date = None
        if (e8k and not e8k.get("not_earnings")
                and _within_days(e8k.get("date", ""), latest_q["date"], 10)):
            earnings_8k_date = e8k["date"]
            repair_docs.append({"form": "8-K", "date": e8k["date"],
                                "url": e8k["url"], "text": e8k.get("text", "")})
        have_urls = {d["url"] for d in docs}
        for rd in repair_docs:
            ex = _extract_doc(db, symbol, rd, force=True)
            if ex:
                if rd["url"] not in have_urls:
                    docs.append(rd)
                    have_urls.add(rd["url"])
                extractions.append(ex)
        # Credit the quarter if the 10-Q OR its same-quarter earnings 8-K (even a
        # few days earlier) now carries real content.
        extra = [earnings_8k_date] if earnings_8k_date else None
        if not _extraction_has_content(extractions, latest_q["date"], extra_dates=extra):
            recency_warning = (
                f"⚠️ LATEST FILED QUARTER ({latest_q['form']} filed "
                f"{latest_q['date']}) MISSING — analysis is stale by construction. "
                f"Its filings could not be fetched/extracted this run "
                f"({latest_q['url']}); do not treat the valuation as current.")

    # Raw materials handed to the Sonnet assembler (grounding — no invention).
    raw = [f"IDENTITY: {res.get('name')} · {res.get('exchange')} · CIK {res.get('cik')} "
           f"(sources: {', '.join(res.get('sources', []))})."]
    if "error" not in price:
        raw.append(f"PRICE (Alpaca IEX, {as_of}): close ${price['latest_close']} "
                   f"({price['asof']}); 52-wk ${price['low_52w']}–${price['high_52w']}; "
                   f"recent {price['recent']}.")
    else:
        raw.append(f"PRICE: NOT FOUND ({price['error']}).")
    raw.append("CONSENSUS: " + (consensus.get("note") or _fmt_consensus(consensus)))
    raw.append("\nEXTRACTED FILINGS (Haiku, dated + sourced):\n" + "\n\n".join(extractions))
    raw.append("\nSOURCE URLS:\n" + "\n".join(f"- {d['form']} {d['date']}: {d['url']}" for d in docs))

    # Audit fix 2026-07-12: consult the evidence store BOTH ways — a fact Scout
    # already extracted (e.g. the exact share count from a proxy 8-K) must reach
    # every later pack instead of being re-guessed as NOT FOUND.
    fetched_urls = {d["url"] for d in docs}
    stored = [e for e in db.select("evidence", {"symbol": symbol})
              if e.get("extracted_text") and e.get("source_url") not in fetched_urls]
    if stored:
        prior = "\n\n".join(
            f"[{e.get('doc_type') or 'doc'} dated {e.get('doc_date') or 'n/a'}, "
            f"source {e['source_url']}]\n{e['extracted_text'][:1200]}"
            for e in stored[-8:])
        raw.append(f"\nPREVIOUSLY STORED DATED FACTS (from {app_name()}'s evidence store — "
                   "use these before declaring anything NOT FOUND; cite their "
                   "original source + date):\n" + prior)

    user = ("Structure the following raw, dated materials into the evidence pack "
            "exactly per the section order and rules in the system prompt. Use ONLY "
            "these materials — invent nothing; mark honest gaps (e.g. no peer comps "
            "fetched this run). Keep every figure's date and source.\n\n" + "\n".join(raw))

    r = llm.call(f"{symbol}-gather", "sonnet",
                 [{"role": "user", "content": user}], max_tokens=8000,
                 system=_gatherer_system(symbol, res.get("name"), as_of), db=db)
    gather_cost = r["usd"] + sum(0.0 for _ in extractions)  # extraction costs already logged
    n_sources = 1 + (0 if "error" in price else 1) + len(docs) + (0 if "note" in consensus else 1)

    # Options expression data (2026-07-12): deterministic block appended
    # AFTER assembly so the math survives verbatim (never paraphrased by a model).
    from .options_ref import options_snapshot_md
    opts_md = options_snapshot_md(symbol, price.get("latest_close") if "error" not in price else None)
    # Peer comps (the project design, Task 1 2026-07-13): auto-discover 3–5 peers
    # (deterministic SIC shortlist + a cheap Haiku pick) and populate the
    # peer_metrics store deterministically (EDGAR company-facts + Alpaca +
    # Nasdaq fwd EPS), then render a POPULATED comps table. Cached with a ~7-day
    # TTL so re-gathers reuse peers and prices. Fail-open: any peer step failing
    # degrades to the NOT-FOUND scaffold, never a crash or a fabricated cell.
    from .comps import comps_table_md
    peer_syms: list[str] = []
    try:
        from . import peers
        pe = peers.ensure_peers(db, symbol, res["cik"], res.get("name"), as_of=as_of)
        peer_syms = pe.get("peer_symbols", [])
        gather_cost += pe.get("cost_usd", 0.0)
    except Exception:
        peer_syms = []
    comps_md = comps_table_md(symbol, db, peer_symbols=peer_syms)
    pack_text = r["text"].rstrip() + "\n\n" + opts_md + "\n\n" + comps_md + "\n"
    # A missing latest quarter is a construction-level staleness flag — it leads
    # the pack so the underwriter (and the owner) can't miss it.
    if recency_warning:
        pack_text = f"> {recency_warning}\n\n" + pack_text
    if "NOT FOUND" not in opts_md:
        n_sources += 1
    return pack_text, n_sources, gather_cost


if __name__ == "__main__":
    import sys
    out = light_evidence(sys.argv[1] if len(sys.argv) > 1 else "AAPL")
    if isinstance(out, Inactive):
        # Inactive/delisted sentinel is a str subclass, not a (md, n) tuple —
        # unpacking it would raise. Surface its honest reason, mirroring how
        # research.py handles it (2026-07-15 stale-ticker fix).
        print(str(out))
    elif out is None:
        print("unresolvable ticker")
    else:
        md, n = out
        print(f"[{n} sources]\n")
        print(md[:2500])
