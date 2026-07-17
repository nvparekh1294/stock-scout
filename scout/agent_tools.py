"""scout/agent_tools.py — the tools the conversational relay agent can call. Transport-agnostic: the Telegram bot (and any future desktop
relay) share these. Every tool is read-only or a benign write to Scout's own
store — there is NO order/execution tool, by construction (the project design).

A costly underwrite (standard/full) does not run inside a tool call; it sets a
pending action on the context so the transport can gate it behind an explicit
owner tap (consent + cost, the project design).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date as _date
from datetime import datetime, timedelta, timezone

from . import config, market_ref, reports, research
from .config import app_name, depth_cost_estimate
from .db import Database

# Tables the agent may read. No write access through query_db.
READABLE = {"theses", "break_conditions", "entry_triggers", "recommendations",
            "ledger_marks", "evidence", "constraints", "accounts", "lots",
            "api_costs"}
SYMBOL_TABLES = {"theses", "evidence", "lots"}        # have a symbol column
THESIS_TABLES = {"break_conditions", "entry_triggers", "recommendations"}  # link via thesis_id
QUADRANTS = {"rec_taken", "rec_passed", "advised_against_done", "own_idea"}


class ToolContext:
    """Carries the db and any pending consent-gated action for the transport."""
    def __init__(self, db: Database):
        self.db = db
        self.pending_underwrite: dict | None = None
        self.send_documents: list = []   # file paths the transport should attach


TOOL_SCHEMAS = [
    {"name": "resolve_ticker",
     "description": "Verify a symbol or company name against SEC EDGAR and the "
                    "Alpaca assets reference. Returns listed/not-found with "
                    "exchange and listing metadata. Call this BEFORE discussing "
                    "any ticker or company you have not already seen in this "
                    "conversation or in the database — verify first, then answer.",
     "input_schema": {"type": "object",
         "properties": {"query": {"type": "string",
             "description": "ticker or company name"}}, "required": ["query"]}},
    {"name": "query_db",
     "description": f"Read {app_name()}'s database. Returns rows as a JSON array, "
                    "read-only, PAGINATED: rows serialize whole (never cut "
                    "mid-row) up to a context budget, and when any row is omitted "
                    "a trailing {\"_meta\": ...} line states the total count and "
                    "the exact `offset` to re-query for the next page. Pass "
                    "`offset` to page forward. The constraints table renders "
                    "compact (id/theme/tier/status/prose/tickers) so the whole "
                    "confirmation queue usually fits in one call. ROW ORDER: rows "
                    "are id-ascending (oldest first); offset=0 is the oldest, not "
                    "the newest. To see the NEWEST rows of an append-heavy table "
                    "(api_costs, evidence, recommendations, ledger_marks), page to "
                    "the end using the `_meta` total, or use the dedicated "
                    "cost_report / list_ledger tools for spend/ledger questions.",
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string", "enum": sorted(READABLE)},
         "symbol": {"type": "string", "description": "optional ticker filter"},
         "offset": {"type": "integer", "default": 0,
                    "description": "0-based row to start the page at; use the "
                    "value from a prior call's _meta to fetch the next page"},
         "limit": {"type": "integer", "default": 50,
                   "description": "max rows per page (the context budget may "
                   "return fewer; a smaller value narrows further)"}},
         "required": ["table"]}},
    {"name": "get_brief",
     "description": "Return the latest saved thesis brief for a symbol (markdown, "
                    "truncated to ~3500 chars for context). To give the owner the "
                    "FULL brief on their phone, call send_brief instead/afterwards.",
     "input_schema": {"type": "object",
         "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "send_brief",
     "description": "Deliver the latest saved brief for a symbol to the owner's "
                    "Telegram as a tap-to-open HTML document (phone-readable). "
                    "Use whenever the owner asks for the full brief / to read a "
                    "brief on their phone, and after an underwrite completes. "
                    "FRESHNESS GATE: if the newest saved brief is OLDER than "
                    "today's data, this tool will NOT silently attach it — it "
                    "returns a note so you can offer to regenerate (state the "
                    "cost) or, only if they ask for the saved one, re-call with "
                    "allow_stale=true (it is then attached clearly labeled as "
                    "historical). Pass `tier` to match the depth they asked for "
                    "(e.g. 'standard brief').",
     "input_schema": {"type": "object",
         "properties": {"symbol": {"type": "string"},
             "tier": {"type": "string", "enum": ["quick", "standard", "full"],
                      "description": "optional depth to match the owner's request"},
             "allow_stale": {"type": "boolean",
                      "description": "only set true when the owner explicitly asks "
                      "for the older saved brief after being told it is stale"}},
         "required": ["symbol"]}},
    {"name": "run_underwrite",
     "description": "Underwrite a symbol. ALL depths are self-sufficient on any "
                    "resolvable ticker — they gather live evidence (no pre-built "
                    "pack required). depth=quick gathers a light snippet and runs "
                    "one Sonnet pass immediately (~$0.10–0.30; never free — always "
                    "state the estimate). depth=standard/full gather a FULL dated "
                    "pack (Sonnet gathering + Haiku extraction) then underwrite; "
                    "because gathering adds cost they are cost-gated "
                    "(~$1–3 standard, ~$5–15 full) — this tool queues an owner "
                    "cost-confirmation button and does NOT spend until they tap.",
     "input_schema": {"type": "object", "properties": {
         "symbol": {"type": "string"},
         "depth": {"type": "string", "enum": ["quick", "standard", "full"]}},
         "required": ["symbol", "depth"]}},
    {"name": "compare_symbols",
     "description": "Compare TWO symbols head-to-head: a deterministic quantitative "
                    "comps table (P/S, margins, growth, multiples) plus a cited "
                    "'case for each' and 'where they genuinely differ', from the "
                    "packs only. Requires pack/extraction data for BOTH; if either "
                    "is missing it reports the gather cost and asks before spending "
                    "(never auto-gathers). Runs a small Sonnet pass when data exists "
                    f"(~$0.05–0.20 — state it). NO buy verdict: {app_name()} compares, the "
                    "owner decides. Attaches a phone-readable HTML brief with a price "
                    "chart for each symbol.",
     "input_schema": {"type": "object", "properties": {
         "symbol_a": {"type": "string"},
         "symbol_b": {"type": "string"}},
         "required": ["symbol_a", "symbol_b"]}},
    {"name": "list_ledger",
     "description": "Summarize the decision ledger: recommendations, verdicts, "
                    "and whether the owner has logged a decision on each.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "add_holding",
     "description": "Record a portfolio lot. Accounts are identified by broker + "
                    "type only — never account numbers (privacy rule).",
     "input_schema": {"type": "object", "properties": {
         "broker": {"type": "string"},
         "account_type": {"type": "string", "enum": ["taxable", "ira", "401k"]},
         "symbol": {"type": "string"},
         "shares": {"type": "number"},
         "total_cost": {"type": "number"},
         "purchase_date": {"type": "string", "description": "YYYY-MM-DD"}},
         "required": ["broker", "account_type", "symbol", "shares", "total_cost"]}},
    {"name": "log_decision",
     "description": "Log the owner's decision on a recommendation (which ledger "
                    "quadrant, and a note).",
     "input_schema": {"type": "object", "properties": {
         "symbol": {"type": "string"},
         "quadrant": {"type": "string", "enum": sorted(QUADRANTS)},
         "note": {"type": "string"}},
         "required": ["symbol", "quadrant"]}},
    {"name": "note_own_idea",
     "description": "Log one of the owner's OWN ideas into the ledger (quadrant "
                    "own_idea) so it can be underwritten and tracked like any other.",
     "input_schema": {"type": "object", "properties": {
         "symbol": {"type": "string"},
         "note": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "confirm_constraint",
     "description": "Act on the radar's confirmation queue: confirm a candidate "
                    "constraint as thesis-eligible, or drop it. Use when the "
                    "owner says e.g. 'confirm constraint 3' / 'drop constraint 3'.",
     "input_schema": {"type": "object", "properties": {
         "constraint_id": {"type": "integer"},
         "decision": {"type": "string", "enum": ["confirm", "drop"]}},
         "required": ["constraint_id", "decision"]}},
    {"name": "tax_sell_plan",
     "description": "Build the specific-lot, LT-only tax-sell plan to raise a "
                    "target dollar amount (CA + federal rates, per-lot tax cost "
                    "shown). Deterministic; delayed prices; advice only — the "
                    "owner executes. Attaches the full plan as a document.",
     "input_schema": {"type": "object", "properties": {
         "target_usd": {"type": "number"}}, "required": ["target_usd"]}},
    {"name": "run_scorecard",
     "description": "Generate the monthly scorecard (all recs vs SPY, quadrant "
                    "decision-value, break-condition accuracy, cost report) and "
                    "attach it as a document. Near-zero cost (deterministic).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "run_radar",
     "description": f"Run the constraint radar NOW — {app_name()}'s new-IDEA generator. It "
                    "walks each theme's dependency tree, proposes candidate "
                    "constraints and the public companies at each tier into the "
                    "confirmation queue, and runs quick takes under any "
                    "already-confirmed constraints; the weekly memo is attached as "
                    "a document. This is the interactive twin of the scheduled "
                    "Monday radar: it runs SYNCHRONOUSLY (not batch) and costs "
                    "roughly $0.05–$1 depending on themes and pending quick takes — "
                    "state that, it is never free. Use when the owner asks to 'run "
                    "the radar', 'find new ideas', or 'scan for constraints'. Themes "
                    "default to the config list (radar.themes); pass `themes` only "
                    "to override for this one run.",
     "input_schema": {"type": "object", "properties": {
         "themes": {"type": "array", "items": {"type": "string"},
                    "description": "optional theme list to walk for this run; "
                    "omit to use the config default (radar.themes)"}}}},
    {"name": "triage_radar",
     "description": "Triage the radar's confirmation queue NOW: dedupe the pending "
                    "candidate constraints into DISTINCT, ranked stories using "
                    "FRESH run-date data — a cheap CURRENT snapshot (price, market "
                    "cap, and the latest 10-Q/10-K) for every queued ticker — then "
                    "suggest up to 3 next quick takes. It ranks by how binding each "
                    "constraint looks FORWARD from today, not how tight it was in "
                    "the past. Reads-only PLUS one cheap synthesis model call "
                    "(~$0.10–0.45 — state it, it is never free); it NEVER spends on "
                    "quick takes or underwrites, only reasons and ranks. The memo is "
                    "attached as a document. Use when the owner asks to 'triage the "
                    "radar', 'dedupe the queue', 'rank the constraints', or 'what's "
                    "the best idea in the queue'.",
     "input_schema": {"type": "object", "properties": {
         "max_stories": {"type": "integer",
             "description": "optional cap on distinct stories to surface (default 3)"}}}},
    {"name": "cost_report",
     "description": f"Summarize {app_name()}'s API spend over the last N days: total cost, "
                    "call count, token totals, and a breakdown by operation and by "
                    "model. Deterministic DB query over api_costs — no LLM spend to "
                    "run it. Use when the owner asks 'what have I spent', 'cost so "
                    "far', 'where is the money going', etc.",
     "input_schema": {"type": "object", "properties": {
         "days": {"type": "integer", "default": 30,
                  "description": "look-back window in days (default 30)"}}}},
]


_QUERY_DB_CHAR_BUDGET = 3500   # was a blind [:3500] slice; now a ROW-boundary budget


def _compact_constraint(row: dict) -> dict:
    """Compact one constraints row for query_db output: keep the queue-relevant
    columns and cap the prose, but ALWAYS carry the FULL ticker list. Tickers live
    only inside the description as a trailing ' | tickers: A,B,C' suffix (no
    separate column — see schema.sql), so truncating the description drops them
    (owner-reported 7/14). radar._split_description keeps prose/ticker splitting in
    one place."""
    from . import radar
    prose, suffix = radar._split_description(row.get("description"))
    tickers = suffix.split("tickers:", 1)[1].strip() if suffix else ""
    return {"id": row.get("id"), "theme": row.get("theme"),
            "tier": row.get("tier"), "status": row.get("status"),
            "prose": prose.strip()[:140], "tickers": tickers}


def _render_query_rows(table: str, rows: list, offset: int, limit: int) -> str:
    """Serialize `rows` as a JSON array starting at `offset`, one whole row at a
    time, stopping BEFORE the running length would exceed ~3500 chars — a row is
    never cut mid-way, and at least one row is always emitted. The constraints
    table renders compact (full ticker list preserved); every other table renders
    its full row. Whenever any row is omitted — paged past via `offset`, capped by
    `limit`, or dropped to stay inside the budget — an honest trailing
    {"_meta": ...} line is appended with the total count and the exact offset to
    re-query. A small, complete result renders as a plain JSON array with NO _meta
    (identical to the old slice's output)."""
    total = len(rows)
    start = max(0, int(offset or 0))
    cap = int(limit) if limit else None
    rendered: list[str] = []
    used, shown = 2, 0                      # `used` seeds the surrounding "[]"
    for row in rows[start:]:
        if cap is not None and shown >= cap:
            break
        cell = json.dumps(_compact_constraint(row) if table == "constraints"
                          else row, default=str)
        add = len(cell) + (2 if rendered else 0)     # ", " item separator
        if rendered and used + add > _QUERY_DB_CHAR_BUDGET:
            break
        rendered.append(cell)
        used += add
        shown += 1
    body = "[" + ", ".join(rendered) + "]"
    more_after = (start + shown) < total
    if start == 0 and not more_after:
        return body                          # complete from the top — no _meta
    if shown == 0:
        meta = f"no rows at offset {start}; {total} row(s) total"
    else:
        meta = f"showing rows {start + 1}-{start + shown} of {total}"
        if more_after:                        # only advertise a next page if one exists
            meta += f"; re-query with offset={start + shown}"
    return body + "\n" + json.dumps({"_meta": meta})


_TICKER_PREFIX = __import__("re").compile(r"^[A-Z0-9.]{1,6}-")


def _op_label(task: str) -> str:
    """Group per-symbol tasks into an operation (strip a leading ticker prefix):
    'QMEM-underwrite' and 'ZYXA-underwrite' both aggregate as 'underwrite';
    'radar-walk-...' and 'standard (batch)' are left as-is."""
    return _TICKER_PREFIX.sub("", task or "?").strip() or "?"


def _tok(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def cost_report(db: Database, days: int = 30) -> str:
    """Deterministic spend summary over api_costs for the last `days` days —
    total, call count, token totals, and a breakdown by operation and by model.
    Zero LLM involvement. Formatted as a concise Telegram-friendly text block."""
    days = max(1, int(days or 30))
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    total_usd = 0.0
    n_calls = tot_in = tot_out = tot_cached = 0
    by_op: dict = defaultdict(lambda: {"usd": 0.0, "n": 0, "in": 0, "out": 0})
    by_model: dict = defaultdict(
        lambda: {"usd": 0.0, "n": 0, "in": 0, "out": 0, "cached": 0})

    for row in db.select("api_costs"):
        if str(row.get("ts", "")) < cutoff_iso:
            continue
        usd = float(row.get("usd_estimate") or 0.0)
        ti = int(row.get("input_tokens") or 0)
        to = int(row.get("output_tokens") or 0)
        tc = int(row.get("cached_tokens") or 0)
        total_usd += usd
        n_calls += 1
        tot_in += ti
        tot_out += to
        tot_cached += tc
        op = by_op[_op_label(str(row.get("task") or "?"))]
        op["usd"] += usd
        op["n"] += 1
        op["in"] += ti
        op["out"] += to
        bm = by_model[str(row.get("model") or "?")]
        bm["usd"] += usd
        bm["n"] += 1
        bm["in"] += ti
        bm["out"] += to
        bm["cached"] += tc

    since = cutoff_iso[:10]
    if n_calls == 0:
        return f"No API spend recorded in the last {days}d (since {since})."

    # Token-first reporting: every line leads with tokens, then the USD
    # estimate ("N in / M out ≈ $X at your configured rates"). Dollars are an
    # ESTIMATE from the config pricing table, never a bill — say so once up top.
    lines = [f"📊 API cost — last {days}d (since {since})",
             f"{_tok(tot_in)} in / {_tok(tot_out)} out ≈ ${total_usd:.2f} at your "
             f"configured rates · {n_calls} calls · {_tok(tot_cached)} cached"]

    ops = sorted(by_op.items(), key=lambda kv: -kv[1]["usd"])
    lines.append("")
    lines.append("By operation:" if len(ops) <= 8 else "By operation (top 8 by $):")
    for name, v in ops[:8]:
        lines.append(f"  {name}: {_tok(v['in'])} in / {_tok(v['out'])} out ≈ "
                     f"${v['usd']:.2f} ({v['n']})")

    lines.append("")
    lines.append("By model:")
    for name, v in sorted(by_model.items(), key=lambda kv: -kv[1]["usd"]):
        lines.append(f"  {name}: {_tok(v['in'])} in / {_tok(v['out'])} out ≈ "
                     f"${v['usd']:.2f} ({v['n']})")

    warning = config.pricing_staleness_warning()
    if warning:
        lines.append("")
        lines.append(warning)
    return "\n".join(lines)


def dispatch(name: str, ti: dict, ctx: ToolContext) -> str:
    db = ctx.db
    try:
        if name == "resolve_ticker":
            return json.dumps(market_ref.resolve_ticker(ti["query"]), default=str)

        if name == "query_db":
            table = ti["table"]
            if table not in READABLE:
                return f"error: table {table!r} is not readable"
            sym = (ti.get("symbol") or "").upper().strip()
            limit = ti.get("limit", 50)
            offset = ti.get("offset", 0)
            if not sym:
                rows = db.select(table, order_by="id")
            elif table in SYMBOL_TABLES:
                rows = db.select(table, {"symbol": sym}, order_by="id")
            elif table in THESIS_TABLES:
                # link via thesis_id — NEVER fall back to unfiltered (that
                # mis-attributes other symbols' rows to this one).
                tids = {t["id"] for t in db.select("theses", {"symbol": sym})}
                rows = sorted(
                    (r for r in db.select(table) if r.get("thesis_id") in tids),
                    key=lambda r: (r.get("id") is None, r.get("id")))
            else:
                return (f"note: the {table} table is not per-symbol. Query "
                        f"'theses' or 'recommendations' for {sym} instead, or "
                        f"re-query {table} without a symbol filter.")
            return _render_query_rows(table, rows, offset, limit)

        if name == "get_brief":
            sym = ti["symbol"].upper()
            files = sorted(reports.BRIEFS_DIR.glob(f"{sym}_*.md"))
            if not files:
                return f"No saved brief for {sym}. Offer to run an underwrite."
            return files[-1].read_text()[:3500]

        if name == "send_brief":
            sym = ti["symbol"].upper()
            tier = (ti.get("tier") or "").strip().lower() or None
            allow_stale = bool(ti.get("allow_stale"))
            latest = reports.latest_brief(sym, tier=tier)
            if not latest and tier:
                # asked for a specific tier we don't have — fall back to any tier
                latest = reports.latest_brief(sym)
            if not latest:
                return f"No saved brief for {sym}. Offer to run an underwrite."
            fresh = reports.assess_freshness(latest)
            if fresh["stale"] and not allow_stale:
                # Never silently attach a brief older than today's data (the
                # stale-brief incident class). Prefer a fresh underwrite; state
                # the cost (per the project design).
                got_tier = reports.brief_tier(latest)
                est = reports.tier_cost_estimate(tier or got_tier)
                return (f"The newest saved {sym} brief ({latest.name}) is dated "
                        f"{fresh['as_of']} — older than today's data "
                        f"({fresh['today']}). I won't silently send a stale brief: "
                        f"its body is the analysis of record for {fresh['as_of']}, "
                        f"but the header cards would render live and could disagree "
                        f"with it. Regenerating a fresh {tier or got_tier} brief "
                        f"costs ~${est}. Ask the owner: a fresh one, or the saved "
                        f"{fresh['as_of']} brief clearly labeled as historical? "
                        f"(Nothing spent yet. If they want the saved one, re-call "
                        f"send_brief with allow_stale=true.)")
            html = reports.html_for_brief(latest, db=db)
            ctx.send_documents.append(str(html))
            if fresh["stale"]:
                return (f"Queued the SAVED {sym} brief ({latest.name}) as an HTML "
                        f"document, clearly labeled historical — as-of "
                        f"{fresh['as_of']}, header data live as of {fresh['today']}. "
                        f"Tell the owner the body is the {fresh['as_of']} analysis "
                        f"of record; live header cards may disagree.")
            return (f"Queued the full {sym} brief ({latest.name}) as an HTML "
                    f"document — it will attach to this reply. Tell the owner "
                    f"to tap it to read the full brief.")

        if name == "run_underwrite":
            sym, depth = ti["symbol"].upper(), ti["depth"]
            if depth == "quick":
                # self-sufficient — costs ~$0.10–0.30 (never free); state actual.
                out = research.underwrite(sym, depth="quick", db=db)
                return (f"Quick take done — actual cost ${out['cost_usd']:.4f}; "
                        f"checkers {'passed' if out['checker_passed'] else 'FLAGGED'}. "
                        f"Brief: {out['brief_path']}")
            # standard/full are self-sufficient (they gather a full pack live if
            # none is curated). Always cost-gate — gathering makes them pricier.
            has_pack = True
            try:
                research.find_pack(sym, None)
            except FileNotFoundError:
                has_pack = False
            # Estimate comes from the config pricing table (depth_tiers.
            # usd_estimate), so it can never drift from what config documents.
            est = depth_cost_estimate(depth)
            gather_note = "" if has_pack else " (includes live evidence gathering)"
            ctx.pending_underwrite = {"symbol": sym, "depth": depth, "est": est}
            return (f"A {depth} underwrite of {sym} costs ~${est}{gather_note}. "
                    f"I've queued a confirmation button for the owner — it will "
                    f"not run until they tap Confirm.")

        if name == "compare_symbols":
            a, b = ti["symbol_a"].upper(), ti["symbol_b"].upper()
            out = research.compare(a, b, db=db)
            if out.get("needs_gather"):
                return out["message"]  # cost-consent ask — nothing was spent
            ctx.send_documents.append(out["html_path"])
            flag = "passed" if out["checker_passed"] else "FLAGGED — review"
            return (f"Compared {a} vs {b} — actual cost ${out['cost_usd']:.4f}; "
                    f"checkers {flag}. Comps table + a cited case-for-each and "
                    f"where-they-differ. No buy verdict — you decide. HTML with a "
                    f"price chart for each is attached (tap to open).")

        if name == "list_ledger":
            recs = db.select("recommendations", order_by="id")
            out = []
            for r in recs:
                th = db.select_one("theses", {"id": r.get("thesis_id")}) or {}
                out.append({"symbol": th.get("symbol"), "rec_type": r.get("rec_type"),
                            "rec_date": r.get("rec_date"), "quadrant": r.get("quadrant"),
                            "decision": r.get("decision")})
            return json.dumps(out, default=str)[:3500] or "ledger empty"

        if name == "add_holding":
            acct = db.select_one("accounts", {"name": ti["broker"],
                                              "type": ti["account_type"]})
            acct_id = acct["id"] if acct else db.insert(
                "accounts", {"name": ti["broker"], "type": ti["account_type"]})
            db.insert("lots", {
                "account_id": acct_id, "symbol": ti["symbol"].upper(),
                "purchase_date": ti.get("purchase_date"),
                "shares": ti["shares"], "total_cost": ti["total_cost"],
                "source": "telegram", "import_confirmed": False})
            return (f"Recorded {ti['shares']} {ti['symbol'].upper()} @ "
                    f"${ti['total_cost']} in {ti['broker']}/{ti['account_type']}.")

        if name == "log_decision":
            sym = ti["symbol"].upper()
            th = None
            for t in db.select("theses", {"symbol": sym}):
                th = t
            if not th:
                return f"No thesis on {sym} to attach a decision to."
            rec = None
            for r in db.select("recommendations", {"thesis_id": th["id"]}):
                rec = r
            if not rec:
                return f"No recommendation on {sym} to log against."
            db.update("recommendations", rec["id"], {
                "quadrant": ti["quadrant"], "decision": ti["quadrant"],
                "decision_date": _date.today().isoformat(),
                "decision_note": ti.get("note", "")[:400]})
            return f"Logged {sym} decision as {ti['quadrant']}."

        if name == "note_own_idea":
            sym = ti["symbol"].upper()
            tid = db.insert("theses", {"symbol": sym, "verdict": "OWN_IDEA",
                                       "status": "watch",
                                       "thesis_text": ti.get("note", "")[:2000]})
            db.insert("recommendations", {
                "thesis_id": tid, "rec_type": "own_idea",
                "rec_date": _date.today().isoformat(), "quadrant": "own_idea",
                "decision_note": ti.get("note", "")[:400]})
            return (f"Logged {sym} as your own idea (ledger quadrant own_idea). "
                    f"Want me to run an underwrite on it?")

        if name == "confirm_constraint":
            cid = int(ti["constraint_id"])
            row = db.select_one("constraints", {"id": cid})
            if not row:
                return f"No constraint with id {cid} in the queue."
            if ti["decision"] == "confirm":
                db.update("constraints", cid, {"confirmed_by_owner": True,
                                               "status": "confirmed"})
                return (f"Constraint {cid} confirmed — its tickers are now "
                        f"eligible for radar quick takes: "
                        f"{(row.get('description') or '')[:160]}")
            db.update("constraints", cid, {"status": "dropped"})
            return f"Constraint {cid} dropped from the queue."

        if name == "tax_sell_plan":
            from . import tax_plan
            from .config import REPO_ROOT
            plan = tax_plan.build_plan(db, float(ti["target_usd"]))
            text = tax_plan.render_plan(plan)
            path = REPO_ROOT / "briefs" / f"tax_plan_{_date.today().isoformat()}.md"
            path.write_text(text)
            ctx.send_documents.append(str(reports.html_for_brief(path, db=db)))
            return (f"Plan built: raises ${plan['raised']:,.0f} across "
                    f"{len(plan['lots'])} LT lots, est. tax ${plan['est_tax']:,.0f}. "
                    f"Full per-lot table attached as a document. Rates are stated "
                    f"assumptions — remind the owner to check them.")

        if name == "run_scorecard":
            from . import scorecard
            path = scorecard.write_scorecard(db)
            ctx.send_documents.append(str(reports.html_for_brief(path, db=db)))
            return "Scorecard generated and attached as a document."

        if name == "run_radar":
            from . import radar
            themes = ti.get("themes") or None
            # SYNCHRONOUS on the interactive path (batch is only for scheduled
            # work — the project design). The monthly-cap guard still fires inside
            # run_weekly's llm calls. Delivery uses the SAME shared helper as the
            # scheduled Monday job, so the owner gets the identical HTML memo.
            d = radar.prepare_delivery(db, themes=themes, use_batch=False)
            ctx.send_documents.append(str(d["html_path"]))
            out = d["out"]
            return (f"Radar run — {out['new_candidates']} new candidate(s) proposed, "
                    f"{out['queue']} now awaiting your confirmation. Memo attached "
                    f"(tap to open); its cost snapshot shows the actual API spend "
                    f"this run. Say e.g. 'confirm constraint 3' to graduate one.")

        if name == "triage_radar":
            from . import triage
            # SYNCHRONOUS + one CHEAP (quick-tier) synthesis call, mirroring
            # run_radar's shared-delivery shape. reads-only — the snapshot reuses
            # existing light helpers, and NOTHING is spent on quick takes or
            # underwrites. The monthly-cap guard still fires inside llm.call.
            d = triage.prepare_delivery(db, max_stories=ti.get("max_stories"))
            out = d["out"]
            if out.get("error"):
                # FAILURE: the synthesis returned no text even after the retry.
                # No memo, no attachment — surface the truth to the owner, never a
                # hollow "(no synthesis)" memo delivered as success (07-14 bug).
                return d["message"]
            ctx.send_documents.append(str(d["html_path"]))
            if out["queue"] == 0:
                return ("The radar confirmation queue is empty — nothing to "
                        "triage. Run the radar first (run_radar) to propose "
                        "candidate constraints. (No model spend.)")
            return (f"Radar triage done — {out['queue']} candidate constraint(s) "
                    f"across {out['tickers']} ticker(s), collapsed into ranked "
                    f"stories; actual cost ${out['cost_usd']:.4f}. Memo attached "
                    f"(tap to open) — it ranks by forward-looking bindingness off a "
                    f"run-date snapshot and ends with up to 3 suggested next quick "
                    f"takes. Nothing was spent on quick takes or underwrites.")

        if name == "cost_report":
            return cost_report(db, int(ti.get("days") or 30))

        return f"error: unknown tool {name!r}"
    except Exception as e:  # never crash the relay on a tool error
        return f"tool error in {name}: {e}"
