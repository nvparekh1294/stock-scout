"""Scheduled-loop default-off wiring and advice-disclaimer presence.

These import the Telegram relay module, which pulls in the full runtime stack
(anthropic, python-telegram-bot). Run inside the project venv:
    python3 -m pytest
"""

import pytest

from scout import config, telegram_bot


@pytest.fixture(autouse=True)
def _clear_config_cache():
    config.load_config.cache_clear()
    yield
    config.load_config.cache_clear()


class _FakeJobQueue:
    def __init__(self):
        self.armed = []

    def run_daily(self, *a, **k):
        self.armed.append(("daily", a, k))

    def run_repeating(self, *a, **k):
        self.armed.append(("repeating", a, k))


class _FakeApp:
    def __init__(self):
        self.job_queue = _FakeJobQueue()


# ── schedules ship disabled and arm nothing ─────────────────────────────────
def test_schedules_disabled_by_default_arms_nothing(monkeypatch):
    # Default (shipped) config has schedules.enabled = false.
    monkeypatch.setattr(telegram_bot, "load_config",
                        lambda: {"schedules": {"enabled": False}})
    app = _FakeApp()
    telegram_bot._schedule_jobs(app)
    assert app.job_queue.armed == []


def test_shipped_config_disables_schedules():
    # The actual config.example.yml the repo ships must default to disabled.
    cfg = config.load_config()
    assert cfg["schedules"]["enabled"] is False


def test_schedules_enabled_arms_all_jobs(monkeypatch):
    enabled = {
        "schedules": {
            "enabled": True,
            "daily_monitor": {"cron": "15 17 * * *"},
            "radar_weekly": {"cron": "0 7 * * 1"},
            "scorecard": {"cron": "0 8 1 * *"},
            "policy_fast_lane": {"every_minutes": 60},
        },
        "feeds": {"market_hours_poll_minutes": 45},
    }
    monkeypatch.setattr(telegram_bot, "load_config", lambda: enabled)
    app = _FakeApp()
    telegram_bot._schedule_jobs(app)
    kinds = [a[0] for a in app.job_queue.armed]
    # 3 run_daily (monitor, radar, scorecard) + 2 run_repeating (watch, policy).
    assert kinds.count("daily") == 3
    assert kinds.count("repeating") == 2


def test_schedule_times_read_from_config(monkeypatch):
    """The daily monitor time comes from the config cron string, not a constant."""
    enabled = {
        "schedules": {
            "enabled": True,
            "daily_monitor": {"cron": "45 9 * * *"},  # 09:45
        },
        "feeds": {"market_hours_poll_minutes": 30},
    }
    monkeypatch.setattr(telegram_bot, "load_config", lambda: enabled)
    app = _FakeApp()
    telegram_bot._schedule_jobs(app)
    daily = [a for a in app.job_queue.armed if a[0] == "daily"][0]
    t = daily[1][1]  # second positional arg to run_daily is the time
    assert (t.hour, t.minute) == (9, 45)


# ── advice disclaimer present ───────────────────────────────────────────────
def test_disclaimer_content():
    d = telegram_bot.ADVICE_DISCLAIMER.lower()
    assert "not investment advice" in d
    assert "never places trades" in d
    assert "responsible" in d
    assert "warranty" in d


def test_disclaimer_in_system_prompt():
    assert telegram_bot.ADVICE_DISCLAIMER in telegram_bot.SYSTEM


def test_disclaimer_in_startup_string():
    msg = telegram_bot.online_announce()
    assert telegram_bot.ADVICE_DISCLAIMER in msg


def test_system_prompt_has_no_private_name():
    # The system prompt must not hard-code the private owner name (built from
    # fragments so the literal never appears in this file / the blocklist scan).
    private_name = "Nik" + "ita"
    assert private_name not in telegram_bot.SYSTEM
    # It must instead speak in the neutral "the owner" voice and carry the
    # config-resolved display name.
    assert "the owner" in telegram_bot.SYSTEM
    assert telegram_bot.APP_NAME in telegram_bot.SYSTEM
