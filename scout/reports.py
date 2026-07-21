"""scout/reports.py — brief renderers.

Assembles the final brief from the pipeline outputs. For a full underwrite this
follows the standard brief format: header block → the blind underwrite → the adversarial
review (separate context) → surfaced disagreements → deterministic checker report
→ decision box. Nothing is dropped or averaged; the underwriter's and adversary's
own words are preserved, and their disagreements are surfaced unresolved
(the project design). This is rendered in the visual brief format (price chart,
key-number cards, comps table).
"""

from __future__ import annotations

import re
from datetime import date as _date
from pathlib import Path

from .config import REPO_ROOT, app_name

BRIEFS_DIR = REPO_ROOT / "briefs"


def parse_header(underwrite_text: str) -> dict:
    """Pull stage / conviction / verdict from the underwrite header lines."""
    def grab(pattern):
        m = re.search(pattern, underwrite_text, re.I | re.M)
        return m.group(1).strip() if m else None
    # Tolerate markdown emphasis the model may add, e.g. "Verdict: **WATCH**".
    # Also accept the quick-take format's synonyms ("Read:", "Stage & direction:").
    star = r"[*_\s]*"
    pre = r"^[-*_\s]*"   # tolerate a leading bullet ("- **Stage …**")
    # A header may be MULTI-LINE (Stage / Conviction / Verdict each on their own
    # line, as the deployed model emits) OR SINGLE-LINE joined by " · ". Stop each
    # field at the next " · <label>" or end-of-line so a single-line header cannot
    # let the Stage cell swallow the conviction+verdict that follow it.
    end = r"(?=\s+·\s+(?:Stage|Conviction|Verdict|Read)\b|\s*$)"
    return {
        "stage": (grab(pre + r"Stage(?:\s*&\s*direction)?:\s*(.+?)" + end)
                  or "").strip("* _") or None,
        # Conviction may sit mid-line after a " · ", so it is NOT anchored to line
        # start — accept a line-start OR a middot as its left boundary.
        "conviction": (grab(r"(?:^|·)[-*_\s]*Conviction[^:\n]*:\s*(.+?)" + end)
                       or "").strip("* _") or None,
        "verdict": (grab(r"(?:Verdict|Read):" + star + r"([A-Za-z_-]+)")
                    or "").upper() or None,
    }


def _section(text: str, label_regex: str) -> str | None:
    """Best-effort extraction of one labelled section's body (up to the next
    label). Tolerant of the underwriter's formatting."""
    m = re.search(label_regex + r"\s*:?\s*(.+?)(?=\n[A-Z][^\n]{0,60}:|\Z)",
                  text, re.I | re.S)
    return m.group(1).strip() if m else None


def _clip(s: str, n: int) -> str:
    """Word-boundary clip with an ellipsis (audit fix 2026-07-12: the raw
    [:180] slice cut mid-word in every full brief's sizing cell)."""
    s = " ".join((s or "").split())
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0] + " …"


_DECISION_BY_VERDICT = {
    "UNDERWRITE": "approve or reject a **starter position** at the suggested "
                  "sizing, or send back for a deeper look. No order is placed by "
                  f"{app_name()} — you execute in your own brokerage.",
    "WATCH": "add {symbol} to the **monitored watchlist** with the entry "
             "triggers below, or drop coverage. No capital decision is proposed "
             "today.",
    "PASS": "log {symbol} as **passed** (it stays ledger-live so a ripening "
            "entry trigger is still caught), or drop coverage.",
}


# ── plain-English verdict display (2026-07-21) ──────────────────────────────
# The machine verdict tokens (UNDERWRITE / WATCH / PASS) are what the pipeline
# parses and stores — they are NEVER renamed. But those three words mean nothing
# to a non-trading reader, so wherever a verdict is SHOWN in a brief we translate
# it to a plain-English label. UNDERWRITE-CANDIDATE is the quick-take token for an
# early, unconfirmed candidate and gets its own softer label.
VERDICT_DISPLAY = {
    "UNDERWRITE": "COMPELLING — evidence suggests the market may be underpricing this",
    "UNDERWRITE-CANDIDATE": "WORTH A DEEPER LOOK — an early, unconfirmed sign it may be mispriced",
    "WATCH": "NOT YET — interesting, but no clear edge today",
    "PASS": "NO EDGE — nothing here suggests it's mispriced",
}

