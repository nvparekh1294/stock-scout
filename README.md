# Stock Scout

**A self-hosted AI equity-research analyst. It researches; it never trades.**

Stock Scout is a personal, chat-native research analyst you run yourself. You talk
to it over Telegram; it underwrites companies, monitors your theses, surfaces new
ideas, and keeps score — all with dated, cited evidence and deterministic checks
on every brief. There is no order-placement code anywhere in this project, by
construction: you make and execute every decision yourself, in your own brokerage.

> **This is educational/research software, NOT investment advice, and it never
> places trades. Its outputs may be wrong despite the built-in checkers — verify
> everything against primary sources. No warranty. You are solely responsible for
> any decision you make.**

## Design principle #1: it never trades

The first rule of this project is that it holds **no ability to trade**. No broker
write-credentials, no order endpoints, not behind a flag, not in a sandbox. Market
data is read-only. The database schema has no order/execution tables. The bot's
system prompt and the agent tool set contain no trade action — the only "actions"
are reading and writing your own research records. If you ever want it to place a
trade: it can't, and that is the point. See `scout/schema.sql`, `scout/agent_tools.py`,
and `scout/telegram_bot.py` — none contain order logic.

## What it does

- **Underwrites with an adversarial review + deterministic checkers.** A "full
  underwrite" runs a blind underwriter (it sees only its evidence pack), then an
  adversary in a *separate* context that argues the other side; their disagreements
  are surfaced to you unresolved (`scout/research.py`). Before you ever see a brief,
  pure-Python checkers verify it (`scout/checkers.py`): every numeric claim must
  carry a nearby dated source, dates must be ISO-formatted, every `=`-anchored
  arithmetic expression is recomputed, and a banned-phrase list catches hedge-y
  filler and any misuse of an analyst target as an "expected return".
- **Daily monitoring.** A daily sweep checks each monitored thesis against its own
  break conditions and entry triggers; silence is the default — an alert means the
  thesis actually changed (`scout/monitor.py`).
- **Weekly radar.** A constraint-radar theme walk generates a short list of new
  ideas per week, with a confirmation queue and cheap "quick takes" (`scout/radar.py`).
- **Scorecard.** A monthly report card scores the decision ledger — was the analyst
  right, and did your overrides add or subtract value (`scout/scorecard.py`).
- **Telegram-first.** You interact entirely in plain language over Telegram; the
  relay is gated to a single owner chat id and refuses to start unconfigured
  (`scout/telegram_bot.py`).
- **First-run profile interview.** On first use, with no profile configured, the
  bot interviews you (goals, horizon, risk, tax jurisdiction, brokerage context,
  research themes) and saves your profile; the tax planner and radar read from it,
  with safe generic fallbacks (`scout/profile.py`).

## Architecture

```
Telegram  ──►  relay (owner-gated)  ──►  agent loop (Sonnet + tools)
                                          │
                    ┌─────────────────────┼───────────────────────┐
                    ▼                      ▼                       ▼
             underwrite orchestrator   scheduled loops        profile intake
             (research.py)             monitor / radar /      (profile.py)
                    │                   policy / scorecard
        ┌───────────┴───────────┐
        ▼                       ▼
  blind underwriter        adversary (fresh context)
  (Opus, evidence-only)    (Opus, argues the other side)
        └───────────┬───────────┘
                    ▼
        deterministic checkers  ──►  brief you read
        (citation · dating · arithmetic · banned-phrase)
```

- An **orchestrator** (`scout/research.py`) gathers a dated evidence pack, runs the
  blind underwriter and the adversary as isolated **specialist sub-agents**, then
  assembles the brief.
- **Checkers** (`scout/checkers.py`) run on every brief — model-independent, pure
  Python.
- Storage is **Postgres** when `DATABASE_URL` is set, or a built-in **JSON fallback**
  otherwise (`scout/db.py`) — so a fresh checkout runs with no database at all.

## Model matrix

Which Claude model does which job is configured in `config.yml` under `models:`
and `depth_tiers:` — quality lives in the *process* (prompts + checkers), not the
model. Defaults:

| Tier | Default model | Used for |
|---|---|---|
| `opus` | `claude-opus-4-8` | radar walks, full underwrites, adversarial review |
| `sonnet` | `claude-sonnet-5` | daily monitor, conversation, memos, brief updates |
| `haiku` | `claude-haiku-4-5` | extraction, dedup, classification |

