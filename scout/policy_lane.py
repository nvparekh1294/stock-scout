"""scout/policy_lane.py — the policy-event fast lane.

Watches for OFFICIAL actions carrying committed money — government equity
stakes, dollar-valued contract awards, offtake/price-floor deals — the one
policy class the event study validated (pop AND drift). Detection sources:
  1. EDGAR full-text search over fresh 8-Ks for award/stake/offtake language
     (primary sources by construction);
  2. any configured official feeds (config feeds.policy_posts_rss — policy-maker
     posts are intent evidence / watch triggers ONLY, never beneficiary triggers).

Feed items are Haiku-classified per prompts/policy_event.md. A BENEFICIARY
trigger runs a fast-lane quick take (cheap tier) and returns an alert for
Telegram; escalation to standard/full stays consent-gated in the relay. These
recs are tagged rec_type='policy_event' so the scorecard can validate the
event-study class out of sample.

Contains NO order/execution code.
"""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta

import requests

from . import llm
from .config import load_config
from .db import Database
from .market_ref import _sec_headers

EFTS = "https://efts.sec.gov/LATEST/search-index"
QUERIES = [
    '"contract award" "Department of"',
    '"offtake agreement"',
    '"price floor"',
    '"equity investment" "United States government"',
]
SEEN_FLAG = "policy_lane_seen"

# ── per-filing classification retry tracking (2026-07-13 honesty fix) ───────
# Bug: a filing whose classification failed/was unusable was still inserted
# into `evidence` (the never-read-twice dedup), so the next hourly scan
# filtered it out and it was never actually retried — yet minimal_alert told
# the owner "will retry next cycle." Fix: withhold the evidence insert on
# failure/unusable output until a usable classification lands OR MAX_RETRIES
# cycles have failed (then commit + send an honest terminal alert instead).
# Counts live in system_flags (no schema change) keyed per source_url.
RETRY_FLAG_PREFIX = "policy_lane_retry:"
MAX_RETRIES = 3


def _retry_flag(url: str) -> str:
    return RETRY_FLAG_PREFIX + url


def _get_retry_count(db: Database, url: str) -> int:
    row = db.select_one("system_flags", {"flag": _retry_flag(url)})
    try:
        return int(row["value"]) if row and row.get("value") is not None else 0
    except (TypeError, ValueError):
        return 0


def _set_system_flag(db: Database, flag: str, value: str) -> None:
    """Set (insert-or-update) a system_flags row. Mirrors run_scan's existing
    SEEN_FLAG update logic — system_flags keys on `flag`, not a serial `id`,
    so the generic Database.update() (which assumes an `id` PK) can't be used."""
    existing = db.select_one("system_flags", {"flag": flag})
    if existing:
        if db.backend == "postgres":
            db._conn.execute("UPDATE system_flags SET value=%s, set_at=now() "
                             "WHERE flag=%s", (value, flag))
        else:
            store = db._json_load("system_flags")
            for r_ in store["rows"]:
                if r_.get("flag") == flag:
                    r_["value"] = value
            db._json_save("system_flags", store)
    else:
        db.insert("system_flags", {"flag": flag, "value": value})


def _bump_retry_count(db: Database, url: str) -> int:
    """Increment and persist this filing's failed-classification count; returns
    the new count."""
    n = _get_retry_count(db, url) + 1
    _set_system_flag(db, _retry_flag(url), str(n))
    return n


def _clear_retry_count(db: Database, url: str) -> None:
    """Drop the retry counter once a filing reaches a terminal state (usably
    classified, or gave up after MAX_RETRIES) — keeps system_flags from growing
    unbounded with rows for filings that are now safely in `evidence`."""
    db.delete("system_flags", {"flag": _retry_flag(url)})


def _efts_search(query: str, since: str) -> list[dict]:
    """EDGAR full-text search for fresh 8-Ks matching one query."""
    try:
        r = requests.get(EFTS, params={
            "q": query, "dateRange": "custom", "startdt": since,
            "enddt": date.today().isoformat(), "forms": "8-K"},
            headers=_sec_headers(), timeout=20)
        if r.status_code != 200:
            return []
        hits = (r.json().get("hits", {}) or {}).get("hits", [])
        out = []
        for h in hits[:10]:
            src = h.get("_source", {})
            acc = (src.get("adsh") or "").replace("-", "")
            cik = (src.get("cik") or [None])[0] if isinstance(src.get("cik"), list) else src.get("cik")
            out.append({
                "date": src.get("file_date"),
                "company": (src.get("display_names") or ["?"])[0],
                "form": src.get("file_type") or "8-K",
                "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
                       if cik and acc else "https://efts.sec.gov/LATEST/search-index?q=" + query,
                "query": query})
        return out
    except Exception:
        return []


