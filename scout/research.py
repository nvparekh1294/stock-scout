"""scout/research.py — the underwrite orchestrator.

underwrite(symbol, depth): depth = quick | standard | full.

Full pipeline:
  evidence pack (curated file; sources stored in the never-read-twice `evidence`
  table) → blind underwriter (Opus, sees the pack only) → deterministic checkers
  → adversary (Opus, FRESH context) → assemble the final brief with the adversary's
  objections and unresolved disagreements flagged → persist thesis +
  break_conditions + entry_triggers + recommendation rows.

quick/standard run the cheaper Sonnet tiers with the same checkers, no adversary.

Sub-agent isolation (the project design): the underwriter and the adversary are
separate `llm.call`s with independent message lists — neither sees the other's
reasoning, only the pack (and, for the adversary, the finished brief).

Contains NO order/execution code.

Evidence gathering note: underwrites run from CURATED, dated evidence packs.
Live web/EDGAR gathering with Haiku extraction is the radar's job; here, a pack
file is required and its sources are extracted deterministically into the store.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date as _date
from pathlib import Path

from . import checkers, reports
from .config import REPO_ROOT, app_name, depth_cost_estimate, load_config
from .db import Database
from . import llm

PROMPTS_DIR = REPO_ROOT / "prompts"
EVIDENCE_DIR = REPO_ROOT / "evidence"

DEPTHS = {"quick", "standard", "full"}
PROMPT_FOR_DEPTH = {"quick": "quick_take", "standard": "standard_dive"}
# Headings that separate stable instructions (system, cached) from the volatile
# evidence/brief content (user message).
SPLIT_HEADINGS = ("## NOW UNDERWRITE THIS PACK", "## EVIDENCE PACK",
                  "## THE TWO PACKS TO COMPARE")
# The evidence pack now lives in a cached SYSTEM block (commit c301d7f), so every
# call site fills the USER-turn {{EVIDENCE_PACK}} placeholder with this pointer
# instead of "" — an empty labeled section reads as "there is no pack", whereas
# this makes it explicit the pack is above in the (cached) system context.
_EVIDENCE_POINTER = ("(The complete evidence pack is provided above in the "
                     "system context.)")

HEADER_COMMENT = re.compile(r"<!--.*?-->\s*", re.DOTALL)
URL_ROW = re.compile(r"https?://\S+")
ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


# ── prompt building ───────────────────────────────────────────────────────
def _load_template(name: str) -> str:
    text = (PROMPTS_DIR / f"{name}.md").read_text()
    return HEADER_COMMENT.sub("", text, count=1).strip()


def build_prompt(name: str, scalars: dict, volatiles: dict) -> tuple[str, str]:
    """Return (system, user). System = stable instructions + exemplar (cacheable);
    user = the volatile evidence pack / brief."""
    text = _load_template(name)
    for k, v in scalars.items():
        text = text.replace("{{" + k + "}}", v)
    idx = min((i for i in (text.find(h) for h in SPLIT_HEADINGS) if i != -1),
              default=-1)
    if idx == -1:
        # No volatile section — fill everything inline.
        for k, v in volatiles.items():
            text = text.replace("{{" + k + "}}", v)
        return text, "Proceed."
    system = text[:idx].strip()
    user = text[idx:]
    for k, v in volatiles.items():
        user = user.replace("{{" + k + "}}", v)
    return system, user


def _cached_pack_system(instructions: str, pack_text: str,
                        pack_first: bool, cache: bool = True) -> list[dict]:
    """Build the `system` content blocks with the evidence pack as its OWN
    cached breakpoint, alongside the (separately cached) instructions.

    Prompt caching is a prefix match, so ORDER decides what is shared (per the project design:
    prompt-cache evidence contexts):

    - pack_first=False (standard/quick): the instructions block leads. The
      instructions+pack prefix is byte-identical only for the SAME name across
      the interactive `underwrite()` truncation/format-completeness retry, so
      that retry cache-READS the prefix it just wrote. This does NOT share across
      names in a fan-out: the instructions embed {{SYMBOL}}/{{AS_OF_DATE}}, which
      the standard_dive template substitutes into the system-side content, so the
      instructions block differs per symbol. The only fan-out that uses
      pack_first=False — reunderwrite_batch — therefore passes cache=False (no
      cross-name prefix to share, and no same-request retry/adversary reader in
      that path), so it doesn't pay the 1.25x cache-WRITE premium for an entry
      nothing ever reads.
    - pack_first=True (full tier): the pack block leads and is byte-identical
      between the underwriter and the adversary, so the adversary pass
      cache-READS the pack the underwriter just wrote (the two calls diverge
      only AFTER the shared pack). No prompt WORDING changes — the pack is simply
      relocated ahead of the divergent per-role instructions (exemplar rule).

    cache=False omits the cache_control key from BOTH blocks entirely (the
    Anthropic content-block schema treats an absent key as "no caching"), for
    call paths where no later request ever cache-reads the prefix.
    """
    instr_block = {"type": "text", "text": instructions}
    pack_block = {"type": "text", "text": pack_text}
    if cache:
        instr_block["cache_control"] = {"type": "ephemeral"}
        pack_block["cache_control"] = {"type": "ephemeral"}
    return [pack_block, instr_block] if pack_first else [instr_block, pack_block]


# ── evidence pack ─────────────────────────────────────────────────────────
def find_pack(symbol: str, pack_path: str | None) -> Path:
    if pack_path:
        p = Path(pack_path)
        if not p.is_absolute():
            p = REPO_ROOT / p
        if not p.exists():
            raise FileNotFoundError(f"evidence pack not found: {p}")
        return p
    # Latest evidence/{SYMBOL}_*.md
    candidates = sorted(EVIDENCE_DIR.glob(f"{symbol}_*.md"))
    if not candidates:
        raise FileNotFoundError(
            f"no curated evidence pack for {symbol} in {EVIDENCE_DIR}. "
            f"P0 underwrites from a curated pack — pass --pack <path>, or build "
            f"one with the evidence_pack.md gatherer (live gathering is the "
            f"radar's job, P1).")
    return candidates[-1]


def store_evidence_sources(db: Database, symbol: str, pack_text: str) -> int:
    """Deterministically parse the pack's source rows (any line with a URL) and
    upsert them into the never-read-twice `evidence` store, keyed on source_url."""
    stored = 0
    for line in pack_text.splitlines():
        m = URL_ROW.search(line)
        if not m:
            continue
        url = m.group(0).rstrip("|) ").rstrip(".")
        if db.select_one("evidence", {"source_url": url}):
            continue  # never store the same document twice
        dm = ISO.search(line)
        dl = line.lower()
        doc_type = ("8-K" if "8-k" in dl else "10-K" if "10-k" in dl else
                    "10-Q" if "10-q" in dl else "S-1" if "s-1" in dl else
                    "web" if url.startswith("http") else "other")
        try:
            db.insert("evidence", {
                "symbol": symbol,
                "doc_date": dm.group(1) if dm else None,
                "source_url": url,
                "doc_type": doc_type,
                "extracted_text": line.strip()[:2000],
            })
            stored += 1
        except Exception:
            pass  # unique(source_url) race / malformed row — skip, never crash
    return stored


# ── persistence ───────────────────────────────────────────────────────────
def _extract_list(text: str, label: str) -> list[str]:
    """Pull numbered/bulleted items under a labelled section."""
    m = re.search(label + r".*?:?\s*\n?(.+?)(?=\n[A-Z][^\n]{0,50}:|\Z)",
                  text, re.I | re.S)
    if not m:
        return []
    items = re.findall(r"^\s*(?:\(?\d+[.)]|[-*])\s*(.+)$", m.group(1), re.M)
    return [i.strip()[:400] for i in items if i.strip()]


_STATUS_FOR_VERDICT = {"UNDERWRITE": "active", "WATCH": "watch", "PASS": "watch"}


def persist_full(db: Database, symbol: str, on_date: str, underwrite_text: str,
                 adversary_text: str) -> dict:
    h = reports.parse_header(underwrite_text)
    verdict = h["verdict"] or "REVIEW"
    stage = _first_int(h["stage"])
    conviction = _first_int(h["conviction"])
    thesis_id = db.insert("theses", {
        "symbol": symbol, "stage": stage, "conviction": conviction,
        "verdict": verdict,
        "thesis_text": (reports._section(underwrite_text, r"Thesis in three sentences") or "")[:4000],
        "variant_view": (reports._section(underwrite_text, r"Variant view") or "")[:4000],
        "status": _STATUS_FOR_VERDICT.get(verdict, "watch"),
    })
    breaks = _extract_list(underwrite_text, "Break conditions")
    for i, b in enumerate(breaks, 1):
        db.insert("break_conditions", {
            "thesis_id": thesis_id, "ordinal": i, "condition_text": b,
            "check_frequency": "monthly", "status": "intact"})
    triggers = _extract_list(underwrite_text, "Entry triggers")
    for t in triggers:
        db.insert("entry_triggers", {
            "thesis_id": thesis_id, "condition_text": t, "status": "watching"})
    rec_id = db.insert("recommendations", {
        "thesis_id": thesis_id,
        "rec_type": verdict.lower(),
        "rec_date": on_date,
        "price_at_rec": _extract_price(underwrite_text),
        "sizing_suggestion": (reports._section(underwrite_text, r"Suggested sizing posture") or "")[:400],
    })
    return {"thesis_id": thesis_id, "recommendation_id": rec_id,
            "break_conditions": len(breaks), "entry_triggers": len(triggers)}


def _first_int(s):
    if not s:
        return None
    m = re.search(r"\d+", s)
    return int(m.group(0)) if m else None


def _extract_price(text):
    m = re.search(r"(?:price|at)\s*\$?\s*([\d,]+\.\d{2})", text, re.I)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def _guard_truncation(r: dict, label: str) -> tuple[str, bool]:
    """Audit fix 2026-07-12: a max_tokens stop previously shipped silently
    (e.g. if two adversary runs get truncated mid-sentence, the verdict and
    disagreements are lost). Stamp a visible banner and let the caller force
    checker_passed=False."""
    text = r.get("text") or ""
    if r.get("stop_reason") == "max_tokens":
        return (f"⚠️ **TRUNCATED OUTPUT — {label} hit its token budget and is "
                f"INCOMPLETE. Do not act on this section; re-run with a larger "
                f"budget.**\n\n" + text, True)
    return text, False


# ── orchestrator ──────────────────────────────────────────────────────────
def underwrite(symbol: str, depth: str = "full", pack_path: str | None = None,
               as_of: str | None = None, db: Database | None = None,
               monthly_budget: float | None = None,
               underwriter_prompt: str = "underwriter") -> dict:
    """underwriter_prompt selects the underwriter template. Defaults to the
    shipped 'underwriter' (QMEM exemplar). Overridden only for point-in-time validation
    verification where the target's own score is the exemplar — running a symbol
    with its own answer embedded would contaminate the blind test."""
    if depth not in DEPTHS:
        raise ValueError(f"depth must be one of {DEPTHS}, got {depth!r}")
    symbol = symbol.upper()
    as_of = as_of or _date.today().isoformat()
    config = load_config()
    db = db or Database()
    db.apply_schema()

    gather_cost = 0.0
    try:
        pack = find_pack(symbol, pack_path)
        pack_text = pack.read_text()
        pack_rel = str(pack.relative_to(REPO_ROOT)) if pack.is_relative_to(REPO_ROOT) else str(pack)
        n_ev = store_evidence_sources(db, symbol, pack_text)
    except FileNotFoundError:
        # Self-sufficient on any resolvable ticker: no curated
        # pack → gather live. Quick = light snippet; standard/full = full pack
        # (Sonnet gathering + Haiku extraction, stored in evidence). On-demand
        # NEVER depends on the weekly radar.
        from . import gather
        if depth == "quick":
            lite = gather.light_evidence(symbol)
            if isinstance(lite, gather.Inactive):
                # Inactive/delisted (2026-07-15 stale-ticker fix): surface the
                # HONEST delisted reason and build NO pack — never the misleading
                # "can't resolve" message (this ticker resolves; it just no longer
                # trades and its queue entry is stale).
                raise ValueError(str(lite))
            if lite is None:
                raise ValueError(f"{symbol} could not be resolved as a listed "
                                 f"security — cannot run a quick take on it.")
            pack_text, n_ev = lite
            pack_rel = "live-gather (quick take)"
        else:
            full = gather.full_evidence(symbol, db, as_of=as_of)
            if full is None:
                raise ValueError(f"{symbol} could not be resolved as a listed "
                                 f"security — cannot underwrite it.")
            pack_text, n_ev, gather_cost = full
            pack_rel = "live-gather (full pack)"
        store_evidence_sources(db, symbol, pack_text)

    scalars = {"SYMBOL": symbol, "AS_OF_DATE": as_of}
    cost = gather_cost  # include on-demand gathering in the reported cost

    if depth in ("quick", "standard"):
        tier = "sonnet"
        budget = int(config["depth_tiers"][depth]["max_tokens"])
        # Pack lives in a cached SYSTEM block (after the cached instructions) so
        # the completeness/truncation retry below is a cache READ of the pack,
        # not a fresh write. `user` stays the small framing string; the retry
        # only appends its instruction after the cached prefix.
        instructions, user = build_prompt(PROMPT_FOR_DEPTH[depth], scalars,
                                          {"EVIDENCE_PACK": _EVIDENCE_POINTER})
        sys_blocks = _cached_pack_system(instructions, pack_text, pack_first=False)
        r = llm.call(f"{symbol}-{depth}", tier, [{"role": "user", "content": user}],
                     max_tokens=budget, system=sys_blocks, db=db,
                     monthly_budget=monthly_budget)
        cost += r["usd"]
        body_text, truncated = _guard_truncation(r, f"{symbol} {depth}")
        retry_used = False
        if truncated:
            # Auto-retry ONCE at 1.5x budget (audit fix 2026-07-12: the standard
            # tier hit its budget mid-sentence and silently dropped whole
            # sections). Both call costs are logged by llm.call; we only fall
            # back to a truncated brief (with the existing banner) if the retry
            # ALSO truncates. The retry is >= the original in completeness, so we
            # always adopt its result.
            retry_budget = int(budget * 1.5)
            r2 = llm.call(f"{symbol}-{depth}-retry", tier,
                          [{"role": "user", "content": user}],
                          max_tokens=retry_budget, system=sys_blocks, db=db,
                          monthly_budget=monthly_budget)
            cost += r2["usd"]
            r, body_text, truncated = r2, *_guard_truncation(r2, f"{symbol} {depth}")
            retry_used = True
        req = checkers.REQUIRED_SECTIONS.get(depth)  # standard has a fixed format
        checker = checkers.run_all(body_text, as_of=as_of, required_sections=req)
        if truncated:
            checker["passed"] = False
        # Completeness retry (audit fix 2026-07-13): a natural `end_turn` stop can
        # ALSO ship an incomplete brief — e.g. a standard brief can end via
        # end_turn missing the Pre-mortem, so the max_tokens path above never fired and
        # only check_format_completeness caught the gap. If the format checker
        # names missing required sections AND we have NOT already spent our single
        # retry, re-run ONCE at 1.5x budget with an explicit instruction to finish
        # the format. Maximum ONE retry per brief TOTAL — shared with the
        # max_tokens path above, so a brief is never re-run twice. If the retry is
        # still incomplete we deliver it with the existing banner + checker flags
        # (the current honest behavior); no third call is ever made.
        if not retry_used and req:
            fmt = next((c for c in checker["results"]
                        if c.get("name") == "format-completeness"), None)
            if fmt and not fmt["passed"]:
                missing = [f.split("missing required section:")[-1].strip()
                           for f in fmt["flags"]]
                strengthened = (
                    user + "\n\n---\nYour previous attempt omitted required "
                    f"sections: {', '.join(missing)}. Produce the COMPLETE "
                    "format; do not stop before the final required section.")
                retry_budget = int(budget * 1.5)
                r2 = llm.call(f"{symbol}-{depth}-retry", tier,
                              [{"role": "user", "content": strengthened}],
                              max_tokens=retry_budget, system=sys_blocks, db=db,
                              monthly_budget=monthly_budget)
                cost += r2["usd"]
                r, body_text, truncated = r2, *_guard_truncation(r2, f"{symbol} {depth}")
                checker = checkers.run_all(body_text, as_of=as_of,
                                           required_sections=req)
                if truncated:
                    checker["passed"] = False
        r = dict(r, text=body_text)
        content = reports.render_short_brief(symbol, as_of, depth, r["text"],
                                             checkers.render_report(checker),
                                             pack_name=pack_rel, n_evidence=n_ev)
        path = reports.write_brief(symbol, depth, content, as_of)
        return {"brief_path": str(path), "cost_usd": round(cost, 4),
                "checker_passed": checker["passed"], "depth": depth}

    # full — the pack leads BOTH the underwriter and the adversary system (same
    # bytes), so the adversary's read is a cache HIT on the pack the underwriter
    # wrote (Task 2 cache audit). build_prompt fills the pack placeholder with ""
    # here; the real pack is the shared cached block.
    u_budget = int(config["depth_tiers"]["full"]["max_tokens"])
    u_sys, u_user = build_prompt(underwriter_prompt, scalars,
                                 {"EVIDENCE_PACK": _EVIDENCE_POINTER})
    u_blocks = _cached_pack_system(u_sys, pack_text, pack_first=True)
    r_u = llm.call(f"{symbol}-underwrite", "opus", [{"role": "user", "content": u_user}],
                   max_tokens=u_budget, system=u_blocks, thinking={"type": "adaptive"},
                   db=db, monthly_budget=monthly_budget)
    cost += r_u["usd"]
    underwrite_text, u_trunc = _guard_truncation(r_u, f"{symbol} underwrite")
    u_checker = checkers.run_all(underwrite_text, as_of=as_of,
                                 required_sections=checkers.REQUIRED_SECTIONS.get("full"))
    if u_trunc:
        u_checker["passed"] = False

    # adversary — FRESH context (new call, independent messages), sees pack + brief.
    # Budget 16k (audit fix 2026-07-12: 10k truncated 2 of 3 shipped reviews).
    a_sys, a_user = build_prompt("adversary", scalars,
                                 {"EVIDENCE_PACK": _EVIDENCE_POINTER,
                                  "UNDERWRITE_BRIEF": underwrite_text})
    a_blocks = _cached_pack_system(a_sys, pack_text, pack_first=True)
    r_a = llm.call(f"{symbol}-adversary", "opus", [{"role": "user", "content": a_user}],
                   max_tokens=16000, system=a_blocks, thinking={"type": "adaptive"},
                   db=db, monthly_budget=monthly_budget)
    cost += r_a["usd"]
    adversary_text, a_trunc = _guard_truncation(r_a, f"{symbol} adversary")
    a_checker = checkers.run_all(adversary_text, as_of=as_of)
    if a_trunc:
        a_checker["passed"] = False

    content = reports.render_full_brief(
        symbol, as_of, underwrite_text, adversary_text,
        checkers.render_report(u_checker),
        adversary_passed_checker_md=checkers.render_report(a_checker),
        pack_name=pack_rel, n_evidence=n_ev)
    path = reports.write_brief(symbol, "full", content, as_of)

    persisted = persist_full(db, symbol, as_of, underwrite_text, adversary_text)

    return {"brief_path": str(path), "cost_usd": round(cost, 4),
            "checker_passed": u_checker["passed"] and a_checker["passed"],
            "underwrite_checker": u_checker["passed"],
            "adversary_checker": a_checker["passed"],
            "n_evidence_sources": n_ev, "persisted": persisted, "depth": "full"}


# ── monthly re-underwrite fan-out (the project design: batch the re-underwrites) ─
def reunderwrite_batch(symbols: list[str], db: Database | None = None,
                       as_of: str | None = None, monthly_budget: float | None = None,
                       use_batch: bool = True) -> list[dict]:
    """Re-run the standard-dive underwriter over MANY names in ONE Message Batch
    (50% cheaper). This is the scorecard's monthly refresh fan-out.

    Each name reuses its EXISTING evidence (curated pack → latest saved brief →
    extraction store, via `_compare_source`) — NO name triggers a fresh live
    gather, so a refresh over the book is cheap and side-effect-free. The
    deterministic checkers still run on every refreshed brief (a hard design rule), and
    each item's cost is logged through llm (batch or sync fallback). Names with
    no usable stored evidence yet are skipped. Returns one result dict per
    refreshed name: {symbol, brief_path, checker_passed, cost_usd, via}.

    No cross-name cache sharing exists here: standard_dive substitutes
    {{SYMBOL}}/{{AS_OF_DATE}} into the system-side instructions, so each name's
    instructions block differs, and there is no same-request retry/adversary
    reader in this batch path. So the system blocks are built with cache=False —
    paying the 1.25x cache-WRITE premium would buy a cache entry nothing reads.
    """
    as_of = as_of or _date.today().isoformat()
    config = load_config()
    db = db or Database()
    db.apply_schema()

    depth = "standard"
    budget = int(config["depth_tiers"][depth]["max_tokens"])
    requests: list[dict] = []
    prepared: list[tuple[str, str]] = []
    for raw in symbols:
        sym = raw.upper()
        src = _compare_source(sym, db)
        if src is None:
            continue
        pack_text, label = src
        instructions, user = build_prompt(
            PROMPT_FOR_DEPTH[depth], {"SYMBOL": sym, "AS_OF_DATE": as_of},
            {"EVIDENCE_PACK": _EVIDENCE_POINTER})
        # cache=False: per-symbol instructions never share a prefix across names,
        # and nothing re-reads this request — so skip the cache-write premium.
        sys_blocks = _cached_pack_system(instructions, pack_text,
                                         pack_first=False, cache=False)
        requests.append({"task": f"{sym}-{depth}-refresh", "model_tier": "sonnet",
                         "messages": [{"role": "user", "content": user}],
                         "max_tokens": budget, "system": sys_blocks})
        prepared.append((sym, label))

    if not requests:
        return []

    if use_batch:
        results = llm.call_batch(requests, db=db, monthly_budget=monthly_budget)
    else:
        results = [llm.call(r["task"], r["model_tier"], r["messages"],
                            max_tokens=r["max_tokens"], system=r["system"], db=db,
                            monthly_budget=monthly_budget) for r in requests]

    req_sections = checkers.REQUIRED_SECTIONS.get(depth)
    out: list[dict] = []
    for (sym, label), r in zip(prepared, results):
        body_text, truncated = _guard_truncation(r, f"{sym} {depth} refresh")
        checker = checkers.run_all(body_text, as_of=as_of,
                                   required_sections=req_sections)
        if truncated:
            checker["passed"] = False
        content = reports.render_short_brief(
            sym, as_of, depth, body_text, checkers.render_report(checker),
            pack_name=f"monthly refresh · {label}", n_evidence=0)
        path = reports.write_brief(sym, depth, content, as_of)
        out.append({"symbol": sym, "brief_path": str(path),
                    "checker_passed": checker["passed"],
                    "cost_usd": round(r["usd"], 4), "via": r.get("via", "sync")})
    return out


# ── head-to-head compare (Task 10) ─────────────────────────────────────────
def _compare_source(symbol: str, db: Database) -> tuple[str, str] | None:
    """Resolve pack/extraction data for a compare, WITHOUT a live gather. Order:
    curated evidence pack → latest saved brief → extraction-store rows. Returns
    (text, label) or None when the symbol has no usable data yet."""
    symbol = symbol.upper()
    try:
        p = find_pack(symbol, None)
        return p.read_text(), f"evidence pack {p.name}"
    except FileNotFoundError:
        pass
    b = reports.latest_brief(symbol)
    if b:
        return b.read_text(), f"latest brief {b.name}"
    rows = db.select("evidence", {"symbol": symbol})
    rows = [r for r in rows if r.get("extracted_text")]
    if rows:
        text = "\n\n".join(
            f"[{r.get('doc_type') or 'doc'} dated {r.get('doc_date') or 'n/a'}, "
            f"source {r.get('source_url')}]\n{r['extracted_text']}" for r in rows[-8:])
        return text, "extraction store"
    return None


def compare(symbol_a: str, symbol_b: str, db: Database | None = None,
            as_of: str | None = None, monthly_budget: float | None = None) -> dict:
    """Head-to-head compare of two symbols from existing pack/extraction data.
    Never auto-gathers: if either symbol lacks data, returns needs_gather=True
    with a cost-consent message (the caller asks before spending). Reuses the
    Task 8 comps machinery for the quantitative side-by-side and a modest Sonnet
    pass for the cited narrative. No BUY verdict — Scout compares, owner decides.
    """
    from . import comps
    symbol_a, symbol_b = symbol_a.upper(), symbol_b.upper()
    as_of = as_of or _date.today().isoformat()
    db = db or Database()
    db.apply_schema()

    src_a = _compare_source(symbol_a, db)
    src_b = _compare_source(symbol_b, db)
    missing = [s for s, src in ((symbol_a, src_a), (symbol_b, src_b)) if src is None]
    if missing:
        # Cost-consent (the project design): each missing symbol needs a fresh dated
        # pack — a standard-tier gather, priced from the config table
        # (depth_tiers.standard.usd_estimate) so it never drifts from what config
        # documents. We state the per-symbol range scaled by count and DO NOT spend.
        _parts = depth_cost_estimate("standard").replace("$", "").replace("–", "-").split("-")
        try:
            _lo, _hi = float(_parts[0]), float(_parts[-1])
        except (ValueError, IndexError):
            _lo, _hi = 0.0, 0.0
        est = f"${_lo * len(missing):.2f}–${_hi * len(missing):.2f}"
        return {"needs_gather": True, "missing": missing,
                "message": (f"I can't compare yet — no pack or stored evidence for "
                            f"{', '.join(missing)}. Gathering a full dated pack for "
                            f"{'each' if len(missing) > 1 else 'it'} would cost about "
                            f"{est}. Want me to gather {', '.join(missing)} first? "
                            f"(Nothing spent yet.)")}

    pack_a, label_a = src_a
    pack_b, label_b = src_b

    # Quantitative side-by-side from the peer_metrics extraction store (Task 8).
    # Populate each side deterministically first (EDGAR + Alpaca + Nasdaq, NO
    # LLM, cached) so the compare table renders real rows, not a NOT-FOUND
    # scaffold — the same machinery a single-name gather uses (Task 1).
    try:
        from . import peers
        from .market_ref import resolve_ticker
        for s in (symbol_a, symbol_b):
            res = resolve_ticker(s)
            if res.get("cik"):
                peers.populate_metrics(db, [{"symbol": s, "cik": res["cik"]}], as_of)
    except Exception:
        pass
    rows = comps.peer_metrics_for(db, [symbol_a, symbol_b])
    comps_md = comps.render_comps_table(symbol_a, rows)

    scalars = {"SYMBOL_A": symbol_a, "SYMBOL_B": symbol_b, "AS_OF_DATE": as_of}
    system, user = build_prompt("compare", scalars,
                                {"PACK_A": pack_a, "PACK_B": pack_b})
    budget = 3000
    r = llm.call(f"{symbol_a}-vs-{symbol_b}-compare", "sonnet",
                 [{"role": "user", "content": user}], max_tokens=budget,
                 system=system, db=db, monthly_budget=monthly_budget)
    narrative, truncated = _guard_truncation(r, f"{symbol_a} vs {symbol_b} compare")
    checker = checkers.run_all(narrative, as_of=as_of)
    if truncated:
        checker["passed"] = False

    content = "\n".join([
        f"# {symbol_a} vs {symbol_b} — Compare · {as_of}",
        "",
        f"*{app_name()} compares; you decide. No BUY verdict. Sources: {symbol_a} — "
        f"{label_a}; {symbol_b} — {label_b}. Analyst targets are context, never "
        f"expected return.*",
        "",
        "## Quantitative comps (deterministic — extraction store)",
        comps_md,
        "",
        narrative.strip(),
        "",
        "## Deterministic checker report",
        checkers.render_report(checker),
    ])
    path = reports.BRIEFS_DIR / f"{symbol_a}_vs_{symbol_b}_compare_{as_of}.md"
    reports.BRIEFS_DIR.mkdir(exist_ok=True)
    path.write_text(content)
    html = reports.html_for_compare(path, [symbol_a, symbol_b], db=db)
    return {"needs_gather": False, "brief_path": str(path), "html_path": str(html),
            "cost_usd": round(r["usd"], 4), "checker_passed": checker["passed"],
            "symbols": [symbol_a, symbol_b]}


def main():
    ap = argparse.ArgumentParser(description="Scout underwrite orchestrator")
    ap.add_argument("symbol")
    ap.add_argument("--depth", default="full", choices=sorted(DEPTHS))
    ap.add_argument("--pack", default=None, help="path to a curated evidence pack")
    ap.add_argument("--as-of", default=None, help="as-of date (YYYY-MM-DD)")
    ap.add_argument("--budget", type=float, default=None,
                    help="override the monthly budget cap (USD) for this run")
    ap.add_argument("--underwriter-prompt", default="underwriter",
                    help="alternate underwriter template (point-in-time anti-contamination)")
    args = ap.parse_args()
    try:
        out = underwrite(args.symbol, depth=args.depth, pack_path=args.pack,
                         as_of=args.as_of, monthly_budget=args.budget,
                         underwriter_prompt=args.underwriter_prompt)
    except llm.BudgetExceeded as e:
        print(f"BUDGET STOP: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"\nbrief written: {out['brief_path']}")
    print(f"run cost: ${out['cost_usd']:.4f}")
    print(f"checkers passed: {out['checker_passed']}")
    if out["depth"] == "full":
        print(f"evidence sources stored: {out['n_evidence_sources']}")
        print(f"persisted: {out['persisted']}")


if __name__ == "__main__":
    main()
