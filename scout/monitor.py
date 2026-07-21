"""scout/monitor.py — the daily thesis-integrity monitor.

For every active/watch thesis, every holding with a thesis, and every live
entry trigger: pull NEW signals only (EDGAR filings since last check, policy
feed, news RSS), Haiku-extract anything unseen (never-read-twice store),
Sonnet-assess against the thesis via prompts/monitor_check.md, and alert ONLY
on a state change. Price moves are explicitly not signals (the project design). A quiet
day produces zero messages and one ledger_marks batch.

Watched tickers (live entry triggers) get a lighter intra-market-hours check.
Market hours are computed from the exchange clock (America/New_York via
zoneinfo) — never hardcoded UTC (a common DST mistake).

Read-only against the world; writes only to Scout's own store.
Contains NO order/execution code.
"""

from __future__ import annotations

import argparse
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from . import llm
from .config import load_config, user_agent
from .db import Database
from .gather import SUBMISSIONS, ARCHIVE, _extract_doc, _fetch_8k_earnings_text
from .market_ref import _sec_headers, resolve_ticker

EASTERN = ZoneInfo("America/New_York")
MONITOR_FORMS = ("8-K", "10-Q", "10-K", "S-1", "424B5", "SC 13D", "SC 13G")
LAST_CHECK_FLAG = "monitor_last_daily"
WATCH_CHECK_FLAG = "monitor_last_watch"


# ── market clock ───────────────────────────────────────────────────────────
def market_hours_now(now: datetime | None = None) -> bool:
    """True during regular NYSE hours (9:30–16:00 ET, Mon–Fri). Exchange-clock
    based, so it is DST-proof by construction. Exchange holidays are accepted
    as false-positive check windows (a quiet holiday costs nothing)."""
    now = (now or datetime.now(tz=EASTERN)).astimezone(EASTERN)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= minutes < (16 * 60)


# ── signal gathering (new documents only) ──────────────────────────────────
def _filings_since(cik: int, since: str, limit: int = 8) -> list[dict]:
    """Filings of monitored forms filed strictly after `since` (YYYY-MM-DD)."""
    try:
        r = requests.get(SUBMISSIONS.format(cik=cik), headers=_sec_headers(), timeout=20)
        r.raise_for_status()
        recent = r.json().get("filings", {}).get("recent", {})
        forms, dates = recent.get("form", []), recent.get("filingDate", [])
        accs, prims = recent.get("accessionNumber", []), recent.get("primaryDocument", [])
        out = []
        for i, form in enumerate(forms):
            if form not in MONITOR_FORMS or dates[i] <= since:
                continue
            acc = accs[i].replace("-", "")
            if form == "8-K":
                url, text = _fetch_8k_earnings_text(cik, acc, prims[i])
            else:
                url = ARCHIVE.format(cik=cik, acc=acc) + (prims[i] or "")
                text = ""  # body fetched only if the doc is new (extract step)
            out.append({"form": form, "date": dates[i], "url": url, "text": text})
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []  # honest gap — a failed fetch is a quiet check, not a crash


def _news_signals(symbol: str, since: str) -> list[str]:
    """Fenced open-internet signals from configured news RSS feeds. These may
    ANNOTATE risk or queue research — the prompt forbids them from flipping a
    state on their own (the project design). Empty feed list → no signals."""
    feeds = load_config().get("feeds", {}).get("news_rss") or []
    out = []
    for feed in feeds:
        try:
            r = requests.get(feed, timeout=15,
                             headers={"User-Agent": user_agent()})
            if r.status_code != 200:
                continue
            for m in re.finditer(r"<item>(.*?)</item>", r.text, re.S):
                item = m.group(1)
                t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
                d = re.search(r"<pubDate>(.*?)</pubDate>", item)
                title = (t.group(1) if t else "").strip()
                if symbol.upper() in title.upper():
                    out.append(f"[UNTRUSTED news RSS, {d.group(1) if d else 'undated'}] "
                               f"{title[:300]}")
        except Exception:
            continue
    return out[:5]


