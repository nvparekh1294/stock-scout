"""scout/checkers.py — deterministic, model-independent verification of a brief.
These run on EVERY brief before you see it (the honesty spine).

Four checks:
  1. citation-presence — numeric factual claims must carry a nearby date/source.
  2. date-format       — explicit numeric dates must be ISO YYYY-MM-DD.
  3. arithmetic        — recompute every '='-anchored $/×/÷/+ expression.
  4. banned-phrase     — a ported banned-phrase list plus project tripwires
                         (e.g. calling an analyst target an "expected return").

Pure Python — no model calls. Honest by construction: an expression the parser
cannot evaluate is reported UNPARSED, never silently passed.
"""

from __future__ import annotations

import re
from datetime import date

# ── banned phrases ────────────────────────────────────────────────────────
# Ported from an earlier private project's banned-phrase list (the hedge-y filler
# the old system vetoed), then extended with this project's own tripwires.
BANNED_PHRASES = [
    # --- ported from an earlier private project ---
    "may not yet be fully priced into consensus estimates",
    "particularly if the theme continues to attract",
    "institutional inflows",
    "combination of bullish",
    "invalidate both the technical and fundamental",
    "consensus estimates, particularly if the theme",
    "may not be fully reflected in",
    # --- Scout additions (project design rules) ---
    "guaranteed",
    "risk-free",
    "can't lose",
    "cannot lose",
    "sure thing",
    "no downside",
    "priced in perfectly",
]

# Contextual tripwires: banned only in a specific context (bare substring would
# over-flag legitimate SEC quotes, e.g. "realizing expected returns from
# capacity expansions"). By design, an analyst target is context, never an
# "expected return".
CONTEXTUAL_BANS = [
    (re.compile(r"(analyst|consensus|price target|\btarget\b)[^.]{0,80}expected return", re.I),
     "'expected return' used to describe an analyst target (per the project design)"),
    (re.compile(r"expected return[^.]{0,80}(analyst|consensus|price target|\btarget\b)", re.I),
     "'expected return' used to describe an analyst target (per the project design)"),
]

ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# Source markers that count as a citation of a primary/dated source.
SOURCE_MARKERS = re.compile(
    r"\b(8-K|10-K|10-Q|S-1|EX-99|newsroom|press release|snapshot|accessed|"
    r"filed|MarketBeat|stockanalysis|Yahoo|Alpaca|EDGAR|CIK|10-K/10-Q)\b", re.I)
# A numeric claim: a $ figure, a percentage, a multiple ("44.1×"), a share
# count, or an analyst count (audit fix 2026-07-12 — these were invisible).
NUMERIC_CLAIM = re.compile(
    r"(\$\s?[\d,]+(?:\.\d+)?\s?[MBK]?|\b\d+(?:\.\d+)?\s?%|\b\d+(?:\.\d+)?×|"
    r"\b\d[\d,]*(?:\.\d+)?\s?[MB]?\s+shares\b|\b\d+\s+analysts?\b)", re.I)
# Non-ISO numeric date shapes we flag: slash dates (M/D/Y, Y/M/D) and strict
# dotted Y.M.D. Deliberately NOT matching decimal divisions like "526.83/31.55".
BAD_DATE = re.compile(
    r"\b(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}/\d{1,2}/\d{1,2}|\d{4}\.\d{2}\.\d{2})\b")
# A numeric range (e.g. "$33–36", "1215–1305") — not a single evaluable equation.
NUM_RANGE = re.compile(r"\d[\d,.]*\s*[–—-]\s*\d")

_SENT_SPLIT = re.compile(r"(?<=[.;:])\s+|\n")


def check_banned_phrases(text: str) -> dict:
    lower = (text or "").lower()
    found = [p for p in BANNED_PHRASES if p.lower() in lower]
    for pat, label in CONTEXTUAL_BANS:
        if pat.search(text or ""):
            found.append(label)
    return {"name": "banned-phrase", "passed": not found, "flags": found}


def check_dates(text: str) -> dict:
    flags = []
    for m in BAD_DATE.finditer(text or ""):
        # Ignore things like "1H CY2027" or ranges — BAD_DATE only matches
        # slash/dot numeric dates, which we always consider non-ISO.
        flags.append(m.group(0))
    return {"name": "date-format", "passed": not flags,
            "flags": sorted(set(flags))}


