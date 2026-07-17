"""scout/triage.py — the radar-triage tool (built 2026-07-14).

The radar (radar.py) PROPOSES candidate constraints into the confirmation queue;
over time that queue accumulates overlapping ideas across themes. triage_radar
COLLAPSES the queue into distinct, ranked stories in ONE cheap pass — and, per
the owner's requirement, it reasons from CURRENT (run-date) data and a
forward-looking frame, never from the model's training-era memories.

Shape (deliberately mirrors radar.py so the two never drift):
  - run_triage(db, max_stories)  — load the queue, build a CHEAP current snapshot
    per unique ticker from EXISTING light helpers (no full gather, no underwrite),
    make ONE synthesis call on the quick/cheap model tier through llm.call (cost
    logged, monthly cap enforced), and write a markdown memo to briefs/.
  - prepare_delivery(...)        — the single Telegram payload the agent tool
    renders from (message text + HTML memo), exactly like radar.prepare_delivery.

The snapshot is GROUND TRUTH handed to the model for anything present-tense; the
prompt forbids presenting pre-cutoff conditions as current and makes it label
any reasoning beyond the snapshot as a hypothesis.

Reuses existing modules only (market_ref / fundamentals / gather) in their
lightest form. Per-ticker failures degrade to "n/a" and NEVER abort the triage.
Read-only. Contains NO order/execution code.
"""

from __future__ import annotations

import argparse
from datetime import date

from . import llm, radar
from .config import REPO_ROOT, app_name
from .db import Database

TRIAGE_MAX_STORIES = 3   # queue quality bar is a few distinct stories, not a list
SNAPSHOT_TICKER_CAP = 40  # bound the per-ticker live-fetch snapshot loop

# Synthesis output budget. This is claude-sonnet-5, which emits THINKING blocks
# by default and THINKING TOKENS COUNT AGAINST max_tokens. The 2026-07-14 incident:
# at production input size (30 queue rows + 40-ticker snapshot ≈ 5.3k input tokens)
# thinking consumed ~2150+ of a 2500 budget, the run hit stop_reason=max_tokens
# with little or NO text, and an empty synthesis was silently DELIVERED as a memo.
# 8000 leaves ample room for thinking PLUS a full memo; if a run still comes back
# truncated/empty we retry ONCE at 1.5x (mirrors config.yml standard tier +
# research.underwrite's truncation auto-retry).
TRIAGE_MAX_TOKENS = 8000
TRIAGE_RETRY_MAX_TOKENS = 12000   # 1.5x — the single truncation/empty retry


TRIAGE_SYSTEM = """You are Scout's radar-triage pass. You are handed the confirmation
queue of CANDIDATE constraints (each constraint lists the public tickers that
would capture it) plus a CURRENT snapshot table of every distinct ticker (company
name, price, market cap, latest periodic filing) fetched TODAY. Your job: collapse
the queue into DISTINCT stories and RANK them.

Ranking rule — order the stories by, in priority:
  (1) how BINDING the underlying constraint looks looking FORWARD from today —
      what breaks next as the theme keeps scaling — NOT how famously tight it was
      in the past; then
  (2) how DIRECTLY a listed company in the queue captures it.

Ground-truth rule: the supplied snapshot is the ONLY current data you have. Use
it as ground truth for anything present-tense (price, size, most recent filing).
Where your reasoning goes beyond the snapshot, LABEL it explicitly as
"(hypothesis)". NEVER present a pre-training-cutoff condition as if it were
current; if you are unsure whether something still holds, say so and defer to
fresh data. A snapshot value of "n/a" means Scout could not fetch it this run —
say so, do not fill the gap from memory.

Output a concise markdown memo, no prose outside it:
  # Radar triage — <today's date>
  ## Distinct stories (ranked)
  Render EACH story under its OWN `###` heading so the memo is skimmable on a
  phone: `### <rank>. <one-line title>`. Under that heading give: the constraint
  it rests on; the queue tickers that capture it (most direct first); a one-line
  FORWARD binding read; and any (hypothesis) flags. On the FIRST mention of a
  company in each story, write it as "Name (TICKER)" using the company column of
  the snapshot (fall back to just the TICKER if the snapshot company name is
  "n/a").
  ## Suggested next quick takes
  At most 3 tickers worth a fresh quick take, each with a one-line why.

No trading advice. An analyst figure is context, never an expected return."""