# One-line caption explaining the 1–5 conviction scale, shown under the header.
_CONVICTION_LEGEND = (
    "*Conviction scale: 1 = weak evidence, mostly unknowns · 3 = decent evidence "
    "but no clear edge · 5 = strong, specific, checked evidence.*")


def verdict_label(verdict: str | None) -> str:
    """Plain-English display label for a machine verdict token. An unknown or
    empty token passes through unchanged (e.g. 'REVIEW')."""
    if not verdict:
        return "—"
    return VERDICT_DISPLAY.get(verdict.upper(), verdict)


def _verdict_cell(verdict: str | None) -> str:
    """Verdict as shown in a brief: the plain label, with the machine token in
    parentheses for provenance (omitted when the label IS the token)."""
    if not verdict:
        return "—"
    disp = verdict_label(verdict)
    return disp + (f" ({verdict})" if disp != verdict else "")


def _conviction_display(raw: str | None) -> str:
    """Render conviction as 'N/5' on the standardized 1–5 scale. Tolerant of what
    the model actually wrote: '3/5' or '3 / 5' keep their own denominator; a bare
    '3' becomes '3/5'; a legacy 1–10 value (>5) passes through unchanged so an old
    brief never mis-renders as e.g. '7/5'. None → '—'."""
    if not raw:
        return "—"
    raw = raw.strip()
    md = re.search(r"(\d+)\s*/\s*(\d+)", raw)
    if md:
        return f"{md.group(1)}/{md.group(2)}"
    mn = re.search(r"\d+", raw)
    if not mn:
        return raw
    n = int(mn.group(0))
    return f"{n}/5" if n <= 5 else raw


def _oneline(s: str) -> str:
    """Collapse internal whitespace so a multi-line prose field renders cleanly
    inside a single-line blockquote callout."""
    return " ".join((s or "").split())


def _grab_field(text: str, label: str) -> str | None:
    """Best-effort single-line field grab ('Watching for: …', 'Worth a full
    deep-dive? …'). Tolerant of a leading bullet and markdown emphasis. None when
    absent — so legacy briefs without the field omit it gracefully."""
    m = re.search(r"^[-*_\s]*\*{0,2}" + label + r"\*{0,2}\s*:?\s*(.+?)\s*$",
                  text or "", re.I | re.M)
    if not m:
        return None
    val = m.group(1).strip().strip("*_ ").strip()
    return val or None


def parse_conclusion_fields(text: str) -> dict:
    """Parse the plain-English conclusion fields the 2026-07-21 prompts require:
    `Bottom line:` (a 2–4 sentence plain conclusion), `Watching for:` (the concrete
    WATCH trigger), and `Worth a full deep-dive?` (standard/quick escalation line).
    Every field is tolerant: absent → None, so briefs written before these fields
    existed still parse and simply omit them."""
    bl = _section(text or "", r"Bottom line")
    if bl:
        bl = bl.strip().strip("*_ \n").strip() or None
    return {
        "bottom_line": bl,
        "watching_for": _grab_field(text, "Watching for"),
        "deep_dive": _grab_field(text, r"Worth a full deep-dive\?"),
    }