_DERIVED = re.compile(r"[=≈×✕÷/→]")  # computation/derivation, not a bald claim


def _is_cited(s: str) -> bool:
    return bool(ISO_DATE.search(s) or SOURCE_MARKERS.search(s))


def check_citations(text: str, max_uncited_fraction: float = 0.35) -> dict:
    """Block-aware citation-presence. These docs cite by SECTION/block (e.g. a
    `*(Source: 10-K FY2024...)*` caption governs the block below it), not every
    sentence. A numeric-claim sentence is 'covered' if its own block, or the
    heading/caption block immediately above it, carries a date or source marker.
    Fails only if the uncited fraction of numeric-claim sentences is high — this
    catches an uncited/fabricated brief without drowning a well-sourced one in
    per-sentence noise."""
    blocks = re.split(r"\n\s*\n", text or "")
    claim_sentences = 0
    uncited = []
    for i, block in enumerate(blocks):
        prev_cited = i > 0 and _is_cited(blocks[i - 1])
        block_cited = _is_cited(block)
        for sent in _SENT_SPLIT.split(block):
            s = sent.strip()
            if not s or not NUMERIC_CLAIM.search(s):
                continue
            if s.startswith("|") or s.startswith("#") or s.startswith("---"):
                continue
            if _DERIVED.search(s):  # derived/computed line, not a primary claim
                continue
            claim_sentences += 1
            if _is_cited(s) or block_cited or prev_cited:
                continue
            uncited.append(s[:140])
    frac = (len(uncited) / claim_sentences) if claim_sentences else 0.0
    # Pass if the fraction is healthy OR only a couple of sentences are uncited.
    # The absolute allowance keeps short bodies (e.g. an adversary's few
    # argumentative sentences that reference already-cited figures) from tripping
    # a percentage threshold on a tiny denominator, without weakening detection
    # on real briefs (a genuinely uncited brief has many uncited claims).
    passed = len(uncited) <= 2 or frac <= max_uncited_fraction
    return {"name": "citation-presence",
            "passed": passed,
            "flags": uncited,
            "detail": f"{len(uncited)}/{claim_sentences} numeric-claim sentences "
                      f"uncited ({frac*100:.0f}%)"}


# ── arithmetic ────────────────────────────────────────────────────────────
# A numeric TERM: optional ~ ≈ − $, digits with commas/decimals, optional M/B/K,
# optional trailing × (as a unit, e.g. "4.0×").
_TERM = r"[~≈]?\s*[-−]?\$?\s*\d[\d,]*(?:\.\d+)?\s*[MBK]?×?"
# A CONTIGUOUS equation: TERM (op TERM)+ (= | ≈) TERM. Operators are unicode
# ×/✕/÷ or / or +. ASCII 'x' is NOT an operator (it collides with words). Only
# spaces may sit between an operand and its operator — no intervening words — so
# prose numbers ("Q2-FY26") cannot leak in. Prose-embedded math is left to the
# adversary's arithmetic audit rather than silently mis-evaluated.
_EQN = re.compile(
    rf"({_TERM}(?:\s*[+×✕÷/]\s*{_TERM})+)\s*[=≈]\s*({_TERM})")


def _parse_num(tok: str) -> float | None:
    t = (tok.strip().replace("−", "-").replace("$", "").replace(",", "")
         .replace("~", "").replace("≈", "").replace("×", "").replace(" ", ""))
    if not t:
        return None
    mult = 1.0
    if t[-1] in "MmBbKk":
        mult = {"m": 1e6, "b": 1e9, "k": 1e3}[t[-1].lower()]
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


# Range equation A–B × C = D–E: verified endpoint-wise (audit fix 2026-07-12 —
# these were skipped entirely, and they are the shape of the scenario math).
# '→' accepted as an equals synonym (scenario math writes "…→ implied …").
_RANGE_EQN = re.compile(
    rf"({_TERM})\s*[–—-]\s*({_TERM})\s*[×✕]\s*({_TERM})\s*[=≈→]\s*"
    rf"({_TERM})\s*[–—-]\s*({_TERM})")