def _classify(db: Database, items: list[dict], as_of: str) -> str:
    from .research import build_prompt
    feed_lines = "\n".join(
        f"- {i['date']} | {i['company']} | {i['form']} | matched {i['query']!r} | {i['url']}"
        for i in items)
    system, user = build_prompt("policy_event", {"AS_OF_DATE": as_of},
                                {"FEED_ITEMS": feed_lines})
    r = llm.call("policy-lane-classify", "haiku",
                 [{"role": "user", "content": user}], max_tokens=1500,
                 system=system, db=db)
    return r["text"] or ""


# ── user-facing alert formatting + guards (Task 3, 2026-07-13) ──────────────
# This morning's bug: the classifier's INTERNAL next-step reasoning ("Retrieve
# and review the full EX-10.1 exhibit text. If it specifies … escalate to
# **BENEFICIARY_TRIGGER** …") was pushed to the owner verbatim and truncated
# mid-word at [:400]. The fix: a user-facing alert is BUILT DETERMINISTICALLY
# from the feed item's own metadata (company/form/date/link) + the parsed class,
# borrowing at most one cleaned sentence of "why" from the model — never raw
# model reasoning. A failed/garbage generation degrades to a minimal alert.

TELEGRAM_LIMIT = 4096
_CLASS_TOKENS = {"BENEFICIARY_TRIGGER", "WATCH_TRIGGER", "IGNORE"}
# A real classification carries the structured "class: TOKEN" form. A bare token
# mention inside prose ("escalate to BENEFICIARY_TRIGGER") is NOT a classification.
_CLASS_RE = re.compile(r"class\s*:\s*\**([A-Z_]+)\**", re.I)
_TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)")

# Phrases that mark model output as INTERNAL reasoning / next-step instructions
# rather than an owner-facing fact. Such sentences are stripped from the "why".
_INTERNAL_MARKERS = (
    "escalate to", "retrieve and review", "review the full", "if it specifies",
    "next step", "i will ", "i'll ", "i should", "i need to", "i would",
    "reclassify", "pending review of", "await the", "once i ", "then classify",
    "cannot yet confirm", "unable to confirm without", "would need to",
)
_DEFAULT_REASON = ("Official 8-K matched committed-money language; see the linked "
                   "primary source for the award/stake/offtake detail.")


def _looks_like_internal(text: str) -> bool:
    t = (text or "").lower()
    return any(mk in t for mk in _INTERNAL_MARKERS)


def truncate_telegram(text: str, limit: int = TELEGRAM_LIMIT,
                      note: str = "(full detail in next check)") -> str:
    """Respect Telegram's char limit, cutting at a SENTENCE boundary (never
    mid-word). Falls back to a word boundary if no sentence break is near."""
    if len(text) <= limit:
        return text
    budget = max(0, limit - len(note) - 1)
    head = text[:budget]
    for sep in (". ", ".\n", "! ", "? ", "\n"):
        i = head.rfind(sep)
        if i >= budget * 0.5:
            return head[:i + 1].rstrip() + " " + note
    i = head.rfind(" ")            # no sentence break — never cut mid-word
    if i > 0:
        head = head[:i]
    return head.rstrip() + " " + note


def clean_reason(reason: str, max_len: int = 280) -> str:
    """Reduce the model's reason to ONE owner-facing sentence, dropping any
    internal-instruction sentences. Empty/garbage → a deterministic default."""
    reason = re.sub(r"\s+", " ", reason or "").strip().lstrip("|:-* ")
    if not reason:
        return _DEFAULT_REASON
    sentences = re.split(r"(?<=[.!?])\s+", reason)
    keep = [s for s in sentences if s.strip() and not _looks_like_internal(s)]
    text = keep[0].strip() if keep else _DEFAULT_REASON
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + " …"
    return text


def _ticker_from_display(name: str | None) -> str | None:
    m = _TICKER_RE.search(name or "")
    return m.group(1) if m else None