def render_full_brief(symbol: str, as_of: str, underwrite_text: str,
                      adversary_text: str, checker_md: str,
                      adversary_passed_checker_md: str | None = None,
                      pack_name: str = "", n_evidence: int = 0) -> str:
    h = parse_header(underwrite_text)
    concl = parse_conclusion_fields(underwrite_text)
    adv_verdict = None
    m = re.search(r"independent verdict[^\n]*\n?(.+?)(?=\n\n)", adversary_text, re.I | re.S)
    if m:
        adv_verdict = m.group(1).strip()[:200]

    disagreements = _section(adversary_text, r"Disagreements the owner must see")
    sizing = _section(underwrite_text, r"Suggested sizing posture")

    verdict = h["verdict"] or "REVIEW"
    decision = _DECISION_BY_VERDICT.get(verdict, "review the brief and decide.").format(symbol=symbol)

    # Audit fix 2026-07-12: an empty adversarial review must be loudly visible,
    # never rendered as a blank heading that reads like a clean pass.
    if len((adversary_text or "").strip()) < 200:
        adversary_text = ("⚠️ **ADVERSARIAL REVIEW FAILED OR EMPTY — this brief "
                          "is INCOMPLETE. The pipeline requires a fresh-context "
                          "adversarial pass before the verdict is trustworthy. "
                          "Re-run the underwrite.**\n\n" + (adversary_text or ""))

    parts = [
        f"# {symbol} — Thesis Brief · {as_of}",
        "",
        "| | |",
        "|---|---|",
        f"| **Verdict** | **{_verdict_cell(verdict)}** |",
        f"| **Stage** | {h['stage'] or '—'} |",
        f"| **Conviction (2–4yr)** | {_conviction_display(h['conviction'])} |",
        f"| **Sizing** | {_clip(sizing or '—', 180)} |",
        f"| **Pipeline** | evidence pack ({n_evidence} sources"
        + (f", `{pack_name}`" if pack_name else "") + ") → blind underwrite (Opus, "
        "pack only) → deterministic checkers → adversarial review (Opus, fresh "
        "context) → assembled brief |",
        "",
        _CONVICTION_LEGEND,
        "",
    ]
    # Plain-English conclusion, surfaced prominently right under the verdict so a
    # non-trading reader sees the takeaway before the dense analysis below.
    if concl["bottom_line"]:
        parts += [f"> **Bottom line:** {_oneline(concl['bottom_line'])}", ""]
    if concl["watching_for"]:
        parts += [f"> **Watching for:** {_oneline(concl['watching_for'])}", ""]
    parts += [
        "## Blind underwrite (Opus — saw the evidence pack only)",
        underwrite_text.strip(),
        "",
        "## Adversarial review (Opus — separate context, saw pack + brief)",
        adversary_text.strip(),
        "",
    ]
    if disagreements:
        parts += ["## Disagreements surfaced unresolved (you decide, not a compromise)",
                  disagreements, ""]
    parts += ["## Decision box",
              f"**You are being asked to decide:** {decision}", ""]
    parts += ["## Deterministic checker report — underwrite", checker_md, ""]
    if adversary_passed_checker_md:
        parts += ["## Deterministic checker report — adversary", adversary_passed_checker_md, ""]
    parts += ["---",
              f"*Pipeline provenance: evidence pack `{pack_name}` (all claims "
              f"dated+sourced) → blind underwrite → adversarial review (separate "
              f"context) → deterministic checkers. Disagreements surfaced, not "
              f"averaged.*"]
    return "\n".join(parts)


# A short brief's model output repeats the "## SYMBOL — dive as of DATE" heading
# and its Stage/Conviction/Verdict lines, which the header strip below already
# shows — so the block printed TWICE in the HTML (audit fix 2026-07-12). We parse
# the header from the echo, then strip the echo, keeping the verdict-first strip.
_HDR_ECHO = re.compile(
    r"^\s*(?:##\s+.*?(?:dive|take|underwrite).*\bas of\b.*"
    r"|[-*_\s]*\*{0,2}(?:Stage(?:\s*&\s*direction)?|Conviction|Verdict|Read)\b.*"
    r"|-{3,}|\*{3,})\s*$", re.I)


def _strip_redundant_header(body: str) -> str:
    """Drop the leading heading/Stage/Conviction/Verdict/hr echo so it isn't
    printed a second time under the header strip. Stops at the first real
    content line (e.g. the Thesis block)."""
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or _HDR_ECHO.match(s):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).lstrip("\n")


def render_short_brief(symbol: str, as_of: str, depth: str, body_text: str,
                       checker_md: str, pack_name: str = "", n_evidence: int = 0) -> str:
    h = parse_header(body_text)
    concl = parse_conclusion_fields(body_text)
    body_text = _strip_redundant_header(body_text)
    parts = [
        f"# {symbol} — {depth.title()} · {as_of}",
        "",
        f"**Verdict:** {_verdict_cell(h['verdict'])} · **Stage:** {h['stage'] or '—'} · "
        f"**Conviction:** {_conviction_display(h['conviction'])}",
        _CONVICTION_LEGEND,
        f"*Pipeline: evidence pack ({n_evidence} sources"
        + (f", `{pack_name}`" if pack_name else "") + ") → {tier} → checkers. "
        "No adversarial pass at this tier.".format(tier=depth),
        "",
    ]
    # Surface the plain-English conclusion right under the verdict (tolerant of
    # older briefs that lack these fields — each is omitted when absent).
    if concl["bottom_line"]:
        parts += [f"> **Bottom line:** {_oneline(concl['bottom_line'])}", ""]
    if concl["watching_for"]:
        parts += [f"> **Watching for:** {_oneline(concl['watching_for'])}", ""]
    if concl["deep_dive"]:
        parts += [f"> **Worth a full deep-dive?** {_oneline(concl['deep_dive'])}", ""]
    parts += [
        body_text.strip(),
        "",
        "## Deterministic checker report",
        checker_md,
    ]
    return "\n".join(parts)