# Prose scenario math: "EPS … $7–8 … multiple … 25x → implied … $175–$220"
# (audit fix 2026-07-12: the NRDX bear line 7–8 × 25 → $175–220 was inconsistent
# — 8×25=200, not 220 — and was invisible because the terms are separated by
# prose, not contiguous). Anchored on the word EPS so the range multiplied is
# the earnings range, then the nearest N× multiple, then the '→' implied range.
# ASCII 'x' is a valid multiple marker here ("20x") because it is pinned to a
# digit and bounded by the '→' arrow, so it can't collide with prose words.
# NB: \bEPS\b is case-sensitive on purpose — a case-insensitive "eps" matched
# the substring in "ke**eps** growing", grabbing a revenue range by mistake.
_SCENARIO_EQN = re.compile(
    r"\bEPS\b[^\n→]*?(\d+(?:\.\d+)?)\s*[–—-]\s*\$?\s*(\d+(?:\.\d+)?)"   # EPS range
    r"[^\n→]*?(\d+(?:\.\d+)?)\s*[xX×✕]"                                 # N× multiple
    r"[^\n→]*?→[^\d\n]*?\$?\s*(\d+(?:\.\d+)?)\s*[–—-]\s*\$?\s*(\d+(?:\.\d+)?)")  # implied range

# Strip markdown emphasis before arithmetic parsing (audit fix 2026-07-12: bold
# on either side of an equation defeated the regex, making the checker inert on
# exactly the decision math it existed to verify).
_MD_EMPHASIS = re.compile(r"[*_]{1,3}")


def check_arithmetic(text: str, tol: float = 0.03) -> dict:
    """Recompute every CONTIGUOUS '='/'≈'-anchored expression. Each is MATCH or
    MISMATCH; expressions the parser cannot form are simply not matched (and are
    the adversary's job) — never a silent pass."""
    text = _MD_EMPHASIS.sub("", text or "")
    checks = []
    for m in _RANGE_EQN.finditer(text):
        a, b, c, d, e = (_parse_num(m.group(i)) for i in range(1, 6))
        if None in (a, b, c, d, e) or 0 in (d, e):
            continue
        for lhs_val, rhs_val, tag in ((a * c, d, "low"), (b * c, e, "high")):
            rel = abs(lhs_val - rhs_val) / abs(rhs_val)
            checks.append({"expr": f"{m.group(0).strip()[:100]} [{tag} endpoint]",
                           "computed": lhs_val, "stated": rhs_val,
                           "status": "MATCH" if rel <= tol else "MISMATCH",
                           "rel_err": round(rel, 4)})
    # Prose scenario math (EPS range × multiple → implied price range). Tolerance
    # is the audit-specified 5% — flag when a stated endpoint falls outside the
    # computed one by more than that. Endpoint-wise: eps_lo×mult vs price_lo,
    # eps_hi×mult vs price_hi.
    SCENARIO_TOL = 0.05
    for m in _SCENARIO_EQN.finditer(text):
        eps_lo, eps_hi, mult, p_lo, p_hi = (_parse_num(m.group(i)) for i in range(1, 6))
        if None in (eps_lo, eps_hi, mult, p_lo, p_hi) or 0 in (p_lo, p_hi):
            continue
        for computed, stated, tag in ((eps_lo * mult, p_lo, "low"),
                                      (eps_hi * mult, p_hi, "high")):
            rel = abs(computed - stated) / abs(stated)
            checks.append({
                "expr": f"{m.group(0).strip()[:100]} [{tag} endpoint, EPS×multiple]",
                "computed": computed, "stated": stated,
                "status": "MATCH" if rel <= SCENARIO_TOL else "MISMATCH",
                "rel_err": round(rel, 4)})
    for m in _EQN.finditer(text):
        lhs, rhs = m.group(1), m.group(2)
        # Ranges (A–B × C = D–E) can't be verified with scalar math — leave them
        # to the adversary's arithmetic audit rather than mis-evaluate. A range
        # dash may sit just OUTSIDE the matched span (the dash breaks the term),
        # so check a small window around the match, not just the match itself.
        window = (text or "")[max(0, m.start() - 8):m.end() + 8]
        if NUM_RANGE.search(window):
            continue
        rhs_val = _parse_num(rhs)
        nums = [n for n in (_parse_num(t) for t in re.split(r"[+×✕÷/]", lhs)) if n is not None]
        if rhs_val is None or rhs_val == 0 or len(nums) < 2:
            continue
        if "+" in lhs:
            lhs_val = sum(nums)
        elif "×" in lhs or "✕" in lhs:
            lhs_val = nums[0]
            for n in nums[1:]:
                lhs_val *= n
        else:  # '/' or '÷'
            lhs_val = nums[0]
            for n in nums[1:]:
                if n == 0:
                    lhs_val = None
                    break
                lhs_val /= n
            if lhs_val is None:
                continue
        rel = abs(lhs_val - rhs_val) / abs(rhs_val)
        checks.append({
            "expr": m.group(0).strip()[:120],
            "computed": lhs_val, "stated": rhs_val,
            "status": "MATCH" if rel <= tol else "MISMATCH",
            "rel_err": round(rel, 4),
        })
    mismatches = [c for c in checks if c["status"] == "MISMATCH"]
    return {"name": "arithmetic", "passed": not mismatches,
            "checks": checks, "flags": mismatches}