# Route the instance display name through config (no hard-coded product name);
# TRIAGE_SYSTEM carries the name in two spots, both replaced here at import.
TRIAGE_SYSTEM = TRIAGE_SYSTEM.replace("Scout", app_name())


def _queue_rows(db: Database) -> list[dict]:
    """The full confirmation queue — unconfirmed candidate constraints, in id
    order (the order the owner sees them and confirms them by)."""
    return [c for c in db.select("constraints", order_by="id")
            if c.get("status") == "candidate" and not c.get("confirmed_by_owner")]


def _row_tickers(row: dict) -> list[str]:
    """The ticker list stored inline in one constraint's description suffix."""
    _, suffix = radar._split_description(row.get("description"))
    if not suffix:
        return []
    return [t.strip().upper()
            for t in suffix.split("tickers:", 1)[1].split(",") if t.strip()]


def _queue_tickers(rows: list[dict]) -> list[str]:
    """Every distinct ticker across the queue, deduped, first-seen order."""
    out: list[str] = []
    for r in rows:
        for t in _row_tickers(r):
            if t not in out:
                out.append(t)
    return out


def _ticker_snapshot(symbol: str, resolved: dict | None = None) -> dict:
    """A CHEAP current snapshot for one ticker, from EXISTING light helpers only —
    NO full gather, NO underwrite:
      - name        : market_ref.resolve_ticker's company name (EDGAR title /
                      Alpaca name) — already fetched here, so the column is free
      - price       : fundamentals.price_latest (Alpaca IEX latest close)
      - market_cap  : price × EDGAR company-facts shares outstanding
      - latest_filing: gather._latest_quarterly (most recent 10-Q/10-K, date+type)
      - active / sec_filer / identity_warning / alpaca_name : the resolve_ticker
        identity flags (2026-07-15 stale-ticker fix) so rendering can flag a
        reassigned ticker (PHTN) or a delisted one (ARJT) instead of passing a
        wrong-company price to the model.

    `resolved` lets the caller hand in the resolve_ticker dict it ALREADY fetched
    this run (each queue ticker is resolved once, up front) so we never double-
    resolve. When omitted, we resolve here (the ad-hoc/unit path).

    Every field degrades to "n/a" on any failure and a per-ticker error is
    swallowed so it can never abort the whole triage. (A next-earnings DATE has no
    cheap existing helper, so it is intentionally omitted — see the build report.)
    """
    from . import fundamentals, gather, market_ref
    snap = {"symbol": symbol, "name": "n/a", "price": "n/a", "market_cap": "n/a",
            "latest_filing": "n/a", "active": None, "sec_filer": None,
            "identity_warning": None, "alpaca_name": None}
    cik = None
    res = resolved
    if res is None:
        try:
            res = market_ref.resolve_ticker(symbol)
        except Exception:
            res = None
    if res:
        # Identity flags carry through whether or not the ticker resolved cleanly.
        snap["active"] = res.get("active")
        snap["sec_filer"] = res.get("sec_filer")
        snap["identity_warning"] = res.get("identity_warning")
        snap["alpaca_name"] = res.get("alpaca_name")
        if res.get("resolved"):
            cik = res.get("cik")
            if res.get("name"):
                snap["name"] = res["name"]
    try:
        px = fundamentals.price_latest(symbol)
        if px:
            snap["price"] = px
    except Exception:
        pass
    if cik:
        try:
            facts = fundamentals.company_facts_metrics(int(cik))
            shares = (facts or {}).get("shares")
            if shares and isinstance(snap["price"], (int, float)):
                snap["market_cap"] = round(snap["price"] * shares)
        except Exception:
            pass
        try:
            lq = gather._latest_quarterly(int(cik))
            if lq:
                snap["latest_filing"] = f"{lq['form']} {lq['date']}"
        except Exception:
            pass
    return snap