def write_brief(symbol: str, depth: str, content: str, on_date: str) -> Path:
    BRIEFS_DIR.mkdir(exist_ok=True)
    path = BRIEFS_DIR / f"{symbol}_{depth}_{on_date}.md"
    path.write_text(content)
    return path


if __name__ == "__main__":
    # Offline test: assemble a full brief from an underwrite + adversary pair
    # (no model calls) and run the checkers on it. The paths below are a
    # hypothetical dev example (no such files ship); the guard prints usage
    # instead of crashing when they are absent.
    from . import checkers
    up_path = REPO_ROOT / "briefs" / "EXAMPLE_underwrite_2026-01-15.md"
    adv_path = REPO_ROOT / "briefs" / "EXAMPLE_adversary_2026-01-15.md"
    if not (up_path.exists() and adv_path.exists()):
        print(f"[dev harness] no example briefs at {up_path} / {adv_path}. "
              f"Drop an underwrite + adversary markdown pair there to exercise "
              f"the offline brief assembly.")
        raise SystemExit(0)
    up = up_path.read_text()
    adv = adv_path.read_text()
    cr = checkers.render_report(checkers.run_all(up))
    brief = render_full_brief("EXAMPLE", "2026-01-15", up, adv, cr,
                              pack_name="evidence/EXAMPLE_2026-01-15.md", n_evidence=9)
    print(brief[:1400])
    print("\n... [brief truncated] ...")
    print(f"\n[offline test] assembled brief length: {len(brief)} chars; "
          f"header parsed: {parse_header(up)}")


# ── phone-readable rendering (2026-07-12) ────────────────────
# Telegram can't render a .md file, so briefs are also written as styled HTML
# documents — one tap on the phone opens a clean, readable page. Dependency-
# free converter: handles the constructs briefs actually use (headings, bold,
# tables, lists, hr, inline code, links).

_HTML_CSS = """
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;line-height:1.55;
 color:#1a1a2e;max-width:720px;margin:0 auto;padding:16px;font-size:16px}
h1{font-size:1.5em;border-bottom:2px solid #4a4e69;padding-bottom:6px}
h2{font-size:1.2em;color:#22223b;margin-top:1.6em;border-bottom:1px solid #ddd;
 padding-bottom:4px}
h3{font-size:1.05em;color:#4a4e69}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:.92em;
 display:block;overflow-x:auto}
th,td{border:1px solid #cbd0d8;padding:6px 9px;text-align:left;vertical-align:top}
th{background:#f2f4f8}
code{background:#f2f4f8;padding:1px 5px;border-radius:4px;font-size:.9em}
blockquote{border-left:3px solid #9a8c98;margin:8px 0;padding:4px 12px;
 color:#555;background:#fafafa}
hr{border:none;border-top:1px solid #ccc;margin:20px 0}
@media(prefers-color-scheme:dark){body{background:#15151f;color:#e8e8f0}
 th{background:#26263a}th,td{border-color:#3a3a52}code{background:#26263a}
 h1,h2{color:#e8e8f0;border-color:#4a4e69}blockquote{background:#1c1c2b}}
"""


def _md_inline(s: str) -> str:
    s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', s)
    return s


def to_html(markdown: str, title: str, visual_header: str = "",
            extra_css: str = "") -> str:
    """Convert a brief's markdown to a standalone, mobile-friendly HTML page.
    `visual_header` (if given) is injected right after the first <h1> — the
    deterministic price chart + key-number cards."""
    out, table, in_list = [], [], False

    def flush_table():
        nonlocal table
        if not table:
            return
        rows = [r for r in table if not re.match(r"^\s*\|[\s:|-]+\|\s*$", r)]
        html_rows = []
        for i, r in enumerate(rows):
            cells = [c.strip() for c in r.strip().strip("|").split("|")]
            tag = "th" if i == 0 else "td"
            html_rows.append("<tr>" + "".join(
                f"<{tag}>{_md_inline(c)}</{tag}>" for c in cells) + "</tr>")
        out.append("<table>" + "".join(html_rows) + "</table>")
        table = []

    def flush_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in markdown.splitlines():
        if line.strip().startswith("|"):
            flush_list()
            table.append(line)
            continue
        flush_table()
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            flush_list()
            n = len(m.group(1))
            out.append(f"<h{n}>{_md_inline(m.group(2))}</h{n}>")
            continue
        if re.match(r"^\s*(---+|\*\*\*+)\s*$", line):
            flush_list()
            out.append("<hr>")
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_md_inline(m.group(1))}</li>")
            continue
        m = re.match(r"^\s*>\s?(.*)$", line)
        if m:
            flush_list()
            out.append(f"<blockquote>{_md_inline(m.group(1))}</blockquote>")
            continue
        flush_list()
        if line.strip():
            out.append(f"<p>{_md_inline(line)}</p>")
    flush_table()
    flush_list()
    # Inject the visual header just after the first <h1> (verdict-first placement
    # is preserved — the header block still follows below it).
    if visual_header:
        for i, el in enumerate(out):
            if el.startswith("<h1"):
                out.insert(i + 1, visual_header)
                break
        else:
            out.insert(0, visual_header)
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{_HTML_CSS}{extra_css}</style></head><body>"
            + "".join(out) + "</body></html>")