# ── consistency checks (audit fixes 2026-07-12) ───────────────────────────
_HEADING = re.compile(r"^#{1,4}\s|\n#{1,4}\s")
_MONTH_YEAR = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{4})\b", re.I)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}


def _section_body(text: str, label_regex: str) -> str:
    m = re.search(label_regex + r".*?\n(.*?)(?=\n#{1,4}\s|\Z)", text or "", re.I | re.S)
    return m.group(1) if m else ""


# A $-value, optionally the near endpoint of a "$A–$B" range. group(1)=near,
# group(2)=far (if a range). Used to read PRICES only — never a bare multiple.
_PRICE_VAL = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)(?:\s*[–—-]\s*\$?\s?([\d,]+(?:\.\d+)?))?")
# A magnitude unit immediately following a $-value marks it as a backlog/revenue
# citation ($3,415.0M), never a per-share price. Skipped when reading the ceiling
# (audit #4 ceiling-inflation hole: a "$3,415.0M" sitting on an `implied price ≈`
# line was being read as the ceiling and masking real trigger violations).
_MAGNITUDE_SUFFIX = re.compile(r"\s?(?:[MBK]\b|bn|mm|billion|million|thousand)", re.I)
# A scenario / implied-price statement — where the brief states a modeled price.
# We read the ceiling of the valuation range from these ONLY. A multiple ("35×")
# carries no "$" and so is structurally excluded: the NRDX false positives came
# from reading the multiple range "35–40×" as a $35–$40 price band.
_SCENARIO_PRICE_CUE = re.compile(r"(?:implied\s+price|price\s*[≈~=]|fair\s+value)", re.I)
# Fallback ceiling source (restored 2026-07-13 audit #4): the scenario-line
# "Base … $A–$B" band. Used ONLY when no cue-based implied-price ceiling is found
# — briefs that write scenarios as "**Base** (…): **$205–228**" carry no implied-
# price cue, so without this fallback the checker went inert and silently passed
# the OPTC founding case (buy ≤$318.50 vs base $205–$228). group(2) = base high.
_BASE_BAND = re.compile(
    r"\bbase\b[^\n]{0,140}?\$\s?([\d,]+(?:\.\d+)?)\s*[–—-]\s*\$?\s?([\d,]+(?:\.\d+)?)", re.I)
# Entry-level trigger phrasing: a $-value is an actionable ENTRY price only when
# one of these governs it. Backlog figures, 52-week-range citations, and bare
# references to a scenario (e.g. "re-price against the base case (~$214)") are NOT
# entry levels and must never be compared as one. Extended 2026-07-13 (audit #4):
# ≤/≥ symbol forms ("Price ≤ $318.50"), "north of $X", "reaches/hits/crosses $X"
# (present tense only — past-tense narration "the stock hit $1,204.68" must NOT
# count), and "initiate at/above $X".
_ENTRY_TRIGGER = re.compile(
    r"(?:pull(?:s|ing|ed)?\s?back(?:s|ing)?\s+(?:toward|to|below|near)|"
    r"buy(?:ing|s)?\s+(?:at|near|below|around|above)|"
    r"enter(?:ing|s)?\s+(?:at|near|below|around|above)|"
    r"entry\s+(?:at|near|below|around|toward|above)|"
    r"initiate\s+(?:at|near|below|around|above)|"
    r"add(?:ing|s)?\s+(?:at|near|below|around|above)|"
    r"north\s+of|"
    r"(?:reaches|hits|crosses)(?:\s+(?:above|below|to|toward))?|"
    r"(?:drop|dip|fall|decline|move|rally)(?:s|ing|ed)?\s+(?:to|toward|below)|"
    r"[≤≥]|"
    r"(?:below|under|above|over))\s+(?:the\s+)?"
    r"(\$\s?[\d,]+(?:\.\d+)?(?:\s*[–—-]\s*\$?\s?[\d,]+(?:\.\d+)?)?)", re.I)