def new_signals_for(db: Database, symbol: str, since: str) -> list[str]:
    """Dated, sourced signals for one symbol since `since`. New filings are
    Haiku-extracted once and stored (never-read-twice)."""
    res = resolve_ticker(symbol)
    signals: list[str] = []
    if res.get("resolved") and res.get("cik"):
        for doc in _filings_since(res["cik"], since):
            if db.select_one("evidence", {"source_url": doc["url"]}):
                continue  # already absorbed by a prior run/underwrite
            if not doc["text"]:
                from .gather import _fetch_doc_text
                doc["text"] = _fetch_doc_text(doc["url"])
            signals.append(_extract_doc(db, symbol, doc) + f"\nSource: {doc['url']}")
    signals.extend(_news_signals(symbol, since))
    return signals


# ── the per-thesis check ───────────────────────────────────────────────────
def _fmt_conditions(rows: list[dict], kind: str) -> str:
    if not rows:
        return f"(no {kind})"
    lines = []
    for r in rows:
        n = r.get("ordinal") or r.get("id")
        lines.append(f"{n}. [{r['status']}] {r['condition_text']}")
    return "\n".join(lines)


def check_thesis(db: Database, thesis: dict, signals: list[str],
                 as_of: str) -> str | None:
    """Run the monitor_check prompt for one thesis. Returns the 3-line alert
    text, or None on NO_CHANGE. No new signals → no LLM call, no alert."""
    if not signals:
        return None
    breaks = db.select("break_conditions", {"thesis_id": thesis["id"]}, order_by="ordinal")
    triggers = db.select("entry_triggers", {"thesis_id": thesis["id"]})
    from .research import build_prompt
    system, user = build_prompt("monitor_check", {"AS_OF_DATE": as_of}, {
        "THESIS": f"{thesis['symbol']} [{thesis['status']}] "
                  f"{(thesis.get('thesis_text') or '')[:1500]}",
        "BREAK_CONDITIONS": _fmt_conditions(breaks, "break conditions"),
        "ENTRY_TRIGGERS": _fmt_conditions(triggers, "entry triggers"),
        "NEW_SIGNALS": "\n\n".join(signals)[:12000],
    })
    r = llm.call(f"{thesis['symbol']}-monitor", "sonnet",
                 [{"role": "user", "content": user}], max_tokens=1500,
                 system=system, db=db)
    text = (r["text"] or "").strip()
    if not text or text.upper().startswith("NO_CHANGE"):
        return None
    _apply_state_changes(db, thesis, breaks, triggers, text)
    return f"⚠️ {thesis['symbol']} — {text}"


def _apply_state_changes(db: Database, thesis: dict, breaks: list[dict],
                         triggers: list[dict], alert_text: str) -> None:
    """Conservatively mirror the alert's named condition into the store: only a
    condition the alert explicitly numbers changes state (dated-primary-source
    discipline lives in the prompt; this is bookkeeping, not judgment)."""
    low = alert_text.lower()
    for b in breaks:
        n = b.get("ordinal")
        if n and re.search(rf"break condition\s*#?{n}\b", low):
            db.update("break_conditions", b["id"], {"status": "triggered"})
    for t in triggers:
        cond = (t.get("condition_text") or "")[:40].lower()
        if "entry trigger" in low and cond and cond in low:
            db.update("entry_triggers", t["id"], {"status": "fired"})
    if "break condition" in low and "triggered" in low:
        db.update("theses", thesis["id"], {"status": "broken",
                                           "updated_at": datetime.now(EASTERN).isoformat()})


# ── ledger marks ───────────────────────────────────────────────────────────
def _closes_since(symbol: str, start: str) -> list[tuple[str, float]]:
    from .market_ref import _alpaca_creds
    creds = _alpaca_creds()
    if not creds:
        return []
    key, sec, _ = creds
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
            params={"timeframe": "1Day", "start": start, "limit": 500, "feed": "iex",
                    "adjustment": "split"},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}, timeout=20)
        if r.status_code != 200:
            return []
        return [(b["t"][:10], float(b["c"])) for b in r.json().get("bars", [])]
    except Exception:
        return []