def _match_line(item: dict, lines: list[str]) -> str | None:
    """Find the classifier line that refers to this feed item (by url, then
    company prefix, then date+class)."""
    url, comp, dt = item.get("url") or "", (item.get("company") or "")[:14], \
        item.get("date") or ""
    for ln in lines:
        if url and url in ln:
            return ln
    for ln in lines:
        if comp and comp in ln:
            return ln
    for ln in lines:
        if dt and dt in ln and _CLASS_RE.search(ln):
            return ln
    return None


def parse_verdicts(items: list[dict], verdicts_text: str) -> list[dict]:
    """Map each feed item to its parsed classification. A line only counts as a
    real classification when it carries the structured `class: TOKEN` form — so
    internal prose that merely mentions a trigger token is never treated as one
    (the core of this morning's bug)."""
    lines = [ln for ln in (verdicts_text or "").splitlines() if ln.strip()]
    out = []
    for item in items:
        ln = _match_line(item, lines)
        cls, reason, usable = None, "", False
        if ln:
            m = _CLASS_RE.search(ln)
            if m and m.group(1).upper() in _CLASS_TOKENS:
                cls = m.group(1).upper()
                reason = clean_reason(ln[m.end():])
                usable = True
        out.append({"item": item, "cls": cls, "reason": reason, "usable": usable})
    return out


def classification_unusable(verdicts_text: str, parsed: list[dict]) -> bool:
    """True when the generation gave us nothing structured to act on — empty,
    failed, or all internal prose with no `class: TOKEN` line for any item. Then
    the caller sends a minimal deterministic alert instead of silence/garbage."""
    if not (verdicts_text or "").strip():
        return True
    return not any(p["cls"] in _CLASS_TOKENS for p in parsed)


def format_alert(item: dict, cls: str, reason: str) -> tuple[str, str | None]:
    """Deterministic owner-facing alert built from the item's own metadata plus
    the parsed class and a cleaned one-sentence why. Returns (text, ticker)."""
    tk = _ticker_from_display(item.get("company"))
    who = item.get("company") or "?"
    klass = ("BENEFICIARY (official committed money)"
             if cls == "BENEFICIARY_TRIGGER"
             else "WATCH (no committed money confirmed)")
    lines = [
        f"🏛 POLICY FAST LANE — {who}",
        f"Filed: {item.get('form', '8-K')} · {item.get('date', 'date n/a')} · "
        f"matched {item.get('query', '')!r}",
        item.get("url", ""),
        f"Classification: {klass}",
        f"Why it matters: {reason}",
    ]
    return "\n".join(l for l in lines if l), tk


def minimal_alert(item: dict) -> str:
    """Fallback when the model call failed or returned unusable text: the bare
    verifiable facts + an explicit 'analysis pending' — never garbage.

    The 'will retry next cycle' promise here is only honest because the caller
    (run_scan) withholds this item from the `evidence` never-read-twice store
    until it is either usably classified or hits MAX_RETRIES (see
    _bump_retry_count / final_failure_alert below, Task 2026-07-13 retry fix)."""
    who = item.get("company") or "?"
    return "\n".join(l for l in [
        f"🏛 POLICY FAST LANE — {who}",
        f"Filed: {item.get('form', '8-K')} · {item.get('date', 'date n/a')} · "
        f"matched {item.get('query', '')!r}",
        item.get("url", ""),
        "Classification pending — automated read unavailable this cycle.",
        "Analysis pending — will retry next cycle.",
    ] if l)


def final_failure_alert(item: dict, max_retries: int) -> str:
    """Sent once a filing has failed classification MAX_RETRIES times running.
    Automated retry stops here (evidence is committed so it is never re-read) —
    an honest terminal message, not another 'pending' promise."""
    who = item.get("company") or "?"
    return "\n".join(l for l in [
        f"🏛 POLICY FAST LANE — {who}",
        f"Filed: {item.get('form', '8-K')} · {item.get('date', 'date n/a')} · "
        f"matched {item.get('query', '')!r}",
        item.get("url", ""),
        f"Analysis failed {max_retries} times — giving up on automated review.",
        "Manual review recommended; link above is the primary source.",
    ] if l)