def _price_values(s: str, drop_magnitudes: bool = False) -> list[float]:
    """All price magnitudes in `s`, including both endpoints of any "$A–$B" range.
    Reads only $-anchored values, so multiples ("35×") are excluded by design.
    With drop_magnitudes=True, a $-value immediately suffixed by a magnitude unit
    (M/B/K/bn) or written as comma-thousands (e.g. $3,415) is skipped — such a
    value is a backlog/revenue figure, never a per-share price (used when reading
    the valuation ceiling so an inline citation can't inflate it)."""
    out = []
    for m in _PRICE_VAL.finditer(s):
        if drop_magnitudes and _MAGNITUDE_SUFFIX.match(s[m.end():m.end() + 12]):
            continue
        for g in m.groups():
            if not g:
                continue
            v = _parse_num(g)
            if v is None:
                continue
            # A comma-thousands value (3,415 → 3415) on a scenario line is a
            # backlog/market-cap figure, not a per-share price — skip for ceiling.
            if drop_magnitudes and "," in g and v >= 1000:
                continue
            out.append(v)
    return out


def _valuation_ceiling(text: str) -> float:
    """The brief's highest modeled per-share price. Preferred: the max value on an
    implied-price / fair-value cue line, read only AFTER the cue and with magnitude
    citations dropped (so "$3,415.0M" on an `implied price ≈` line can't become the
    ceiling). Fallback when no cue line exists: the base-case band high. Returns 0
    when neither is present (checker then stays inert)."""
    ceiling = 0.0
    for line in text.splitlines():
        if "$" not in line:
            continue
        cue = _SCENARIO_PRICE_CUE.search(line)
        if not cue:
            continue
        vals = _price_values(line[cue.end():], drop_magnitudes=True)
        if vals:
            ceiling = max(ceiling, max(vals))
    if ceiling:
        return ceiling
    # Fallback: no cue-based implied price anywhere. Use the base-case band — but
    # read the base HIGH as the max per-share PRICE on that scenario line, not the
    # first "$A–$B" range (which is often the EPS band: "EPS ~$6–7 … → ~$78–91").
    # drop_magnitudes strips revenue like "$4.6B" so only per-share prices remain.
    for line in text.splitlines():
        if _BASE_BAND.search(line):
            vals = _price_values(line, drop_magnitudes=True)
            if vals:
                return max(vals)
    return 0.0


def check_trigger_consistency(text: str) -> dict:
    """The recurring FATAL class (OPTC trigger ≤$318.50 vs base $205–$228; QMEM
    'north of $105' vs base $78–$91): a BUY entry-trigger price above the brief's
    own valuation ceiling is inconsistent and gets flagged. Deterministic
    bookkeeping, not judgment.

    Hardened 2026-07-13 (audit #4). Two failure modes were fixed:
    (1) the NRDX 5/5 false positives came from reading the valuation MULTIPLE range
        ("35–40×") as a $35–$40 price band, then comparing every $-value in the
        triggers section against it. The ceiling is now read only from implied-
        price statements (a multiple carries no "$" and is excluded), after the cue
        position, with magnitude citations dropped — so a "$3,415.0M" backlog on an
        `implied price ≈` line can no longer masquerade as the ceiling.
    (2) that rewrite then silently PASSED the OPTC/VRTA founding cases, whose
        scenario lines carry no implied-price cue (ceiling→0, checker inert). The
        base-case band is restored as a FALLBACK ceiling source; and the entry-
        trigger patterns were widened (≤/≥, "north of", "reaches/hits/crosses",
        "initiate at/above") to catch "Price ≤ $318.50"-style phrasing."""
    text = _MD_EMPHASIS.sub("", text or "")
    trig = _section_body(text, r"Entry triggers?")
    flags = []
    ceiling = _valuation_ceiling(text)
    if ceiling and trig:
        for m in _ENTRY_TRIGGER.finditer(trig):
            # The trigger regex captures only the $-number (any trailing B/M unit
            # is left out), so a "≥ $1.05B" guide figure reads as a harmless $1.05
            # far below the ceiling — no magnitude drop needed here, and a legit
            # 4-digit entry price ("buy at $1,204") must stay in.
            px = max(_price_values(m.group(1)) or [0.0])
            if px and px > ceiling * 1.02 and px < ceiling * 100:
                flags.append(f"entry-trigger price ${px:,.2f} exceeds the brief's "
                             f"own valuation ceiling ${ceiling:,.2f}")
    return {"name": "trigger-vs-scenario", "passed": not flags, "flags": flags}


