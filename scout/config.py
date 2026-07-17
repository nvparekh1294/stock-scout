"""scout/config.py — shared configuration and .env loading.

Two jobs, both used across the package:
  1. load_env()    — read the repo-root .env into os.environ (no extra deps;
                     never prints values; existing env vars win).
  2. load_config() — parse config.yml (the non-secret settings).

Secrets come ONLY from the environment (.env). config.yml holds no secrets.
Copy config.example.yml to config.yml and edit it for your own instance; if
config.yml is absent, the shipped config.example.yml is used as a fail-closed
default (empty SEC contact, no tax jurisdiction, scheduled loops disabled).
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
from functools import lru_cache
from pathlib import Path

import yaml

# Repo root = parent of the scout/ package directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
# Your instance config lives at the repo root as config.yml (copied from the
# shipped config.example.yml). The example is used as a safe fallback so a fresh
# checkout runs and the tests pass without any setup — but every value in it
# fails closed (see edgar_user_agent() and the tax planner's jurisdiction gate).
CONFIG_PATH = REPO_ROOT / "config.yml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config.example.yml"
VERSION_PATH = REPO_ROOT / "VERSION"


def scout_version() -> str:
    """Best available build id for this process, in precedence order:
      1. SCOUT_VERSION env var (a friendly stamp the deploy flow can set);
      2. a VERSION file written at image-build time (the Dockerfile stamps the
         build date here — the container has no .git, which is why the old
         `git rev-parse` returned "unknown");
      3. the git short SHA on a dev checkout (desktop sessions);
      4. last resort — this file's mtime as a build date.
    Never raises."""
    v = os.getenv("SCOUT_VERSION", "").strip()
    if v:
        return v
    try:
        if VERSION_PATH.exists():
            txt = VERSION_PATH.read_text().strip()
            if txt:
                return txt
    except Exception:
        pass
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT), text=True,
            stderr=subprocess.DEVNULL).strip()
        if sha:
            return sha
    except Exception:
        pass
    try:
        mt = _dt.date.fromtimestamp(Path(__file__).stat().st_mtime)
        return f"build {mt.isoformat()}"
    except Exception:
        return "unknown"


def load_env(path: Path = ENV_PATH) -> None:
    """Load KEY=VALUE lines from .env into os.environ. Minimal parser (no
    python-dotenv dependency). Silently does nothing if .env is absent.
    Never prints any value (the project design). Existing environment variables
    take precedence, so a shell-exported secret is never clobbered."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@lru_cache(maxsize=1)
def load_config() -> dict:
    """Parse and cache config.yml. Prefers the repo-root config.yml; if that is
    absent, falls back to the shipped config.example.yml (whose defaults fail
    closed). Raises a clear error only if NEITHER file exists."""
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    if not path.exists():
        raise RuntimeError(
            "No configuration found. Copy config.example.yml to config.yml and "
            "edit it for your instance (see README/SETUP).")
    with open(path) as fh:
        return yaml.safe_load(fh)


def app_name() -> str:
    """The instance's display name, resolved from config (`app.name`). This is
    the single source for the user-facing name — no name is hard-coded in code.
    Falls back to a neutral label if config is missing or the name is unset/the
    placeholder, so imports and tests never depend on a configured name."""
    try:
        name = ((load_config() or {}).get("app") or {}).get("name") or ""
    except Exception:
        name = ""
    name = str(name).strip()
    if not name or name.startswith("__") or name == "Scout":
        return "the analyst"
    return name


def depth_cost_estimate(tier: str) -> str:
    """The user-facing USD cost-estimate range for a depth tier (e.g. "1-3" for
    `standard`), read from config's `depth_tiers[tier].usd_estimate`. This is the
    single source for the cost figures Scout quotes before it spends — no range
    is hard-coded in the agent code, so editing config.yml's pricing table is
    what changes what owners are told. Returns "" if the tier or field is absent
    (callers fall back to their surrounding wording)."""
    tiers = (load_config() or {}).get("depth_tiers") or {}
    return str(((tiers.get(tier) or {}).get("usd_estimate") or "")).strip()


def user_agent() -> str:
    """A generic HTTP User-Agent for NON-SEC web fetches (news RSS, price/consensus
    scrapes). Derived from the instance display name and build id so no product
    name is hard-coded. SEC/EDGAR requests use edgar_user_agent() instead, which
    fails closed until a real contact is configured."""
    slug = app_name().lower().replace(" ", "-") or "analyst"
    return f"Mozilla/5.0 (compatible; {slug}/{scout_version()})"


def edgar_user_agent() -> str:
    """The SEC EDGAR User-Agent contact string, validated. SEC's fair-access
    policy REQUIRES a descriptive contact (your name + a real email). This ships
    empty on purpose, so it fails closed: any EDGAR request raises a clear error
    until you set `edgar.user_agent` in config.yml to your own name and email."""
    ua = str(((load_config() or {}).get("edgar") or {}).get("user_agent") or "").strip()
    if not ua or "@" not in ua or "." not in ua.split("@")[-1]:
        raise RuntimeError(
            "SEC EDGAR requires a descriptive User-Agent identifying you "
            "(your name and a real contact email). Set `edgar.user_agent` in "
            "config.yml — e.g. \"Jane Doe research jane@example.com\" — before "
            "using any SEC/EDGAR feature.")
    return ua


def pricing_as_of() -> str:
    """The month (YYYY-MM) the config pricing table was last verified, from
    `costs.pricing_as_of`. Empty string if unset."""
    return str(((load_config() or {}).get("costs") or {})
               .get("pricing_as_of") or "").strip()


def pricing_staleness_warning(today: _dt.date | None = None,
                              months_threshold: int = 4) -> str:
    """A plain, message-only staleness warning when the pricing table is
    more than `months_threshold` months older than `today` — appended at boot and
    to every cost report so cost estimates never drift silently. NEVER fetches
    anything; the fix is to verify Anthropic's pricing page and edit config.
    Returns "" when the table is fresh (or the as_of date is unparsable — a bad
    value must not crash boot)."""
    as_of = pricing_as_of()
    today = today or _dt.date.today()
    if not as_of:
        return ("⚠️ Pricing table has no `costs.pricing_as_of` date set — add one "
                "and verify current rates at Anthropic's pricing page; cost "
                "estimates cannot be checked for staleness until you do.")
    try:
        parts = as_of.split("-")
        y, m = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return ""
    months = (today.year - y) * 12 + (today.month - m)
    if months > months_threshold:
        return (f"⚠️ Pricing table is {months} months old (as of {as_of}). Cost "
                f"estimates may be wrong — verify current rates at Anthropic's "
                f"pricing page and update `costs.pricing` in config.yml. "
                f"(Nothing is fetched automatically.)")
    return ""


def get_db_url() -> str:
    """Return DATABASE_URL from the environment, normalized to the
    'postgresql://' scheme psycopg expects (Railway sometimes hands out the
    legacy 'postgres://'). Empty string if unset → triggers JSON fallback."""
    load_env()
    url = os.getenv("DATABASE_URL", "").strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url
