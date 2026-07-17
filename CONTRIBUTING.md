# Contributing

Thanks for your interest in Stock Scout. This is a small, self-hosted project; the
notes below cover local development.

## Dev setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yml config.yml   # edit for your instance
cp .env.example .env               # add secrets (never commit .env)
```

You do not need any API keys or a database to run the test suite — it uses the JSON
store and stubs out model calls.

## Running the tests

```bash
python -m pytest            # the full suite
python -m scout.prompts_lint # verify the prompt library headers/placeholders
```

The deterministic checkers can be run against any brief file, e.g. the shipped
example:

```bash
python -m scout.checkers briefs.example/NRDX_underwrite_EXAMPLE.md
```

Please keep the suite green (zero skips) and the checkers passing on the example
brief before opening a PR.

## Enable the pre-commit content check

The repo ships a pre-commit hook that blocks a commit if a staged file contains a
personal identifier or a private ticker symbol — a mechanical backstop against
leaking private data into a public repository. Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

(The hook is `.githooks/pre-commit`. To bypass it in a genuine emergency, commit with
`--no-verify` — but understand exactly why it fired first.)

## A note on personal-data safety

Stock Scout is designed so that **your data lives in your database, not in the repo**.
Holdings, theses, briefs, evidence, and your investor profile are all in Postgres (or
the local JSON store) and are `.gitignore`d — none are ever committed. That is what
makes this project safe to fork and even keep public. Please preserve that property:
never add code that writes personal research or identifiers into tracked files, and
keep example/seed data **synthetic** (invented tickers and numbers, clearly labelled).
