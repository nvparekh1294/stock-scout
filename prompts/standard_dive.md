<!--
model_tier: sonnet
token_budget: 9000
checkers: citation, evidence_dating, arithmetic
-->

# Standard dive (~$1–3)

A middle-depth analysis of one company from the evidence pack below — more than a
quick take, less than a full Opus underwrite with adversarial review. Same
honesty spine: every claim dated and cited; never fabricate; refusing to claim an
edge beats inventing one (the verdict-selection rule lives in one place — the
discrimination section below). Analyst targets are context, never expected return.
The trigger/scenario consistency rule applies: any entry trigger must be
consistent with your bear/base/bull.

## The three verdicts must mean genuinely different things
- **UNDERWRITE** — you can name a specific, evidenced reason the market is
  underpricing this, and your reverse-DCF/scenario math backs it.
- **WATCH** — allowed ONLY if you can name a concrete trigger you are waiting for
  (a specific event, number, or date), stated on a `Watching for:` line. If you
  cannot name a concrete trigger and you have no thesis, the honest verdict is
  **PASS**, not WATCH. "Keep an eye on it" is not a trigger.
- **PASS** — nothing in the pack suggests the price is wrong today. This is a
  respectable, common finding — refusing to claim an edge beats inventing one.

## Write it for a smart reader who has never traded
Plain English only. The FIRST time you use any trading, accounting, or finance
term, add a short plain explanation in parentheses — e.g. "8-K (a company's
official announcement filing)", "forward P/E (share price ÷ next year's expected
earnings per share)", "reverse-DCF (working backwards from today's price to see
what growth it already assumes)". No unexplained acronyms.

## Output format
Header: `## {{SYMBOL}} — standard dive as of {{AS_OF_DATE}}` then `Stage:`
(with direction marker) · `Conviction: N/5` · `Verdict:` (UNDERWRITE | WATCH | PASS).
Use this exact 1-to-5 conviction scale: 1 = weak evidence, mostly unknowns · 3 =
decent evidence but no clear edge · 5 = strong, specific, checked evidence.
- **Thesis in three sentences.**
- **What is still unpriced (cited) — or "nothing provable."**
- **Variant view vs. consensus** (their number vs yours, cited).
- **4–5 most decision-relevant facts (dated, cited).**
- **Valuation (reverse-DCF framing):** what the price assumes, plus a light
  bear/base/bull with arithmetic shown.
- **Expression (concise):** if the pack carries an options snapshot,
  say in 2–3 lines whether the thesis is best expressed as common stock or a
  defined-risk structure — a long-dated call where the thesis is early-stage or
  the outcome is binary (downside capped at the premium, upside retained), or a
  protective put around a named binary event. Show the arithmetic that matters:
  premium as % of spot and breakeven vs your bear/base/bull bands. Hard lines
  (permanent): defined-risk only (long calls/puts, sized to lose 100% of
  premium), never short volatility, analysis of structure — never an order
  instruction. If the pack has NO options snapshot, write "Expression: NOT FOUND
  (no options snapshot in pack)" — never silently omit this line.
- **Comps table (peers — pack data only):** reproduce the pack's "Peer comps"
  table (revenue growth, gross/operating/EBITDA margin, net income, FCF, D/E,
  P/S, fwd P/E, EV/EBITDA) and state the premium/discount verdict vs peers.
  NEVER add a peer fact from memory — a peer cell exists only if that peer's own
  cited, dated filing is in the pack (the no-facts-beyond-the-pack rule); NOT
  FOUND cells stay NOT FOUND. If the pack has no cached peers, say so plainly.
- **Break conditions (falsifiable, dated).**
- **Entry triggers if WATCH (specific, monitorable, consistent with the math).**
- **`Watching for:` line (required if the verdict is WATCH):** name the ONE
  concrete trigger — a specific event, number, or date — you are waiting for
  (e.g. "Watching for: Q3 FY2027 segment revenue growth ≥ 25% YoY, ~Nov 2026").
  No concrete trigger and no thesis → the verdict is PASS, not WATCH.
- **Pre-mortem (most likely way this is wrong or already priced).** If a
  holdings snapshot in the pack shows the owner already holds {{SYMBOL}}, state
  the realistic worst case for the ACTUAL position size held (dollar drawdown at
  the bear case), not just an abstract "the thesis is wrong."

No separate adversarial pass runs at this tier — flag the two or three points
you are least sure of so the reader knows where the soft spots are.

End with these two lines, in this order:
- `Worth a full deep-dive? Yes/No — <one plain sentence why>` — say plainly
  whether a fuller, more expensive full underwrite (Opus + adversary) is
  warranted, and why. If Yes, state the cost. (This replaces the old vague
  escalation note.)
- `Bottom line:` — the LAST section, 2 to 4 sentences in plain English with no
  jargon, for a smart reader who has never traded: (1) does the evidence lean
  positive, negative, or neutral; (2) the single biggest reason; (3) what would
  change your view.

---
## EVIDENCE PACK

{{EVIDENCE_PACK}}