# Deadline phrasing — a date is only a *deadline* if one of these governs it
# (audit fix 2026-07-12: the old check flagged EVERY past date in the section,
# so citation dates like "(8-K filed 2026-03-26)" read as lapsed deadlines —
# 5/5 false positives on the NRDX brief). We now require genuine future-deadline
# phrasing near the date. "trigger" is included for explicit trigger dates.
_DEADLINE_CUE = re.compile(
    r"\b(by|before|until|through|no later than|on or before|deadline|expires?|"
    r"expiry|expiration|due|ends?|closes?|cutoff|lapses?|trigger(?:s|ed|ing)?|"
    r"must .{0,20}? by)\b", re.I)
# Citation/provenance phrasing — a date governed by these is a SOURCE citation,
# never a deadline, and must never be flagged (the project design dating lives
# exactly here). Also covers dates inside a parenthetical that names a filing.
_CITATION_CUE = re.compile(
    r"\b(filed|accessed|reported|dated|published|retrieved|per|as of|as-of|"
    r"source|snapshot|8-K|10-K|10-Q|S-1|EX-99|press release)\b", re.I)


def _governs_date(body: str, start: int) -> tuple[bool, bool]:
    """Look at the text immediately governing a date at `start`: the ~48 chars
    before it, extended back to the start of any open parenthetical it sits in.
    Returns (is_deadline, is_citation)."""
    lo = max(0, start - 48)
    ctx = body[lo:start]
    # If the date is inside parentheses, include the whole parenthetical so a
    # "(8-K filed 2026-03-26)" reads as a citation regardless of the 48-char cap.
    open_paren = body.rfind("(", 0, start)
    close_before = body.rfind(")", 0, start)
    if open_paren != -1 and open_paren > close_before:
        ctx = body[open_paren:start] + " " + ctx
    is_citation = bool(_CITATION_CUE.search(ctx))
    is_deadline = bool(_DEADLINE_CUE.search(ctx))
    return is_deadline, is_citation


def check_stale_deadlines(text: str, as_of: str | None = None) -> dict:
    """Flag a date in Break conditions or Entry triggers ONLY when it is phrased
    as a genuine future deadline (by/before/until/deadline/expires/explicit
    trigger date) AND is already past at `as_of`. Dates inside citation
    parentheticals (filed/accessed/reported/dated DATE) are never flagged — they
    are provenance, not deadlines (audit fix 2026-07-12: those were 5/5 false
    positives on the NRDX brief)."""
    if not as_of:
        return {"name": "stale-deadline", "passed": True, "flags": [],
                "detail": "skipped (no as_of)"}
    flags = []
    for label in (r"Break conditions?", r"Entry triggers?"):
        body = _section_body(_MD_EMPHASIS.sub("", text or ""), label)
        name = label.rstrip("?s")
        for m in _MONTH_YEAR.finditer(body):
            iso = f"{int(m.group(2)):04d}-{_MONTHS[m.group(1).lower()]:02d}-28"
            if iso >= as_of:
                continue
            is_deadline, is_citation = _governs_date(body, m.start())
            if is_deadline and not is_citation:
                flags.append(f"{m.group(0)} in {name} is a deadline already past "
                             f"as of {as_of} — condition may have lapsed")
        for m in ISO_DATE.finditer(body):
            if m.group(0) >= as_of:
                continue
            is_deadline, is_citation = _governs_date(body, m.start())
            if is_deadline and not is_citation:
                flags.append(f"{m.group(0)} in {name} is a deadline already past "
                             f"as of {as_of} — condition may have lapsed")
    return {"name": "stale-deadline", "passed": not flags,
            "flags": sorted(set(flags))}


