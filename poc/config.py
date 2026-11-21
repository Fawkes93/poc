"""Environment configuration. Trading defaults off. Factor HTTP is opt-in."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass
class Settings:
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    gemini_fallback_model: str = "gemini-4-hydrogen"
    gemini_attempts: int = 3
    grok_api_key: str = ""
    grok_model: str = "grok-4.7"
    grok_timeout_sec: float = 180.0
    twelvedata_api_key: str = ""
    reaktor_supabase_url: str = ""
    reaktor_supabase_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    factor_base_url: str = ""
    factor_api_token: str = ""
    factor_account_id: str = ""
    factor_mode: str = "stub"
    trading_enabled: bool = False
    label: str = "POC"
    candle_symbol: str = "XAU/USD"
    broker_symbol: str = "XAUUSD"
    volume_lots: float = 0.01
    oz_per_lot: float = 100.0
    max_loss_usd: float = 25.0
    min_stop_usd: float = 5.0
    max_entry_distance: float = 200.0
    min_rr: float = 1.2
    min_confidence: float = 60.0
    max_daily_loss_usd: float = 25.0
    sr_cluster_usd: float = 8.0
    history_days: int = 120
    ai_timeout_sec: float = 90.0
    alert_poll_sec: int = 60
    alert_debounce_sec: int = 600
    heartbeat_hours: int = 12
    log_http: bool = True
    log_http_host: str = "127.0.0.1"
    log_http_port: int = 8787
    max_alerts: int = 4
    check_minute: int = 5
    db_path: str = ""

    def require_runtime(self) -> None:
        missing = []
        if not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        if self.factor_mode != "http":
            if not self.reaktor_supabase_url:
                missing.append("REAKTOR_SUPABASE_URL")
            if not self.reaktor_supabase_key:
                missing.append("REAKTOR_SUPABASE_KEY")
        if missing:
            raise SystemExit("missing required env: " + ", ".join(missing))

    def account_id_int(self) -> int | None:
        raw = self.factor_account_id.strip()
        if not raw:
            return None
        return int(raw)

    def database_path(self) -> Path:
        if self.db_path:
            return Path(self.db_path)
        return ROOT / "poc.sqlite"

    @classmethod
    def from_env(cls) -> "Settings":
        attempts = _int("GEMINI_ATTEMPTS", 3)
        grok_key = os.getenv("XAI_API_KEY", "").strip() or os.getenv("GROK_API_KEY", "").strip()
        return cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip() or "gemini-3.6-flash",
            gemini_fallback_model=(
                os.getenv("GEMINI_FALLBACK_MODEL", "gemini-4-hydrogen").strip() or "gemini-4-hydrogen"
            ),
            gemini_attempts=attempts if attempts > 0 else 3,
            grok_api_key=grok_key,
            grok_model=os.getenv("GROK_MODEL", "grok-4.7").strip() or "grok-4.7",
            grok_timeout_sec=_float("GROK_TIMEOUT_SEC", 180.0),
            twelvedata_api_key=os.getenv("TWELVEDATA_API_KEY", "").strip(),
            reaktor_supabase_url=os.getenv("REAKTOR_SUPABASE_URL", "").strip().rstrip("/"),
            reaktor_supabase_key=os.getenv("REAKTOR_SUPABASE_KEY", "").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            factor_base_url=os.getenv("FACTOR_BASE_URL", "").strip().rstrip("/"),
            factor_api_token=os.getenv("FACTOR_API_TOKEN", "").strip(),
            factor_account_id=os.getenv("FACTOR_ACCOUNT_ID", "").strip(),
            factor_mode=(os.getenv("FACTOR_MODE", "stub").strip().lower() or "stub"),
            trading_enabled=_bool("TRADING_ENABLED", False),
            label=os.getenv("AI_LABEL", "POC").strip() or "POC",
            volume_lots=_float("VOLUME_LOTS", 0.01),
            max_loss_usd=_float("MAX_LOSS_USD", 25.0),
            min_stop_usd=_float("MIN_STOP_USD", 5.0),
            min_rr=_float("MIN_RR", 1.2),
            min_confidence=_float("MIN_CONFIDENCE", 60.0),
            max_daily_loss_usd=_float("MAX_DAILY_LOSS_USD", 25.0),
            sr_cluster_usd=_float("SR_CLUSTER_USD", 8.0),
            history_days=_int("HISTORY_DAYS", 120),
            ai_timeout_sec=_float("AI_TIMEOUT_SEC", 90.0),
            alert_poll_sec=_int("ALERT_POLL_SEC", 60),
            alert_debounce_sec=_int("ALERT_DEBOUNCE_SEC", 600),
            heartbeat_hours=_int("HEARTBEAT_HOURS", 12),
            log_http=_bool("LOG_HTTP", True),
            log_http_host=os.getenv("LOG_HTTP_HOST", "127.0.0.1").strip() or "127.0.0.1",
            log_http_port=_int("LOG_HTTP_PORT", 8787),
            db_path=os.getenv("POC_DB", "").strip(),
        )
