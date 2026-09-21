"""Configuration for the ccfleet server, read from CCFLEET_* environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

ENV_PREFIX = "CCFLEET_"
MIN_ADMIN_TOKEN_LEN = 16


class ConfigError(ValueError):
    """Raised when the environment holds an unusable value."""


def _env_int(env: Mapping[str, str], key: str, default: int, minimum: int = 0) -> int:
    raw = env.get(ENV_PREFIX + key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_PREFIX}{key} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{ENV_PREFIX}{key} must be >= {minimum}, got {value}")
    return value


def _env_bool(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    """A flag is on for 1/true/yes/on, off for 0/false/no/off, and nothing else."""
    raw = env.get(ENV_PREFIX + key)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{ENV_PREFIX}{key} must be a boolean, got {raw!r}")


def _parse_bind(raw: str) -> tuple[str, int]:
    host, sep, port = raw.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ConfigError(f"{ENV_PREFIX}BIND must look like host:port, got {raw!r}")
    port_num = int(port)
    if not 1 <= port_num <= 65535:
        raise ConfigError(f"{ENV_PREFIX}BIND port out of range: {port_num}")
    return host, port_num


@dataclass(frozen=True)
class Config:
    """Immutable server settings. Build with :meth:`from_env`."""

    bind_host: str = "127.0.0.1"
    bind_port: int = 8110
    db_path: str = "data/ccfleet.db"
    admin_user: str = "admin"
    admin_token: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    public_url: str = ""
    heartbeat_max_age_s: int = 15 * 60
    token_stale_s: int = 24 * 3600
    token_expired_grace_s: int = 3600
    disk_warn_pct: int = 85
    disk_crit_pct: int = 95
    quota_warn_pct: int = 75
    quota_crit_pct: int = 90
    check_interval_s: int = 60
    retention_days: int = 30
    max_body_bytes: int = 64 * 1024
    # Off by default: bypass removes every permission prompt on a node whose owner
    # also holds passwordless sudo. An operator turns it on for their own fleet;
    # it never becomes the default for somebody else's.
    bypass_by_default: bool = False

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> Config:
        env = os.environ if env is None else env
        host, port = _parse_bind(env.get(ENV_PREFIX + "BIND", "127.0.0.1:8110"))
        cfg = cls(
            bind_host=host,
            bind_port=port,
            db_path=env.get(ENV_PREFIX + "DB", "data/ccfleet.db"),
            admin_user=env.get(ENV_PREFIX + "ADMIN_USER", "admin"),
            admin_token=env.get(ENV_PREFIX + "ADMIN_TOKEN", ""),
            telegram_bot_token=env.get(ENV_PREFIX + "TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=env.get(ENV_PREFIX + "TELEGRAM_CHAT_ID", ""),
            public_url=env.get(ENV_PREFIX + "PUBLIC_URL", "").rstrip("/"),
            heartbeat_max_age_s=_env_int(env, "HEARTBEAT_MAX_AGE_S", 15 * 60, 60),
            token_stale_s=_env_int(env, "TOKEN_STALE_S", 24 * 3600, 60),
            token_expired_grace_s=_env_int(env, "TOKEN_EXPIRED_GRACE_S", 3600, 0),
            disk_warn_pct=_env_int(env, "DISK_WARN_PCT", 85, 1),
            disk_crit_pct=_env_int(env, "DISK_CRIT_PCT", 95, 1),
            quota_warn_pct=_env_int(env, "QUOTA_WARN_PCT", 75, 1),
            quota_crit_pct=_env_int(env, "QUOTA_CRIT_PCT", 90, 1),
            check_interval_s=_env_int(env, "CHECK_INTERVAL_S", 60, 5),
            retention_days=_env_int(env, "RETENTION_DAYS", 30, 1),
            max_body_bytes=_env_int(env, "MAX_BODY_BYTES", 64 * 1024, 1024),
            bypass_by_default=_env_bool(env, "BYPASS_BY_DEFAULT", False),
        )
        if cfg.quota_warn_pct > cfg.quota_crit_pct:
            raise ConfigError(f"{ENV_PREFIX}QUOTA_WARN_PCT must be <= "
                              f"{ENV_PREFIX}QUOTA_CRIT_PCT")
        if cfg.disk_warn_pct > cfg.disk_crit_pct:
            raise ConfigError("CCFLEET_DISK_WARN_PCT must not exceed CCFLEET_DISK_CRIT_PCT")
        return cfg

    def require_admin_token(self) -> None:
        if len(self.admin_token) < MIN_ADMIN_TOKEN_LEN:
            raise ConfigError(
                f"{ENV_PREFIX}ADMIN_TOKEN must be set to at least {MIN_ADMIN_TOKEN_LEN} characters "
                "(generate one with: openssl rand -hex 32)"
            )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)