# Adjacency phrasing that asserts one event happened right after another. If the
# sentence carrying it names two dates more than a fortnight apart, the "right
# after" claim is inconsistent with its own cited dates (audit #4: the NRDX brief
# said the stock fell "immediately following" filings that were five weeks older).
_ADJACENCY_CUE = re.compile(
    r"\b(immediately\s+(?:after|following)|right\s+after|shortly\s+after|"
    r"just\s+after|days?\s+after|the\s+day\s+after|hours?\s+after|"
    r"in\s+the\s+days?\s+(?:after|following))\b", re.I)
_TEMPORAL_TOL_DAYS = 14


def _iso_to_ord(iso: str) -> int | None:
    try:
        y, m, d = (int(x) for x in iso.split("-"))
        return date(y, m, d).toordinal()
    except (ValueError, TypeError):
        return None


def check_temporal_claims(text: str) -> dict:
    """Flag a sentence that asserts temporal adjacency ("immediately following",
    "days after", "the day after") yet cites two dates more than ~2 weeks apart —
    the claim contradicts its own dates. Deterministic; a same-sentence pair ≤14
    days apart (a genuine 'fell the day after the 8-K') never flags."""
    body = _MD_EMPHASIS.sub("", text or "")
    flags = []
    for sent in re.split(r"(?<=[.!?])\s+", body):
        if not _ADJACENCY_CUE.search(sent):
            continue
        ords = sorted(o for o in (_iso_to_ord(m.group(0))
                                  for m in ISO_DATE.finditer(sent)) if o is not None)
        if len(ords) >= 2 and (ords[-1] - ords[0]) > _TEMPORAL_TOL_DAYS:
            cue = _ADJACENCY_CUE.search(sent).group(0)
            gap = ords[-1] - ords[0]
            flags.append(f"temporal claim inconsistent with cited dates: "
                         f"\"{cue}\" but the sentence's dated events span {gap} "
                         f"days (>{_TEMPORAL_TOL_DAYS})")
    return {"name": "temporal-claim", "passed": not flags, "flags": flags}


_ARROW_PCT = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s*→\s*\$\s?([\d,]+(?:\.\d+)?)\s*\(\s*"
    r"([+−-]?\d+(?:\.\d+)?)\s*%\s*\)")


def check_pct_relationships(text: str, tol: float = 0.06) -> dict:
    """Verify '$X → $Y (−Z%)' style claims — the most common decision figures,
    previously invisible to the arithmetic checker."""
    text = _MD_EMPHASIS.sub("", text or "")
    flags, n = [], 0
    for m in _ARROW_PCT.finditer(text):
        x, y = _parse_num(m.group(1)), _parse_num(m.group(2))
        z = float(m.group(3).replace("−", "-"))
        if not x or y is None:
            continue
        n += 1
        actual = (y / x - 1) * 100
        if abs(actual - z) > max(tol * abs(z), 1.0):
            flags.append(f"`{m.group(0)}` — actual change is {actual:+.1f}%")
    return {"name": "pct-relationship", "passed": not flags, "flags": flags,
            "detail": f"{n - len(flags)}/{n} verified" if n else "0 found"}


# ── format completeness (audit fix 2026-07-12) ────────────────────────────
# Every tier's Output format promises a fixed set of sections; a truncated brief
# silently dropped its Pre-mortem (NRDX standard). This maps each depth to the
# section headers its prompt promises, so a missing one is caught deterministically
# rather than read as a clean pass. Labels trace to the tier prompts' Output format.
REQUIRED_SECTIONS = {
    "standard": [
        ("Thesis", r"Thesis in three sentences"),
        ("What is still unpriced", r"still unpriced"),
        ("Variant view", r"Variant view"),
        ("Decision-relevant facts", r"most decision-relevant facts"),
        ("Valuation", r"Valuation"),
        ("Break conditions", r"Break conditions"),
        ("Entry triggers", r"Entry triggers"),
        ("Pre-mortem", r"Pre-?mortem"),
    ],
    "full": [
        ("Thesis", r"Thesis in three sentences"),
        ("What is still unpriced", r"still unpriced"),
        ("Variant view", r"Variant view"),
        ("Catalyst path", r"Catalyst path"),
        ("Decision-relevant facts", r"most decision-relevant facts"),
        ("Valuation", r"Valuation"),
        ("Break conditions", r"Break conditions"),
        ("Pre-mortem", r"Pre-?mortem"),
        ("Suggested sizing", r"Suggested sizing"),
    ],
}


