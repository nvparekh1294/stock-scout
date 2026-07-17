"""scout/scorecard.py — the monthly report card.

Answers two separate questions from the decision ledger: (1) were the analyst's
ideas good (recommended vs market, taken vs passed), and (2) were the owner's
decisions good (did overriding the analyst add or subtract value). Plus break-
condition accuracy and the month's API cost. Small-sample honesty: counts are
stated and confidence language stays humble until the ledger has months of data.

Deterministic — reads the store and renders markdown; the only model call is
none. Contains NO order/execution code.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone

from .config import REPO_ROOT, app_name
from .db import Database

QUADRANT_LABEL = {
    "rec_taken": "the analyst recommended, you took it",
    "rec_passed": "the analyst recommended, you passed",
    "advised_against_done": "the analyst advised against, you did it anyway",
    "own_idea": "your own idea",
}


def _latest_mark(db: Database, rec_id) -> dict | None:
    marks = db.select("ledger_marks", {"recommendation_id": rec_id}, order_by="mark_date")
    return marks[-1] if marks else None


def _forward_mark(db: Database, rec_id, rec_date: str, days: int) -> dict | None:
    """First mark on/after rec_date + days (progressive fill per the project design)."""
    from datetime import timedelta
    target = (date.fromisoformat(str(rec_date)[:10]) + timedelta(days=days)).isoformat()
    for m in db.select("ledger_marks", {"recommendation_id": rec_id}, order_by="mark_date"):
        if str(m["mark_date"]) >= target:
            return m
    return None


def build_scorecard(db: Database, month: str | None = None) -> str:
    month = month or date.today().strftime("%Y-%m")
    recs = db.select("recommendations", order_by="id")
    lines = [f"# {app_name()} scorecard — {month} (generated {date.today().isoformat()})",
             "", f"Ledger: {len(recs)} recommendation(s). Sample sizes are small "
             "— read every number as anecdote until ~2 quarters of ledger data "
             "exist (the project design).", ""]

    # Per-recommendation table with vs-SPY marks.
    lines += ["## All recommendations vs SPY (since rec date)",
              "| Symbol | Rec | Date | Px@rec | Quadrant | Latest vs SPY | 30d | 90d |",
              "|---|---|---|---|---|---|---|---|"]
    quad_perf: dict[str, list[float]] = defaultdict(list)
    for r in recs:
        th = db.select_one("theses", {"id": r.get("thesis_id")}) or {}
        sym = th.get("symbol") or "?"
        latest = _latest_mark(db, r["id"])
        vs = latest.get("vs_spy_pct") if latest else None
        if vs is not None:
            quad_perf[r.get("quadrant") or "undecided"].append(float(vs))
        m30 = _forward_mark(db, r["id"], r["rec_date"], 30)
        m90 = _forward_mark(db, r["id"], r["rec_date"], 90)
        fmt = lambda m: (f"{float(m['vs_spy_pct']):+.1f}%"
                         if m and m.get("vs_spy_pct") is not None else "—")
        px = f"${float(r['price_at_rec']):,.2f}" if r.get("price_at_rec") else "—"
        lines.append(f"| {sym} | {r.get('rec_type') or '—'} | {r['rec_date']} | {px} | "
                     f"{r.get('quadrant') or 'undecided'} | "
                     f"{f'{float(vs):+.1f}%' if vs is not None else '—'} | "
                     f"{fmt(m30)} | {fmt(m90)} |")

    # Quadrant decision-value.
    lines += ["", "## Decision value by quadrant (mean vs-SPY, latest mark)"]
    for q, label in QUADRANT_LABEL.items():
        vals = quad_perf.get(q, [])
        mean = sum(vals) / len(vals) if vals else None
        lines.append(f"- **{q}** ({label}): "
                     + (f"{mean:+.1f}% mean vs SPY across {len(vals)}" if vals
                        else "no marked entries yet") + "")
    undecided = sum(1 for r in recs if not r.get("quadrant"))
    if undecided:
        lines.append(f"- ⚠️ {undecided} recommendation(s) have NO logged decision — "
                     f"the quadrant audit can't work until each is logged "
                     f"(tell the bot 'log my decision on X').")

    # Break-condition accuracy.
    lines += ["", "## Break-condition discipline"]
    bcs = db.select("break_conditions")
    triggered = [b for b in bcs if b.get("status") == "triggered"]
    stale = [b for b in bcs if b.get("status") == "stale"]
    lines.append(f"- {len(bcs)} conditions on file · {len(triggered)} triggered · "
                 f"{len(stale)} stale.")
    for b in triggered:
        th = db.select_one("theses", {"id": b["thesis_id"]}) or {}
        lines.append(f"  - TRIGGERED: {th.get('symbol')} #{b.get('ordinal')} — "
                     f"{(b.get('condition_text') or '')[:100]}")

    # Cost report for the month.
    spend = 0.0
    by_task: dict[str, float] = defaultdict(float)
    for row in db.select("api_costs"):
        if str(row.get("ts", "")).startswith(month):
            usd = float(row.get("usd_estimate") or 0)
            spend += usd
            by_task[str(row.get("task", ""))[:24]] += usd
    top = sorted(by_task.items(), key=lambda kv: -kv[1])[:5]
    lines += ["", "## Cost report",
              f"- {month} spend: **${spend:.2f}** (budget guardrail in "
              f"scout/config.yml; hard cap in the Anthropic console)."]
    for task, usd in top:
        lines.append(f"  - {task}: ${usd:.2f}")

    lines += ["", "*Earliness tracking (did coverage/estimates move toward the "
              "thesis?) starts reporting once theses are ≥90 days old.*"]
    return "\n".join(lines)


def reunderwrite_active(db: Database, as_of: str | None = None,
                        monthly_budget: float | None = None,
                        use_batch: bool = True) -> list[dict]:
    """Refresh every ACTIVE thesis by re-running its standard dive, fanned out
    over all active names in ONE Message Batch (the project design: batch the
    re-underwrites; 50% off). Reuses stored evidence — no live gather. This is
    OPT-IN: the scheduled scorecard never calls it, so the monthly report never
    triggers surprise LLM spend. Invoke it explicitly for a monthly refresh."""
    symbols = list(dict.fromkeys(
        t["symbol"] for t in db.select("theses", order_by="id")
        if t.get("symbol") and t.get("status") == "active"))
    from . import research
    return research.reunderwrite_batch(symbols, db=db, as_of=as_of,
                                       monthly_budget=monthly_budget,
                                       use_batch=use_batch)


def write_scorecard(db: Database | None = None, month: str | None = None,
                    reunderwrite: bool = False,
                    monthly_budget: float | None = None) -> str:
    db = db or Database()
    db.apply_schema()
    month = month or date.today().strftime("%Y-%m")
    # Optional monthly refresh of the active book BEFORE scoring, so the
    # scorecard reflects the freshest briefs. Default OFF — the scheduled
    # scorecard stays deterministic and free unless a refresh is asked for.
    if reunderwrite:
        reunderwrite_active(db, monthly_budget=monthly_budget)
    text = build_scorecard(db, month)
    out = REPO_ROOT / "briefs" / f"scorecard_{month}.md"
    out.write_text(text)
    return str(out)


if __name__ == "__main__":
    db = Database()
    db.apply_schema()
    print(build_scorecard(db))