def _resolve_safe(symbol: str) -> dict:
    """resolve_ticker for one ticker, fail-OPEN to {} on any error. A transient
    lookup failure must never retire a ticker or abort the triage — only an
    explicit Alpaca active==False retires (see run_triage)."""
    from . import market_ref
    try:
        return market_ref.resolve_ticker(symbol) or {}
    except Exception:
        return {}


def _fmt_num(v) -> str:
    if isinstance(v, (int, float)):
        return f"${v:,}" if v >= 1000 else f"${v}"
    return str(v)


def _snapshot_table(snaps: list[dict], today: str) -> str:
    """Render the per-ticker snapshot as a MARKDOWN PIPE TABLE (not a code fence).

    Why a pipe table: this same string is BOTH the model's ground-truth input AND
    the block persisted into the memo, and reports.html_for_brief renders pipe
    tables into a real <table> but has no code-fence handling — a fenced block
    rendered as garbage `<p>```</p>` lines in the 07-14 artifact. A pipe table is
    equally legible to the model and renders as a clean phone-readable table.

    The run date is embedded in the caption so the model (and the saved memo)
    always know the data's as-of."""
    lines = [f"CURRENT SNAPSHOT (fetched {today} — ground truth for anything "
             f"present-tense; 'n/a' = not fetched this run, do NOT fill from "
             f"memory). Any row whose company cell is marked ⚠ is IDENTITY-SUSPECT "
             f"(the ticker is inactive/delisted, or may have been reassigned to a "
             f"DIFFERENT security than the queue intends): do NOT treat its "
             f"price/size/filing as data for the intended queue company — say so "
             f"and set it aside.",
             "",
             "| ticker | company | price | market cap | latest filing |",
             "| --- | --- | --- | --- | --- |"]
    for s in snaps:
        cells = _snapshot_cells(s)
        # A stray pipe in an EDGAR/Alpaca title would break the table split.
        cells = [c.replace("|", "/") for c in cells]
        lines.append(f"| {s['symbol']} | {cells[0]} | {cells[1]} | "
                     f"{cells[2]} | {cells[3]} |")
    return "\n".join(lines)


def _snapshot_cells(s: dict) -> list[str]:
    """Deterministic (company, price, market cap, latest filing) cells for one
    snapshot row — the identity flags decide the text, no LLM judgment (2026-07-15
    stale-ticker fix):

      * active is False (ARJT shape — delisted/inactive) → every data cell reads
        "n/a (inactive/delisted)" and the company cell keeps the name plus a
        ⚠ inactive/delisted tag. There is no live data for a delisted ticker.
      * sec_filer is False but the security is active (PHTN shape — the ticker was
        REASSIGNED to a different Alpaca-listed security than the queue intends) →
        the price cell reads "n/a — identity unverified" so the fetched price is
        NEVER shown as if it were the queue company's, and the company cell shows
        the ALPACA-listed name plus a ⚠ possible ticker reassignment — verify tag.

    Any other row (a clean filer, or a row whose flags are unknown/None) renders
    exactly as before."""
    active = s.get("active")
    sec_filer = s.get("sec_filer")
    raw_name = str(s.get("name") or "n/a")
    if active is False:
        return [f"{raw_name} ⚠ inactive/delisted", "n/a (inactive/delisted)",
                "n/a (inactive/delisted)", "n/a (inactive/delisted)"]
    if sec_filer is False:
        # Attribute to the Alpaca-listed instrument, not the assumed company.
        alpaca_name = str(s.get("alpaca_name") or raw_name)
        return [f"{alpaca_name} ⚠ possible ticker reassignment — verify",
                "n/a — identity unverified",
                _fmt_num(s["market_cap"]), str(s["latest_filing"])]
    return [raw_name, _fmt_num(s["price"]), _fmt_num(s["market_cap"]),
            str(s["latest_filing"])]