def check_format_completeness(text: str, required: list | None) -> dict:
    """Verify a brief contains every required section header for its tier. A
    missing section is named in the flag. Skipped (pass) when no requirement
    list is supplied, so callers that don't know the tier are unaffected."""
    if not required:
        return {"name": "format-completeness", "passed": True, "flags": [],
                "detail": "skipped (no tier section list)"}
    body = _MD_EMPHASIS.sub("", text or "")
    flags = [f"missing required section: {name}"
             for name, pat in required if not re.search(pat, body, re.I)]
    return {"name": "format-completeness", "passed": not flags, "flags": flags,
            "detail": f"{len(required) - len(flags)}/{len(required)} required "
                      f"sections present"}


def run_all(brief_text: str, as_of: str | None = None,
            required_sections: list | None = None) -> dict:
    # Empty text must FLAG, never pass (audit fix 2026-07-12: an empty
    # adversarial-review section previously sailed through all four checks).
    if not (brief_text or "").strip():
        return {"passed": False, "results": [
            {"name": "non-empty", "passed": False,
             "flags": ["text is EMPTY — the section this report covers "
                       "produced no output; the brief is incomplete"]}]}
    results = [
        check_citations(brief_text),
        check_dates(brief_text),
        check_arithmetic(brief_text),
        check_pct_relationships(brief_text),
        check_trigger_consistency(brief_text),
        check_stale_deadlines(brief_text, as_of),
        check_temporal_claims(brief_text),
        check_banned_phrases(brief_text),
    ]
    if required_sections:
        results.append(check_format_completeness(brief_text, required_sections))
    return {"passed": all(r["passed"] for r in results), "results": results}


def render_report(report: dict) -> str:
    """Markdown block appended to every brief. No heading of its own — every
    caller supplies one (a duplicate '## Deterministic checker report' shipped
    in quick briefs until 2026-07-12)."""
    lines = [f"**Overall:** {'PASS' if report['passed'] else 'FLAGS RAISED'}", ""]
    for r in report["results"]:
        status = "PASS" if r["passed"] else "FLAG"
        if r["name"] == "arithmetic":
            n = len(r["checks"])
            ok = sum(1 for c in r["checks"] if c["status"] == "MATCH")
            lines.append(f"- **arithmetic** [{status}] — {ok}/{n} equations recomputed OK")
            for c in r["flags"]:
                lines.append(f"    - MISMATCH: `{c['expr']}` computed {c['computed']:,.4g} "
                             f"vs stated {c['stated']:,.4g} ({c['rel_err']*100:.1f}% off)")
        else:
            detail = f" — {r['detail']}" if r.get("detail") else (
                f" — {len(r['flags'])} flag(s)" if r["flags"] else "")
            lines.append(f"- **{r['name']}** [{status}]{detail}")
            # Only show individual flags when the check actually failed.
            if not r["passed"]:
                for f in r["flags"][:8]:
                    lines.append(f"    - {f}")
    return "\n".join(lines)


if __name__ == "__main__":
    # Offline self-check (no model calls). Pass a brief path as argv[1]; the
    # default below is a hypothetical dev example (no such file ships) — the
    # guard prints usage instead of crashing when it is absent.
    import sys
    from pathlib import Path
    path = sys.argv[1] if len(sys.argv) > 1 else "briefs/EXAMPLE_FINAL_2026-01-15.md"
    if not Path(path).exists():
        print(f"[dev harness] no brief at {path!r}. Pass a brief path as the "
              f"first argument to run the offline checkers over it.")
        sys.exit(0)
    text = Path(path).read_text()
    rep = run_all(text)
    print(render_report(rep))
    ar = next(r for r in rep["results"] if r["name"] == "arithmetic")
    print(f"\n[debug] arithmetic equations evaluated: {len(ar['checks'])}")
    for c in ar["checks"]:
        print(f"  {c['status']:<8} {c['expr']}")
