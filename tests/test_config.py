"""Missing trading flags stay off. Reaktor is required only when cTrader bars are unavailable."""

import pytest

from poc.config import Settings


def test_missing_trading_flags_stay_safe(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini")
    monkeypatch.delenv("FACTOR_MODE", raising=False)
    monkeypatch.delenv("TRADING_ENABLED", raising=False)
    monkeypatch.delenv("REAKTOR_SUPABASE_URL", raising=False)
    monkeypatch.delenv("REAKTOR_SUPABASE_KEY", raising=False)
    settings = Settings.from_env()
    assert settings.factor_mode == "stub"
    assert settings.trading_enabled is False
    with pytest.raises(SystemExit, match="REAKTOR_SUPABASE_URL"):
        settings.require_runtime()


def test_http_mode_starts_without_reaktor(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini")
    monkeypatch.setenv("FACTOR_MODE", "http")
    monkeypatch.delenv("REAKTOR_SUPABASE_URL", raising=False)
    monkeypatch.delenv("REAKTOR_SUPABASE_KEY", raising=False)
    settings = Settings.from_env()
    settings.require_runtime()
    assert settings.factor_mode == "http"