_TICKER = re.compile(r"[A-Z]{1,6}[0-9]?$")


def _symbol_from_brief(path: Path, md: str) -> str | None:
    """A brief file is named SYMBOL_depth_date.md and opens with `# SYMBOL —`.
    Only render the visual header when both agree it's a ticker brief (tax
    plans / scorecards / radar memos have no symbol and get no chart)."""
    tok = path.stem.split("_")[0]
    if _TICKER.fullmatch(tok) and re.search(rf"^#\s+{re.escape(tok)}\b", md, re.M):
        return tok
    return None


# Provenance line shown between a live header and an older body (Task 2). The
# header cards enrich with today's price/consensus; the body is the analysis of
# record from an earlier as-of date, so the two can legitimately disagree.
_PROVENANCE_CSS = """
.provenance{border:1px solid #d9a441;background:#fdf6e3;color:#7a5a00;
 border-radius:8px;padding:8px 11px;margin:10px 0 14px;font-size:.86em}
@media(prefers-color-scheme:dark){.provenance{background:#2a2410;color:#e8cf8a;
 border-color:#6b5410}}
:root[data-theme=dark] .provenance{background:#2a2410;color:#e8cf8a;
 border-color:#6b5410}
:root[data-theme=light] .provenance{background:#fdf6e3;color:#7a5a00;
 border-color:#d9a441}
"""


def _provenance_line(render_date: str, as_of: str) -> str:
    return (f'<div class="provenance"><b>Header data live as of {render_date}</b> '
            f'· brief body as of {as_of} — figures may disagree; the body is the '
            f'analysis of record.</div>')


def html_for_brief(brief_path: Path | str, db=None) -> Path:
    """Write the HTML twin of a saved markdown brief, with the deterministic
    visual header (price chart + key-number cards) when the file is a per-symbol
    brief. `db` (if given) — pass the caller's existing handle (e.g. the Telegram
    poller's long-lived DB, or a tool call's ctx.db) so this render reuses it
    instead of opening a new Postgres connection. Only when no db is given do we
    open one here, and only then do we close it — a caller-supplied db is the
    caller's to manage, never closed by us. Chart/card failures degrade to NOT
    FOUND, never crash the brief.

    Re-render provenance (Task 2): the header always enriches with LIVE figures.
    When today's render date is later than the brief's as-of date, that live
    header can contradict an older body (the stale-header incident class — a brief
    written one day and re-rendered the next, when the header cards move but the
    body does not). We then
    (a) inject a provenance line between header and body, (b) stamp the render
    date into the H1 title, and (c) write to a `_rerendered-<date>.html` file so
    the re-render never overwrites/masquerades as the original."""
    p = Path(brief_path)
    md = p.read_text()
    render_date = _date.today().isoformat()
    as_of = brief_as_of(p)
    stale = bool(as_of and as_of < render_date)
    header_html, extra_css = "", ""
    sym = _symbol_from_brief(p, md)
    if sym:
        owns_db = False
        if db is None:
            try:
                from .db import Database
                db = Database()
                owns_db = True
            except Exception:
                db = None
        try:
            from . import visuals
            header_html, extra_css = visuals.render_visual_header(sym, md, db=db)
        except Exception:
            header_html, extra_css = "", ""
        finally:
            if owns_db and db is not None:
                db.close()
    if sym and stale:
        header_html = header_html + _provenance_line(render_date, as_of)
        extra_css = extra_css + _PROVENANCE_CSS
        # Stamp the render date into the H1 so the title itself flags the re-render.
        md = re.sub(r"^(#\s+.+)$",
                    lambda m: m.group(1) + f" · re-rendered {render_date}",
                    md, count=1, flags=re.M)
        html_path = p.with_name(f"{p.stem}_rerendered-{render_date}.html")
    else:
        html_path = p.with_suffix(".html")
    html_path.write_text(to_html(md, html_path.stem.replace("_", " "),
                                 visual_header=header_html, extra_css=extra_css))
    return html_path