def _queue_block(rows: list[dict]) -> str:
    lines = []
    for c in rows:
        prose, _ = radar._split_description(c.get("description"))
        tickers = ",".join(_row_tickers(c)) or "(none listed)"
        lines.append(f"- [{c['id']}] tier {c.get('tier')} · {c.get('theme')}: "
                     f"{prose.strip()} | tickers: {tickers}")
    return "\n".join(lines) or "- (queue empty)"


def _synthesis_incomplete(r: dict) -> bool:
    """A synthesis result that must be retried at a larger budget: the model hit
    its token ceiling (max_tokens stop) OR came back empty / suspiciously short
    (<200 chars). Both are symptoms of the 07-14 thinking-token squeeze."""
    text = (r.get("text") or "").strip()
    return r.get("stop_reason") == "max_tokens" or len(text) < 200


def run_triage(db: Database | None = None, max_stories: int | None = None) -> dict:
    """Dedupe + rank the confirmation queue off a fresh run-date snapshot in one
    cheap synthesis call. Returns {memo, path, queue, tickers, cost_usd}."""
    db = db or Database()
    db.apply_schema()
    today = date.today().isoformat()
    cap = int(max_stories) if max_stories else TRIAGE_MAX_STORIES

    rows = _queue_rows(db)
    if not rows:
        memo = (f"# Radar triage — {today}\n\n"
                "The confirmation queue is empty — nothing to triage. Run the "
                "radar first (run_radar) to propose candidate constraints.\n")
        path = REPO_ROOT / "briefs" / f"triage_{today}.md"
        path.write_text(memo)
        return {"memo": memo, "path": str(path), "queue": 0, "tickers": 0,
                "cost_usd": 0.0}

    tickers = _queue_tickers(rows)
    # Resolve each queue ticker ONCE, up front (bounded like the snapshot loop it
    # feeds), so identity is checked before any fetch and no ticker is resolved
    # twice (2026-07-15 stale-ticker fix). A ticker Alpaca reports
    # inactive/delisted (active is False) is RETIRED: excluded from the snapshot
    # fetch and surfaced to the owner. Only an explicit False retires — a resolve
    # error / unknown fails OPEN (kept) so a transient outage never silently drops
    # a live ticker. DB queue rows are NEVER rewritten or deleted here.
    resolve_pool = tickers[:SNAPSHOT_TICKER_CAP]
    resolved_map = {t: _resolve_safe(t) for t in resolve_pool}
    retired = [(t, resolved_map[t]) for t in resolve_pool
               if resolved_map[t].get("active") is False]
    retired_syms = {t for t, _ in retired}
    snapshot_tickers = [t for t in resolve_pool if t not in retired_syms]
    snaps = [_ticker_snapshot(t, resolved_map[t]) for t in snapshot_tickers]
    table = _snapshot_table(snaps, today)
    if len(tickers) > SNAPSHOT_TICKER_CAP:
        # Report the actual number fetched this run (inactive/delisted tickers
        # are excluded from the fetch), not the raw cap (2026-07-15 stale-ticker
        # fix): otherwise the count overstates coverage whenever any ticker was
        # retired below.
        table += (f"\n\n(snapshot covered {len(snapshot_tickers)} of "
                  f"{len(tickers)} tickers)")
    if retired:
        # Tell the model plainly why these queue tickers have no snapshot row.
        table += ("\n\n(excluded as inactive/delisted, not fetched: "
                  + ", ".join(t for t, _ in retired) + ")")

    user = (f"Today is {today}. Collapse the confirmation queue below into at most "
            f"{cap} distinct, ranked stories, then suggest up to 3 next quick "
            f"takes. Use the snapshot as ground truth for anything current.\n\n"
            f"## Confirmation queue ({len(rows)} candidate constraints)\n"
            f"{_queue_block(rows)}\n\n## {table}")

    r = llm.call("radar-triage", "sonnet",
                 [{"role": "user", "content": user}],
                 max_tokens=TRIAGE_MAX_TOKENS, system=TRIAGE_SYSTEM, db=db)
    cost = r["usd"]
    retry_used = False
    if _synthesis_incomplete(r):
        # Mirror research.underwrite's truncation auto-retry: one retry at 1.5x
        # the budget. Both call costs are logged by llm.call; we always adopt the
        # retry's result (it is >= the first in completeness). See the 07-14
        # incident note on TRIAGE_MAX_TOKENS.
        r = llm.call("radar-triage-retry", "sonnet",
                     [{"role": "user", "content": user}],
                     max_tokens=TRIAGE_RETRY_MAX_TOKENS, system=TRIAGE_SYSTEM, db=db)
        cost += r["usd"]
        retry_used = True

    memo_body = (r["text"] or "").strip()
    if not memo_body:
        # FAILURE — the model produced no text even after the larger retry (only
        # thinking tokens). This is NOT a memo: write no file, return an error the
        # delivery path surfaces to the owner as a failure. The old
        # "(no synthesis returned)" fallback that masqueraded as a memo is GONE.
        return {"error": ("Radar triage synthesis returned no text after a retry "
                          "at the larger token budget — the model produced only "
                          "thinking tokens and no memo. Nothing was written."),
                "queue": len(rows), "tickers": len(tickers),
                "cost_usd": round(cost, 4), "retry_used": retry_used}

    # Persist the snapshot alongside the memo so the artifact carries its own
    # dated ground truth (the model saw this exact table). No code fence — the
    # table is emitted as markdown so html_for_brief renders a real <table>.
    # Surface retired (inactive/delisted) tickers to the owner ONCE, plainly, so a
    # stale queue entry is visible without the owner digging (2026-07-15). The DB
    # queue rows are deliberately left untouched — this is a read-only heads-up.
    retired_note = ""
    if retired:
        retired_note = ("\n\n## Retired tickers (dropped from this triage)\n\n"
                        + "\n".join(
                            f"- {t} — inactive/delisted; dropped from this "
                            f"triage; the queue entry is stale (Alpaca "
                            f"status={r.get('status') or 'n/a'}). Its DB queue "
                            f"row was left untouched."
                            for t, r in retired))
    memo = (memo_body + retired_note
            + "\n\n---\n\n## Snapshot used (fetched " + today + ")\n\n"
            + table + "\n")
    path = REPO_ROOT / "briefs" / f"triage_{today}.md"
    path.write_text(memo)
    return {"memo": memo, "path": str(path), "queue": len(rows),
            "tickers": len(tickers), "cost_usd": round(cost, 4),
            "retry_used": retry_used}


