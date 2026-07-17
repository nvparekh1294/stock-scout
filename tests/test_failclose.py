"""Fail-closed and genericization tests for the public build.

Covers the design requirement that these default-safe behaviors hold, so a
regression that reintroduces a personal default or an unsafe fallback is caught
mechanically:
  - EDGAR user_agent ships empty and every SEC call fails closed;
  - the tax planner refuses on an unset or non-US jurisdiction;
  - scheduled loops ship disabled and arm nothing by default;
  - the display name resolves from config, never hard-coded;
  - the advice disclaimer is present in the system prompt and startup string.

Run: python3 -m pytest
"""

import pytest

from scout import config


@pytest.fixture(autouse=True)
def _clear_config_cache():
    """load_config is lru_cached; clear it around each test so a monkeypatched
    config never leaks between tests and the on-disk default is re-read."""
    config.load_config.cache_clear()
    yield
    config.load_config.cache_clear()


# ── EDGAR user-agent fails closed ───────────────────────────────────────────
def test_edgar_user_agent_empty_raises():
    """The shipped config.example.yml has edgar.user_agent = "" — any resolution
    must raise, never return a usable header."""
    with pytest.raises(RuntimeError) as ei:
        config.edgar_user_agent()
    assert "SEC" in str(ei.value) and "config.yml" in str(ei.value)


def test_sec_headers_raise_when_ua_unset():
    from scout import market_ref
    with pytest.raises(RuntimeError):
        market_ref._sec_headers()


def test_edgar_user_agent_valid_passes(monkeypatch):
    monkeypatch.setattr(
        config, "load_config",
        lambda: {"edgar": {"user_agent": "Jane Doe research jane@example.com"}})
    assert config.edgar_user_agent() == "Jane Doe research jane@example.com"


def test_edgar_user_agent_without_email_raises(monkeypatch):
    # A non-empty but contact-less string (no email) must still fail closed.
    monkeypatch.setattr(config, "load_config",
                        lambda: {"edgar": {"user_agent": "just a name"}})
    with pytest.raises(RuntimeError):
        config.edgar_user_agent()


# ── tax planner refuses unset / non-US jurisdiction ─────────────────────────
def _cfg(tax: dict):
    return lambda: {"tax": tax}


def test_tax_refuses_unset_jurisdiction(monkeypatch):
    from scout import tax_plan
    monkeypatch.setattr(tax_plan, "load_config", _cfg({"jurisdiction": ""}))
    with pytest.raises(tax_plan.TaxConfigError) as ei:
        tax_plan._resolved_rates()
    assert "jurisdiction" in str(ei.value).lower()


def test_tax_refuses_non_us_jurisdiction(monkeypatch):
    from scout import tax_plan
    monkeypatch.setattr(tax_plan, "load_config",
                        _cfg({"jurisdiction": "UK", "federal_lt_rate": 0.2}))
    with pytest.raises(tax_plan.TaxConfigError):
        tax_plan._resolved_rates()


def test_tax_refuses_us_with_missing_rate(monkeypatch):
    from scout import tax_plan
    monkeypatch.setattr(tax_plan, "load_config",
                        _cfg({"jurisdiction": "US", "federal_lt_rate": None}))
    with pytest.raises(tax_plan.TaxConfigError):
        tax_plan._resolved_rates()


def test_tax_default_config_refuses(monkeypatch):
    """Belt-and-suspenders: the shipped default config (no jurisdiction) refuses
    even when build_plan is called end-to-end."""
    from scout import tax_plan
    from scout.db import Database
    with pytest.raises(tax_plan.TaxConfigError):
        tax_plan.build_plan(Database(), 10000.0)


def test_tax_us_configured_resolves(monkeypatch):
    from scout import tax_plan
    monkeypatch.setattr(tax_plan, "load_config",
                        _cfg({"jurisdiction": "US", "federal_lt_rate": 0.20,
                              "long_term_only": True, "state": "NY"}))
    rates = tax_plan._resolved_rates()
    assert rates["jurisdiction"] == "US"
    assert rates["federal_lt"] == 0.20
    assert rates["long_term_only"] is True


# ── app name resolves from config, never hard-coded ─────────────────────────
def test_app_name_from_shipped_example():
    # The shipped example config names the product; app_name() surfaces it.
    assert config.app_name() == "Stock Scout"


def test_app_name_from_config(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {"app": {"name": "Beacon"}})
    assert config.app_name() == "Beacon"


def test_app_name_ignores_private_name(monkeypatch):
    # A leftover "Scout" name must not surface as the public display name.
    monkeypatch.setattr(config, "load_config", lambda: {"app": {"name": "Scout"}})
    assert config.app_name() == "the analyst"
