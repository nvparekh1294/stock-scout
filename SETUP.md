# Setup

End-to-end setup for Stock Scout — from zero to a running, owner-gated bot. Read
[README.md](README.md) first for what it is and what it costs. Reminder:

> This is educational/research software, NOT investment advice, and it never places
> trades. No warranty. You are solely responsible for any decision you make.

## Prerequisites

- **Python 3.12+** (the image uses 3.12; the code runs on 3.11+).
- An **Anthropic API key** (see below — this is not a Claude.ai subscription).
- A **Telegram account** (to create a bot and receive messages).
- Optional: a **Railway account** (or any Docker host) to run it always-on.
- Optional: **Alpaca** (read-only market data) and **Postgres** (otherwise a local
  JSON store is used).

## 1. Create your Telegram bot

1. In Telegram, open a chat with **@BotFather**.
2. Send `/newbot`, choose a name and a username. BotFather replies with a **bot
   token** like `123456789:AA...`. Keep it secret — it goes in `.env` as
   `TELEGRAM_BOT_TOKEN`.

## 2. Get your chat id (and understand owner-gating)

The relay answers **only one chat** — yours. It compares every incoming chat id
against `TELEGRAM_OWNER_CHAT_ID` and silently ignores anyone else, so a stray person
who finds your bot cannot use it or spend your API budget. If the token or the owner
chat id is unset, the bot **refuses to start** (by design).

To find your chat id: message **@userinfobot** (or **@RawDataBot**) on Telegram; it
replies with your numeric id. Put it in `.env` as `TELEGRAM_OWNER_CHAT_ID`.

## 3. Get your Anthropic API key (this is NOT a Claude subscription)