def run_scan(db: Database | None = None, quick_take: bool = True) -> dict:
    db = db or Database()
    db.apply_schema()
    today = date.today().isoformat()
    since_row = db.select_one("system_flags", {"flag": SEEN_FLAG})
    since = (str(since_row["value"])[:10] if since_row and since_row.get("value")
             else (date.today() - timedelta(days=2)).isoformat())

    items = []
    for q in QUERIES:
        items.extend(_efts_search(q, since))
    # Never re-classify a filing already absorbed (never-read-twice).
    items = [i for i in items if not db.select_one("evidence", {"source_url": i["url"]})]

    alerts: list[str] = []
    if items:
        try:
            verdicts = _classify(db, items, today)
        except Exception as e:
            verdicts = ""  # failed generation → minimal-alert fallback below
            print(f"policy classify failed: {str(e)[:120]}", file=sys.stderr)

        parsed = parse_verdicts(items, verdicts)

        # Commit to `evidence` (never-read-twice) ONLY on a terminal outcome:
        # a usable classification, or MAX_RETRIES failed cycles for this filing.
        # Anything else is withheld so the next hourly scan genuinely re-reads
        # it — making the "will retry next cycle" alert copy actually true.
        final_failures: list[dict] = []
        for p in parsed:
            item = p["item"]
            if p["usable"]:
                _clear_retry_count(db, item["url"])
                try:
                    db.insert("evidence", {"symbol": None, "doc_date": item["date"],
                                           "source_url": item["url"], "doc_type": "policy-scan",
                                           "extracted_text": f"policy-lane scanned: {item['company']}"})
                except Exception:
                    pass
            else:
                attempts = _bump_retry_count(db, item["url"])
                if attempts >= MAX_RETRIES:
                    _clear_retry_count(db, item["url"])
                    try:
                        db.insert("evidence", {"symbol": None, "doc_date": item["date"],
                                               "source_url": item["url"], "doc_type": "policy-scan",
                                               "extracted_text":
                                                   f"policy-lane gave up after {attempts} failed "
                                                   f"classification attempts: {item['company']}"})
                    except Exception:
                        pass
                    final_failures.append(item)
                # else: left OUT of evidence — genuinely retried next cycle.

        beneficiaries = [p for p in parsed
                         if p["usable"] and p["cls"] == "BENEFICIARY_TRIGGER"]
        if beneficiaries:
            for p in beneficiaries[:3]:
                text, sym = format_alert(p["item"], "BENEFICIARY_TRIGGER", p["reason"])
                if quick_take and sym:
                    try:
                        from . import research
                        out = research.underwrite(sym, depth="quick", db=db)
                        rec = db.select("recommendations", order_by="id")
                        if rec:
                            db.update("recommendations", rec[-1]["id"],
                                      {"rec_type": "policy_event"})
                        text += (f"\nNext: fast-lane quick take done "
                                 f"(${out['cost_usd']:.2f}, {out['brief_path']}); "
                                 f"standard/full stays consent-gated.")
                    except Exception as e:
                        text += (f"\nNext: quick take pending (failed this cycle: "
                                 f"{str(e)[:80]}); will retry.")
                else:
                    text += "\nNext: standard/full stays consent-gated."
                alerts.append(truncate_telegram(text))
        elif classification_unusable(verdicts, parsed):
            # Model call failed or returned unusable text this cycle — send the
            # bare verifiable facts for items still genuinely retrying, capped,
            # rather than silence or garbage. Items that just hit MAX_RETRIES
            # are reported separately below (an honest "giving up," not another
            # "pending" promise).
            pending = [p["item"] for p in parsed
                      if not p["usable"] and p["item"] not in final_failures]
            for i in pending[:3]:
                alerts.append(truncate_telegram(minimal_alert(i)))
        # else: clean classification, no beneficiary trigger → stay silent.

        # Terminal failures are always surfaced (capped — same per-scan
        # batching as the pending/beneficiary alerts above, so credit-exhaustion
        # or a broken prompt can't spam beyond a few messages per scan) —
        # regardless of what else happened this cycle, since automated retry
        # has genuinely stopped for these filings.
        for item in final_failures[:3]:
            alerts.append(truncate_telegram(final_failure_alert(item, MAX_RETRIES)))

    # update the seen marker
    _set_system_flag(db, SEEN_FLAG, today)
    return {"scanned": len(items), "alerts": alerts}


if __name__ == "__main__":
    out = run_scan()
    for a in out["alerts"]:
        print(a, "\n")
    print(f"scanned {out['scanned']} fresh filings; {len(out['alerts'])} trigger(s)")