To use different models, edit the `models:` block (any Claude model ids) and the
`depth_tiers[*].model_tier` mapping. See the Anthropic docs for current model ids.

## What it costs to run

Stock Scout calls the **Anthropic API**, which is **pay-as-you-go and bills your
own API key**. This is **not** a Claude.ai subscription: a Claude Pro or Max plan
includes **no API credits** and cannot be used here. You need an API key from the
[Anthropic Console](https://console.anthropic.com/), funded separately. The API has
**no free tier**.

Cost is driven by **tokens**, so every cost report shows tokens first and dollars
second (`≈ $X at your configured rates`). Typical per-run figures come straight from
`config.example.yml` (`depth_tiers[*].usd_estimate`), **at July 2026 pricing** —
verify current rates on the [Anthropic pricing page](https://www.anthropic.com/pricing):

| Depth | Model tier | Rough cost per run |
|---|---|---|
| quick take | Sonnet | ~$0.10–0.30 |
| standard dive | Sonnet | ~$1–3 |
| full underwrite | Opus | ~$5–15 |

**Safe by default.** The scheduled loops ship **off** (`schedules.enabled: false`)
and there is a **monthly budget cap** (`costs.monthly_budget_usd`, default **$20**):
once the month's logged spend exceeds the cap, the next call raises `BudgetExceeded`
rather than silently spending more. If you enable the scheduled loops they run
automatically (some on Opus) and bill your key on a timer — **a fully-enabled month
can run tens of dollars.** Turn them on only after you have set a budget you are
comfortable with.

**Budget preset.** Most of the ~5× saving here comes from the **depth downgrade**
(full underwrite ~$5–15 → standard dive ~$1–3), not from the model change — Sonnet
versus Opus is only about **1.7×** on its own. To take both, run the `standard` depth
on Sonnet: set `depth_tiers.full.model_tier: sonnet` in `config.yml` (or simply use
the `standard` depth instead of `full`). The analysis is shallower, so the
deterministic checkers carry more of the weight — a reasonable trade for routine work.

**Hosting.** The packaged path is [Railway](https://railway.app/) (~$5/mo), driven
by the included `railway.json` and `Dockerfile`. Any Docker host works equally well.
Be honest with yourself about one thing: an always-on research bot needs an
always-on process, and **there is no free always-on tier** — expect a few dollars a
month for hosting on top of API usage.

Other data sources are free: **Telegram** (bot API), **Alpaca** (read-only market
data, free tier — optional), and **SEC EDGAR** (free; requires a contact string —
see setup).

## Quickstart

Full, step-by-step instructions — creating the Telegram bot, getting your keys,
configuring, and deploying — are in **[SETUP.md](SETUP.md)**. In short:

```bash
git clone <your-fork-url> stock-scout && cd stock-scout
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yml config.yml     # edit: app name, EDGAR contact, tax, themes
cp .env.example .env                 # add your secrets (never commit this)
python -m pytest                     # optional: confirm the suite is green
python -m scout.telegram_bot         # boots once your bot token + chat id are set
```

## Forking is safe for this project

You can fork and even make your copy public **without leaking personal data**,
because Stock Scout keeps **your data in your database, never in repo files**. Your
holdings, theses, briefs, evidence, and your investor profile live in Postgres (or
the local JSON store) — all of which are `.gitignore`d and never committed. This is
deliberately different from tools that commit personal files into the repo. For a
clean, history-free copy, use GitHub's **"Use this template"** button rather than a
fork.

## Example output

See **[`briefs.example/NRDX_underwrite_EXAMPLE.md`](briefs.example/NRDX_underwrite_EXAMPLE.md)** —
a fully worked underwrite of a **fictional** company (invented ticker, invented
numbers) that demonstrates the format, the dating/citation discipline, and the
reverse-DCF valuation. It passes every deterministic checker.

## This repo tracks a private build

The public repo is **downstream** of the owner's private development. See
**[MAINTENANCE.md](MAINTENANCE.md)** for the port rule and the monthly drift check
(including keeping the pricing table current).

## Contributing

See **[CONTRIBUTING.md](CONTRIBUTING.md)** for dev setup, running the tests, and
enabling the pre-commit content check.

## License

MIT — see [LICENSE](LICENSE).