def ledger_marks_daily(db: Database, as_of: str) -> int:
    """One mark per recommendation per day: latest close + return-vs-SPY since
    rec date. Skips recs already marked today; honest skip when price data is
    unavailable."""
    recs = db.select("recommendations", order_by="id")
    if not recs:
        return 0
    spy = {d: c for d, c in _closes_since("SPY",
           min(str(r["rec_date"]) for r in recs))}
    marked = 0
    for rec in recs:
        if db.select_one("ledger_marks", {"recommendation_id": rec["id"],
                                          "mark_date": as_of}):
            continue
        th = db.select_one("theses", {"id": rec.get("thesis_id")}) or {}
        symbol = th.get("symbol")
        if not symbol:
            continue
        closes = _closes_since(symbol, str(rec["rec_date"]))
        if not closes:
            continue
        first_px, last_px = closes[0][1], closes[-1][1]
        vs_spy = None
        spy_days = sorted(spy)
        spy_at_rec = next((spy[d] for d in spy_days if d >= str(rec["rec_date"])), None)
        spy_now = spy[spy_days[-1]] if spy_days else None
        base = float(rec.get("price_at_rec") or first_px)
        if spy_at_rec and spy_now and base:
            stock_ret = (last_px / base - 1) * 100
            spy_ret = (spy_now / spy_at_rec - 1) * 100
            vs_spy = round(stock_ret - spy_ret, 2)
        db.insert("ledger_marks", {"recommendation_id": rec["id"],
                                   "mark_date": as_of, "price": last_px,
                                   "vs_spy_pct": vs_spy})
        marked += 1
    return marked


# ── entry points ───────────────────────────────────────────────────────────
def _last_check(db: Database, flag: str, default_days: int = 3) -> str:
    row = db.select_one("system_flags", {"flag": flag})
    if row and row.get("value"):
        return str(row["value"])[:10]
    return (date.today() - timedelta(days=default_days)).isoformat()


def _set_last_check(db: Database, flag: str, value: str) -> None:
    if db.select_one("system_flags", {"flag": flag}):
        if db.backend == "postgres":
            db._conn.execute("UPDATE system_flags SET value=%s, set_at=now() "
                             "WHERE flag=%s", (value, flag))
        else:
            store = db._json_load("system_flags")
            for r in store["rows"]:
                if r.get("flag") == flag:
                    r["value"] = value
            db._json_save("system_flags", store)
    else:
        db.insert("system_flags", {"flag": flag, "value": value})


def monitored_theses(db: Database) -> list[dict]:
    """Active + watch theses, deduped to the latest per symbol."""
    latest: dict[str, dict] = {}
    for t in db.select("theses", order_by="id"):
        if t.get("status") in ("active", "watch") and t.get("symbol"):
            latest[t["symbol"]] = t
    return list(latest.values())


def run_daily(db: Database | None = None, as_of: str | None = None) -> dict:
    """The daily sweep: thesis-integrity per monitored thesis + one
    ledger-marks batch. Returns {'alerts': [...], 'checked': n, 'marks': n}."""
    db = db or Database()
    db.apply_schema()
    as_of = as_of or date.today().isoformat()
    since = _last_check(db, LAST_CHECK_FLAG)
    alerts, checked = [], 0
    for thesis in monitored_theses(db):
        signals = new_signals_for(db, thesis["symbol"], since)
        alert = check_thesis(db, thesis, signals, as_of)
        checked += 1
        if alert:
            alerts.append(alert)
        for b in db.select("break_conditions", {"thesis_id": thesis["id"]}):
            db.update("break_conditions", b["id"],
                      {"last_checked": datetime.now(EASTERN).isoformat()})
    marks = ledger_marks_daily(db, as_of)
    _set_last_check(db, LAST_CHECK_FLAG, as_of)
    return {"alerts": alerts, "checked": checked, "marks": marks}