def prepare_delivery(db: Database | None = None,
                     max_stories: int | None = None) -> dict:
    """Build the ONE Telegram delivery payload the triage_radar tool renders from
    (message text + HTML memo) — mirrors radar.prepare_delivery so the shape stays
    consistent with run_radar. Returns {"out": <run_triage dict>, "message": str,
    "html_path": Path}."""
    from . import reports
    out = run_triage(db=db, max_stories=max_stories)
    if out.get("error"):
        # No synthesis after retry: FAILURE, not a memo. No HTML attachment — the
        # owner hears the truth, never a hollow memo. (07-14 root cause.)
        message = (f"🧭 Radar triage failed to produce a synthesis (model returned "
                   f"no text after retry, cost ${out['cost_usd']:.2f}) - try again "
                   f"or ask Claude to investigate")
        return {"out": out, "message": message, "html_path": None}
    html = reports.html_for_brief(out["path"], db=db)
    if out["queue"] == 0:
        message = ("🧭 Radar triage — the confirmation queue is empty; nothing to "
                   "rank yet. Run the radar to propose candidates.")
    else:
        message = (f"🧭 Radar triage done — {out['queue']} candidate constraint(s) "
                   f"across {out['tickers']} ticker(s), collapsed into ranked "
                   f"stories off a run-date snapshot. Memo attached "
                   f"(cost ${out['cost_usd']:.2f}).")
    return {"out": out, "message": message, "html_path": html}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scout radar triage")
    ap.add_argument("--max-stories", type=int, default=None)
    args = ap.parse_args()
    out = run_triage(max_stories=args.max_stories)
    print(out["memo"])
    print(f"\nmemo written: {out['path']}")