def html_for_compare(md_path: Path | str, symbols: list[str], db=None) -> Path:
    """HTML twin of a compare brief, with a Task-7 visual header for EACH symbol
    (labelled) injected after the <h1>. `db` — pass the caller's existing handle
    the same way as html_for_brief; a db we open ourselves here is closed here
    too, never a caller-supplied one. Header failures degrade to nothing."""
    p = Path(md_path)
    md = p.read_text()
    owns_db = False
    if db is None:
        try:
            from .db import Database
            db = Database()
            owns_db = True
        except Exception:
            db = None
    headers, css = [], ""
    try:
        for s in symbols:
            try:
                from . import visuals
                h, css = visuals.render_visual_header(s, md, db=db)
                headers.append(f"<h3>{s}</h3>{h}")
            except Exception:
                pass
    finally:
        if owns_db and db is not None:
            db.close()
    html_path = p.with_suffix(".html")
    html_path.write_text(to_html(md, p.stem.replace("_", " "),
                                 visual_header="".join(headers), extra_css=css))
    return html_path


# ── brief freshness (Task 1, 2026-07-13) ───────────────────────────────────
# A saved brief is named SYMBOL_depth_YYYY-MM-DD.md; a later re-render appends
# `_rerendered-YYYY-MM-DD`. The FIRST ISO date in the stem is the brief's
# as-of (the data date it was underwritten against) — that is what freshness is
# measured against, never the re-render date.
_BRIEF_DATE = re.compile(r"_(\d{4}-\d{2}-\d{2})(?=_|$)")

# Depth-tier dollar estimates for the regenerate cost-consent ask (per the project design). Kept in sync with config.yml depth_tiers[*].usd_estimate; a small
# hardcoded fallback so the relay never crashes if config is unreadable.
_TIER_COST_FALLBACK = {"quick": "0.10–0.30", "standard": "1–3",
                       "full": "5–15"}


def brief_as_of(path: Path | str) -> str | None:
    """The brief's as-of date (first ISO date in the filename stem), or None."""
    m = _BRIEF_DATE.search(Path(path).stem)
    return m.group(1) if m else None


def brief_tier(path: Path | str) -> str | None:
    """The depth tier from a SYMBOL_tier_date.md filename (quick/standard/full)."""
    parts = Path(path).stem.split("_")
    return parts[1] if len(parts) >= 2 else None


def assess_freshness(path: Path | str, today: str | None = None) -> dict:
    """Compare a saved brief's as-of date to today's data date. `stale` is True
    only when the brief was underwritten against an EARLIER day than today — the
    signal that its body may contradict a live-enriched header (e.g. a brief
    re-rendered a day later with fresher header cards)."""
    today = today or _date.today().isoformat()
    as_of = brief_as_of(path)
    return {"as_of": as_of, "today": today,
            "stale": bool(as_of and as_of < today)}


def tier_cost_estimate(tier: str | None) -> str:
    """Dollar-range estimate for regenerating at a depth tier (cost-consent)."""
    try:
        from .config import load_config
        est = (load_config().get("depth_tiers", {}).get(tier or "", {})
               .get("usd_estimate"))
        if est:
            return str(est).replace("-", "–")
    except Exception:
        pass
    return _TIER_COST_FALLBACK.get(tier or "", "0.40–0.70")


def latest_brief(symbol: str, tier: str | None = None) -> Path | None:
    """Newest saved single-name brief for a symbol, chosen by AS-OF DATE (not the
    lexical filename order — `quick` sorts before `standard`, which used to make
    an older standard brief win over a newer quick take). Compare briefs
    (SYMBOL_vs_...) are excluded. Pass `tier` to restrict to one depth."""
    sym = symbol.upper()
    pattern = f"{sym}_{tier}_*.md" if tier else f"{sym}_*.md"
    files = [f for f in BRIEFS_DIR.glob(pattern) if "_vs_" not in f.stem]
    if not files:
        return None
    # Sort by (as-of date, mtime) so the freshest DATA wins; mtime breaks ties
    # between same-day tiers deterministically.
    return sorted(files, key=lambda f: (brief_as_of(f) or "0000-00-00",
                                        f.stat().st_mtime))[-1]