Stock Scout calls the **Anthropic API**, which is **pay-as-you-go** and billed to
your API key. A **Claude Pro or Max plan includes no API credits** and will not work
here — they are separate products. Create and fund a key at the
[Anthropic Console](https://console.anthropic.com/), then set `ANTHROPIC_API_KEY` in
`.env`. The API has **no free tier**; see the cost section of the README and set a
budget you are comfortable with. As a backstop, also set a hard spend cap in the
Anthropic console itself.

## 4. Set your SEC EDGAR contact string (required)

SEC EDGAR's fair-access policy **requires** a descriptive `User-Agent` identifying
you (your name and a real email). Stock Scout ships this **empty on purpose and fails
closed**: any EDGAR request raises a clear error until you set it. In `config.yml`:

```yaml
edgar:
  user_agent: "Jane Doe research jane@example.com"
```

Leave it blank and every EDGAR-backed feature (ticker resolution, filings) will
refuse to run with an explanatory error. This is enforced in
`scout/config.py::edgar_user_agent()`.

## 5. Optional: Alpaca (read-only market data)

Company/price enrichment uses Alpaca's **read-only** data API. It is optional — the
system works without it (SEC EDGAR is the authoritative source). If you want it, get
free API keys from [Alpaca](https://alpaca.markets/) and set them in `.env`:

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
# ALPACA_BASE_URL=...   # optional override
```

These keys are used for **read-only GETs only** — there is no order code anywhere in
this project.

## 6. Optional: Postgres (JSON fallback is the default)

If `DATABASE_URL` is set, Stock Scout uses Postgres as the single source of truth. If
it is **not** set, it falls back to a local JSON store under `scout/_localdb/` — so a
fresh checkout runs with no database at all. For an always-on deploy, use Postgres
(Railway can provision one). Set `DATABASE_URL` in `.env`; the legacy `postgres://`
scheme is normalized automatically.

## 7. Configure `config.yml`

```bash
cp config.example.yml config.yml
```

Then walk the blocks (all non-secret settings live here; secrets stay in `.env`):

- **`app.name`** — the display name your bot uses. Defaults to `"Stock Scout"`.
- **`models`** — which Claude model does which job (`opus` / `sonnet` / `haiku`).
  Edit to use different model ids.
- **`depth_tiers`** — output depth per tier, the model tier it runs on, and the
  documented `usd_estimate` range. Lower cost by running `full` on `sonnet`.
- **`costs`** — `monthly_budget_usd` (default $20; a call over it raises
  `BudgetExceeded`), `escalate_consent_usd`, and the **`pricing`** table with
  **`pricing_as_of`** (see maintenance note below).
- **`schedules`** — the scheduled loops. **`enabled: false` by default.** When you
  flip it to `true`, these run automatically and bill your key on a timer:
  - `daily_monitor` — the daily thesis-integrity sweep (Sonnet; cheap).
  - `radar_weekly` — the weekly idea radar (**Opus** walk; the most expensive loop).
  - `scorecard` — the monthly report card (deterministic; near-free).
  - `policy_fast_lane` — a market-hours poll for policy events.
  Enable them only after you have set a comfortable budget — a fully-enabled month
  can run tens of dollars.
- **`radar.themes`** — the starter themes the radar explores. Replace with your own
  (or let the first-run profile interview seed them).
- **`edgar.user_agent`** — your SEC contact string (step 4).
- **`tax`** — leave `jurisdiction` **unset** and the tax planner refuses to run
  rather than assume a country/state. Set your own jurisdiction and rates before
  using any tax feature.

### Keeping the pricing table current (`pricing_as_of`)

`costs.pricing_as_of` records the month you last verified the `costs.pricing` numbers
against the [Anthropic pricing page](https://www.anthropic.com/pricing). When it is
more than four months old, boot and every cost report print a plain staleness warning
(nothing is fetched automatically) — verify the current rates, update `costs.pricing`,
and bump `pricing_as_of`.

## 8. Deploy

### Railway (packaged path, ~$5/mo)

The repo includes a `Dockerfile` and `railway.json` (Dockerfile builder, restart
policy, and the migrate-then-boot start command). Steps:

1. Create a Railway project from your fork/template copy.
2. Add a **Postgres** plugin; Railway sets `DATABASE_URL` for you.
3. Add the service variables (Railway → your service → Variables):
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID`, `ANTHROPIC_API_KEY`, and
   optionally `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`. The bot refuses to start until
   the first three are set.
4. Provide your non-secret config: `config.yml` is **tracked and committable** — it is
   *not* `.gitignore`d — so commit a `config.yml` **in your own fork** (the `Dockerfile`
   copies `config*.yml` into the image, and `load_config()` prefers `config.yml` over
   the shipped example). Be aware that `config.yml` will contain your EDGAR contact
   (your name and email) and your tax jurisdiction, so only make your fork or copy
   public if you are comfortable with those being visible — otherwise keep them in a
   private copy. If you deploy without one, the image boots on `config.example.yml`'s
   fail-closed defaults (empty SEC contact, no tax jurisdiction, loops off) — usable to
   verify the deploy, but you will want your own `config.yml` for real use.
5. Deploy. The start command runs `scout.migrate` (one-time seed) then the relay.

### Any Docker host (generic)

```bash
docker build -t stock-scout .
docker run --rm \
  -e TELEGRAM_BOT_TOKEN=... -e TELEGRAM_OWNER_CHAT_ID=... \
  -e ANTHROPIC_API_KEY=... -e DATABASE_URL=... \
  stock-scout
```

The container's start command is `python -m scout.migrate --seed-dir
seed_localdb.example && python -m scout.telegram_bot`. With no `DATABASE_URL`, migrate
is a no-op (JSON mode) and the relay runs on the local JSON store.

## 9. First run

- **Profile interview.** On your first Telegram message, with no saved profile, the
  bot does **not** drop you into chat — it runs a short one-time interview (about 7
  questions: goals, horizon, risk, tax country/state, brokerage context, research
  themes, optional budget limits), shows you a summary, and saves nothing until you
  reply `confirm`. You can redo it anytime by saying **"redo my profile"**. The tax
  planner reads your jurisdiction from it; the radar reads your themes; both keep a
  generic fallback when unset. (`scout/profile.py`)
- **Sample data.** The image seeds a small **synthetic** example on first boot (the
  `seed_localdb.example/` directory, per the Dockerfile start command): a couple of
  fictional lots (invented tickers NRDX/HLXR), one sample thesis clearly labelled
  "SAMPLE DATA", and empty conversation/profile tables. Seeding only fills a table
  that is **empty**, so it is a one-time bootstrap and never overwrites your data. To
  start completely empty, point `--seed-dir` at an empty directory.
- **Plaintext profile storage.** Your profile is stored **as plaintext** in your own
  database (the `profiles` table, one row per chat) — it never enters the repo and
  never leaves your database. Profile values are always treated as *data*, never as
  instructions to the model (they are length-capped and fenced at every prompt
  boundary). See the storage-and-privacy note in `scout/profile.py`.

## Troubleshooting

- **Bot won't start** → `TELEGRAM_BOT_TOKEN` and/or `TELEGRAM_OWNER_CHAT_ID` unset.
  This is intentional; set them.
- **EDGAR errors** → `edgar.user_agent` is empty or lacks a real email. Set it.
- **Tax planner refuses** → `tax.jurisdiction` is unset. Set your jurisdiction/rates.
- **"Budget stop"** → this month's logged spend hit `costs.monthly_budget_usd`. Raise
  it deliberately if you mean to.
- **Pricing staleness warning** → update `costs.pricing` and `pricing_as_of`.