def run_watch(db: Database | None = None) -> dict:
    """The intra-market-hours pass: ONLY symbols with live entry triggers or
    watch status, ONLY new filings (no news, no marks). Cheap and quiet."""
    db = db or Database()
    if not market_hours_now():
        return {"alerts": [], "checked": 0, "skipped": "market closed"}
    since = _last_check(db, WATCH_CHECK_FLAG, default_days=1)
    alerts, checked = [], 0
    for thesis in monitored_theses(db):
        if thesis.get("status") != "watch":
            live = [t for t in db.select("entry_triggers", {"thesis_id": thesis["id"]})
                    if t.get("status") == "watching"]
            if not live:
                continue
        res = resolve_ticker(thesis["symbol"])
        if not (res.get("resolved") and res.get("cik")):
            continue
        docs = [d for d in _filings_since(res["cik"], since)
                if not db.select_one("evidence", {"source_url": d["url"]})]
        signals = []
        for doc in docs:
            if not doc["text"]:
                from .gather import _fetch_doc_text
                doc["text"] = _fetch_doc_text(doc["url"])
            signals.append(_extract_doc(db, thesis["symbol"], doc)
                           + f"\nSource: {doc['url']}")
        alert = check_thesis(db, thesis, signals, date.today().isoformat())
        checked += 1
        if alert:
            alerts.append(alert)
    _set_last_check(db, WATCH_CHECK_FLAG, date.today().isoformat())
    return {"alerts": alerts, "checked": checked}


# ── AC verification ────────────────────────────────────────────────────────
def _simulate_break(db: Database) -> dict:
    """Acceptance test: a simulated triggered break condition produces exactly one
    3-line alert; a quiet pass produces zero. Uses a synthetic thesis + a
    synthetic dated primary-source signal; cleans up after itself."""
    tid = db.insert("theses", {
        "symbol": "__SIM__", "stage": 1, "conviction": 7, "verdict": "UNDERWRITE",
        "thesis_text": "SIMULATION: __SIM__ wins because its sole-source supply "
                       "agreement with MegaCorp guarantees capacity through 2028.",
        "status": "active"})
    db.insert("break_conditions", {
        "thesis_id": tid, "ordinal": 1,
        "condition_text": "MegaCorp terminates or fails to renew the sole-source "
                          "supply agreement", "check_frequency": "daily",
        "status": "intact"})
    thesis = db.select_one("theses", {"id": tid})
    signal = ("[8-K filed 2026-07-11] __SIM__ announced that MegaCorp has "
              "terminated the sole-source supply agreement effective immediately."
              "\nSource: https://www.sec.gov/Archives/edgar/data/0000000000/sim8k.htm")
    alert = check_thesis(db, thesis, [signal], date.today().isoformat())
    quiet = check_thesis(db, thesis, [], date.today().isoformat())
    b = db.select_one("break_conditions", {"thesis_id": tid})
    out = {"alert": alert, "quiet_pass_alert": quiet,
           "break_status_after": b["status"] if b else None}
    db.delete("break_conditions", {"thesis_id": tid})
    db.delete("theses", {"id": tid})
    return out


def main():
    ap = argparse.ArgumentParser(description="Scout daily monitor")
    ap.add_argument("--watch", action="store_true", help="intra-hours watch pass")
    ap.add_argument("--simulate-break", action="store_true",
                    help="run the acceptance simulation")
    args = ap.parse_args()
    db = Database()
    db.apply_schema()
    if args.simulate_break:
        out = _simulate_break(db)
        print("SIMULATION RESULT")
        print(f"  alert: {out['alert']!r}")
        print(f"  quiet pass alert (must be None): {out['quiet_pass_alert']!r}")
        print(f"  break condition status after: {out['break_status_after']}")
        ok = (out["alert"] and out["quiet_pass_alert"] is None
              and out["break_status_after"] == "triggered")
        print("Acceptance:", "PASS" if ok else "FAIL")
        return
    out = run_watch(db) if args.watch else run_daily(db)
    for a in out["alerts"]:
        print(a)
    print(f"\nchecked={out['checked']} alerts={len(out['alerts'])} "
          + (f"marks={out['marks']}" if "marks" in out else out.get("skipped", "")))


if __name__ == "__main__":
    main()
