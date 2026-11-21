"""Configuration for the ccfleet server, read from CCFLEET_* environment variables."""

from __future__ import annotations

import os
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

from .sessions import MIN_TTL_S as SESSION_MIN_TTL_S

ENV_PREFIX = "CCFLEET_"
MIN_ADMIN_TOKEN_LEN = 16
#: An address to publish, loosely: one @, no spaces, nothing that could leave
#: an attribute. It is shown on a public page, so it is checked, not trusted.
CONTACT_EMAIL_RE = re.compile(r'^[^@\s<>"\']{1,64}@[^@\s<>"\']+\.[^@\s<>"\']{2,}$')
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)"
                         r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


# An address, or a display name and an address in angle brackets: what an
# email's From says.
_ADDRESS = r"[^\s<>@\"]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
EMAIL_FROM_RE = re.compile(rf"{_ADDRESS}|[^<>@\r\n]{{1,64}} <{_ADDRESS}>")

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


def _public_origin(value: str) -> str:
    """An operator-configured HTTPS origin; loopback HTTP is for local tests."""
    if (not isinstance(value, str) or not value or len(value) > 512 or not value.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        raise ConfigError("public URL aliases must be clean HTTPS origins")
    try:
        parsed = urllib.parse.urlsplit(value)
        host, port = parsed.hostname, parsed.port
        local = host in {"localhost", "127.0.0.1", "::1"}
        if (not host or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or not (parsed.scheme == "https" or parsed.scheme == "http" and local)
                or not (local or HOSTNAME_RE.fullmatch(host))
                or port is not None and not 1 <= port <= 65535):
            raise ValueError
    except ValueError:
        raise ConfigError("public URL aliases must be clean HTTPS origins") from None
    host = "[" + host + "]" if ":" in host else host
    default = 443 if parsed.scheme == "https" else 80
    return parsed.scheme + "://" + host + (":" + str(port) if port not in (None, default) else "")


def _public_aliases(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    values = tuple(_public_origin(value.strip()) for value in raw.split(","))
    if len(values) > 8 or len(set(values)) != len(values):
        raise ConfigError("public URL aliases must be distinct, with at most eight origins")
    return values


def _authority_matches(header: Optional[str], origin: str) -> bool:
    """Never reflect Host or forwarded headers into a credential destination."""
    if (not isinstance(header, str) or not header or len(header) > 512 or not header.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in header)):
        return False
    try:
        supplied = urllib.parse.urlsplit("//" + header)
        configured = urllib.parse.urlsplit(origin)
        default = 443 if configured.scheme == "https" else 80
        return bool(supplied.hostname and supplied.username is None and supplied.password is None
            and not supplied.path and not supplied.query and not supplied.fragment
            and not header.endswith(":")
            and (supplied.port is None or 1 <= supplied.port <= 65535)
            and supplied.hostname == configured.hostname
            and (supplied.port or default) == (configured.port or default))
    except ValueError:
        return False


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
    # Optional migration origins. Signed clients keep their saved origin; aliases
    # serve the same state without HTTP redirects or credential retargeting.
    public_url_aliases: tuple[str, ...] = ()
    broker_only: bool = False
    usd_slot_credit_enabled: bool = False
    stripe_topup_enabled: bool = False
    stripe_product_approved: bool = False
    stripe_mode: str = "test"
    stripe_expected_account_id: str = ""
    stripe_api_key_file: str = ""
    stripe_webhook_secret_file: str = ""
    stripe_sandbox_database: bool = False
    # Enrollment may be off while existing recurring contracts continue processing.
    stripe_subscriptions_enabled: bool = False
    stripe_subscription_webhook_secret_file: str = ""
    stripe_subscription_portal_configuration_id: str = ""
    # Public model requests are an explicit, separate authority from opaque SSH.
    gateway_enabled: bool = False
    devin_trial_enabled: bool = False
    devin_trial_directory: str = ""
    devin_trial_worker_file: str = ""
    devin_trial_ssh_key_file: str = ""
    # Native compatibility requires this opt-in and an exact approved runtime profile.
    gateway_devin_enabled: bool = False
    gateway_devin_native_version: str = ""
    gateway_devin_adapter_revision: str = ""
    gateway_devin_model_map_digest: str = ""
    gateway_ssh_key_path: str = ""
    gateway_ssh_public_key: str = ""
    gateway_max_active: int = 16
    gateway_max_per_key: int = 8
    gateway_max_queued: int = 16
    gateway_max_queued_per_key: int = 8
    gateway_queue_timeout_s: int = 30
    gateway_max_body_bytes: int = 16 * 1024 * 1024
    gateway_body_budget_bytes: int = 32 * 1024 * 1024
    gateway_max_connections: int = 128
    gateway_header_timeout_s: int = 10
    gateway_request_timeout_s: int = 900
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
    # Public infrastructure figures are an operator opt-in, separate from user traffic.
    public_machine_metrics_enabled: bool = False
    # Off by default: bypass removes every permission prompt on a node whose owner
    # also holds passwordless sudo. An operator turns it on for their own fleet;
    # it never becomes the default for somebody else's.
    bypass_by_default: bool = False

    # -- signing in with Google ------------------------------------------
    # Absent by default: a fleet with no Google credentials still runs as the
    # operator-only console it is today, and simply offers no user sign-in.
    google_client_id: str = ""
    google_client_secret: str = ""
    # Where Google sends people back. Derived from public_url when not set,
    # because a redirect that disagrees with the one registered at Google
    # fails with a message that says nothing useful.
    google_redirect_uri: str = ""
    # Signs the session cookie. Without it there are no sessions at all — an
    # unsigned cookie is a string the browser can write, and guessing a default
    # would mean every deployment that forgot to set one shares a key.
    cookie_secret: str = ""
    session_ttl_s: int = 14 * 24 * 3600
    # Off only for local development over plain http, where a Secure cookie is
    # dropped and sign-in appears to do nothing at all.
    cookie_secure: bool = True
    # The operator's own hostname. Unset, one hostname serves both the console
    # and the user site, exactly as before. Set, the console and its API answer
    # only here (and on loopback, the operator's way in from the box itself),
    # and every other hostname is the product: somebody arriving there sees
    # the user site and never a console, whatever credentials they carry.
    admin_host: str = ""
    # Shown on the public privacy page as the way to reach the operator. Empty,
    # the page says to use the support address on Google's sign-in screen, so
    # nobody's address is published unless the operator chose to publish it.
    contact_email: str = ""
    # Outage emails (see ccfleetd/mail.py): a Resend send-only key and the
    # address they come from, e.g. "ccfleet <ccfleet@9relay.com>". Either one
    # empty, nothing is sent and the pages offer no emails.
    resend_api_key: str = ""
    email_from: str = ""

    @property
    def emails_ready(self) -> bool:
        """Whether outage emails can be sent, and so offered."""
        return bool(self.resend_api_key and self.email_from)

    @property
    def gateway_ready(self) -> bool:
        return bool(self.gateway_enabled and not self.broker_only
                    and self.gateway_ssh_key_path and self.gateway_ssh_public_key
                    and self.public_url)

    @property
    def google_ready(self) -> bool:
        """Whether user sign-in can be offered at all."""
        return bool(self.google_client_id and self.google_client_secret
                    and self.redirect_uri and self.cookie_secret)

    @property
    def redirect_uri(self) -> str:
        if self.google_redirect_uri:
            return self.google_redirect_uri
        return f"{self.public_url}/auth/google/callback" if self.public_url else ""

    @property
    def admin_redirect_uri(self) -> str:
        """Where Google sends an operator back to: the admin host's own
        callback, because the session is a cookie that host alone can set."""
        if not self.admin_host:
            return ""
        scheme = "http" if self.public_url.startswith("http://") else "https"
        return f"{scheme}://{self.admin_host}/auth/google/callback"

    def public_origin_for_host(self, host: Optional[str]) -> str:
        """Return only a configured origin, preserving old clients during migration.

        Unconfigured deployments retain their existing one-origin behavior.
        Enabling aliases opts into strict canonical/alias Host admission.
        """
        if not self.public_url_aliases:
            return self.public_url
        try:
            origins = tuple(_public_origin(value) for value in
                            (self.public_url, *self.public_url_aliases))
            return next((origin for origin in origins if _authority_matches(host, origin)), "")
        except (ConfigError, TypeError, ValueError):
            return ""

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
            public_url_aliases=_public_aliases(env.get(ENV_PREFIX + "PUBLIC_URL_ALIASES", "")),
            broker_only=_env_bool(env, "BROKER_ONLY", False),
            usd_slot_credit_enabled=_env_bool(env, "USD_SLOT_CREDIT_ENABLED", False),
            stripe_topup_enabled=_env_bool(env, "STRIPE_TOPUP_ENABLED", False),
            stripe_product_approved=_env_bool(env, "STRIPE_PRODUCT_APPROVED", False),
            stripe_mode=env.get(ENV_PREFIX + "STRIPE_MODE", "test"),
            stripe_expected_account_id=env.get(ENV_PREFIX + "STRIPE_EXPECTED_ACCOUNT_ID", ""),
            stripe_api_key_file=env.get(ENV_PREFIX + "STRIPE_API_KEY_FILE", ""),
            stripe_webhook_secret_file=env.get(ENV_PREFIX + "STRIPE_WEBHOOK_SECRET_FILE", ""),
            stripe_sandbox_database=_env_bool(env, "STRIPE_SANDBOX_DATABASE", False),
            stripe_subscriptions_enabled=_env_bool(env, "STRIPE_SUBSCRIPTIONS_ENABLED", False),
            stripe_subscription_webhook_secret_file=env.get(
                ENV_PREFIX + "STRIPE_SUBSCRIPTION_WEBHOOK_SECRET_FILE", ""),
            stripe_subscription_portal_configuration_id=env.get(
                ENV_PREFIX + "STRIPE_SUBSCRIPTION_PORTAL_CONFIGURATION_ID", ""),
            gateway_enabled=_env_bool(env, "GATEWAY_ENABLED", False),
            devin_trial_enabled=_env_bool(env, "DEVIN_TRIAL_ENABLED", False),
            devin_trial_directory=env.get(ENV_PREFIX + "DEVIN_TRIAL_DIRECTORY", ""),
            devin_trial_worker_file=env.get(ENV_PREFIX + "DEVIN_TRIAL_WORKER_FILE", ""),
            devin_trial_ssh_key_file=env.get(ENV_PREFIX + "DEVIN_TRIAL_SSH_KEY_FILE", ""),
            gateway_devin_enabled=_env_bool(env, "GATEWAY_DEVIN_ENABLED", False),
            gateway_devin_native_version=env.get(
                ENV_PREFIX + "GATEWAY_DEVIN_NATIVE_VERSION", "").strip(),
            gateway_devin_adapter_revision=env.get(
                ENV_PREFIX + "GATEWAY_DEVIN_ADAPTER_REVISION", "").strip(),
            gateway_devin_model_map_digest=env.get(
                ENV_PREFIX + "GATEWAY_DEVIN_MODEL_MAP_DIGEST", "").strip(),
            gateway_ssh_key_path=env.get(ENV_PREFIX + "GATEWAY_SSH_KEY_PATH", "").strip(),
            gateway_ssh_public_key=env.get(ENV_PREFIX + "GATEWAY_SSH_PUBLIC_KEY", "").strip(),
            gateway_max_active=_env_int(env, "GATEWAY_MAX_ACTIVE", 16, 1),
            gateway_max_per_key=_env_int(env, "GATEWAY_MAX_PER_KEY", 8, 1),
            gateway_max_queued=_env_int(env, "GATEWAY_MAX_QUEUED", 16, 1),
            gateway_max_queued_per_key=_env_int(env, "GATEWAY_MAX_QUEUED_PER_KEY", 8, 1),
            gateway_queue_timeout_s=_env_int(env, "GATEWAY_QUEUE_TIMEOUT_S", 30, 0),
            gateway_max_body_bytes=_env_int(env, "GATEWAY_MAX_BODY_BYTES", 16*1024*1024, 1024),
            gateway_body_budget_bytes=_env_int(
                env, "GATEWAY_BODY_BUDGET_BYTES", 32*1024*1024, 1024),
            gateway_max_connections=_env_int(env, "GATEWAY_MAX_CONNECTIONS", 128, 8),
            gateway_header_timeout_s=_env_int(env, "GATEWAY_HEADER_TIMEOUT_S", 10, 1),
            gateway_request_timeout_s=_env_int(env, "GATEWAY_REQUEST_TIMEOUT_S", 900, 1),
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
            public_machine_metrics_enabled=_env_bool(env, "PUBLIC_MACHINE_METRICS_ENABLED", False),
            bypass_by_default=_env_bool(env, "BYPASS_BY_DEFAULT", False),
            google_client_id=env.get(ENV_PREFIX + "GOOGLE_CLIENT_ID", ""),
            google_client_secret=env.get(ENV_PREFIX + "GOOGLE_CLIENT_SECRET", ""),
            google_redirect_uri=env.get(ENV_PREFIX + "GOOGLE_REDIRECT_URI", ""),
            cookie_secret=env.get(ENV_PREFIX + "COOKIE_SECRET", ""),
            session_ttl_s=_env_int(env, "SESSION_TTL_S", 14 * 24 * 3600,
                                   SESSION_MIN_TTL_S),
            cookie_secure=_env_bool(env, "COOKIE_SECURE", True),
            admin_host=env.get(ENV_PREFIX + "ADMIN_HOST", "").strip().lower(),
            contact_email=env.get(ENV_PREFIX + "CONTACT_EMAIL", "").strip(),
            resend_api_key=env.get(ENV_PREFIX + "RESEND_API_KEY", "").strip(),
            email_from=env.get(ENV_PREFIX + "EMAIL_FROM", "").strip(),
        )
        # Outage emails are both settings or neither: one alone would offer
        # emails nobody sends, or send from an address that is no address.
        if bool(cfg.resend_api_key) != bool(cfg.email_from):
            raise ConfigError(
                f"{ENV_PREFIX}RESEND_API_KEY and {ENV_PREFIX}EMAIL_FROM go together: "
                "set both for outage emails, or neither")
        if cfg.email_from and not EMAIL_FROM_RE.fullmatch(cfg.email_from):
            raise ConfigError(
                f"{ENV_PREFIX}EMAIL_FROM must be an address, or a name and an address, like "
                f"ccfleet <status@example.com>; got {cfg.email_from!r}")
        if cfg.contact_email and (len(cfg.contact_email) > 254
                                  or not CONTACT_EMAIL_RE.match(cfg.contact_email)):
            raise ConfigError(
                f"{ENV_PREFIX}CONTACT_EMAIL must be one email address, like "
                f"help@example.com; got {cfg.contact_email!r}")
        if cfg.admin_host and not HOSTNAME_RE.match(cfg.admin_host):
            raise ConfigError(
                f"{ENV_PREFIX}ADMIN_HOST must be a hostname like admin.fleet.example.com, "
                f"with no scheme, port or path; got {cfg.admin_host!r}")
        if cfg.public_url_aliases:
            canonical = _public_origin(cfg.public_url)
            if (canonical in cfg.public_url_aliases
                    or cfg.admin_host and any(
                        urllib.parse.urlsplit(origin).hostname == cfg.admin_host
                        for origin in (canonical, *cfg.public_url_aliases))):
                raise ConfigError("public URL aliases must exclude the canonical and admin origins")
        # Half-configured sign-in is worse than none: the button appears and
        # then fails on the callback, which reads as the product being broken.
        google_bits = {
            "GOOGLE_CLIENT_ID": cfg.google_client_id,
            "GOOGLE_CLIENT_SECRET": cfg.google_client_secret,
            "COOKIE_SECRET": cfg.cookie_secret,
        }
        given = {k for k, v in google_bits.items() if v}
        if given and given != set(google_bits):
            missing = sorted(set(google_bits) - given)
            raise ConfigError(
                f"Google sign-in needs all of {ENV_PREFIX}GOOGLE_CLIENT_ID, "
                f"{ENV_PREFIX}GOOGLE_CLIENT_SECRET and {ENV_PREFIX}COOKIE_SECRET; "
                f"missing " + ", ".join(ENV_PREFIX + m for m in missing))
        if given and not cfg.redirect_uri:
            raise ConfigError(
                f"Google sign-in needs {ENV_PREFIX}PUBLIC_URL or "
                f"{ENV_PREFIX}GOOGLE_REDIRECT_URI so Google knows where to "
                f"send people back")
        if cfg.quota_warn_pct > cfg.quota_crit_pct:
            raise ConfigError(f"{ENV_PREFIX}QUOTA_WARN_PCT must be <= "
                              f"{ENV_PREFIX}QUOTA_CRIT_PCT")
        if cfg.disk_warn_pct > cfg.disk_crit_pct:
            raise ConfigError("CCFLEET_DISK_WARN_PCT must not exceed CCFLEET_DISK_CRIT_PCT")
        if (cfg.gateway_max_active > 64 or cfg.gateway_max_per_key > cfg.gateway_max_active
                or cfg.gateway_max_queued > 64
                or cfg.gateway_max_queued_per_key > cfg.gateway_max_queued
                or cfg.gateway_queue_timeout_s > 60
                or cfg.gateway_max_connections > 256
                or cfg.gateway_header_timeout_s > 30 or cfg.gateway_request_timeout_s > 1800
                or cfg.gateway_max_body_bytes > 64 * 1024 * 1024
                or cfg.gateway_body_budget_bytes < cfg.gateway_max_body_bytes
                or cfg.gateway_body_budget_bytes > 128 * 1024 * 1024):
            raise ConfigError("CCFLEET_GATEWAY resource limits are inconsistent or too large")
        if (cfg.gateway_devin_native_version and not re.fullmatch(
                r"[0-9]{1,6}(?:\.[0-9]{1,6}){2}", cfg.gateway_devin_native_version)):
            raise ConfigError("CCFLEET_GATEWAY_DEVIN_NATIVE_VERSION must be "
                              "an exact native version")
        for suffix, value in (("ADAPTER_REVISION", cfg.gateway_devin_adapter_revision),
                              ("MODEL_MAP_DIGEST", cfg.gateway_devin_model_map_digest)):
            if value and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ConfigError("CCFLEET_GATEWAY_DEVIN_" + suffix + " must be a SHA-256 digest")
        if cfg.gateway_enabled:
            from . import cli_access
            public = urllib.parse.urlsplit(cfg.public_url)
            secure = public.scheme == "https" or (public.scheme == "http"
                and public.hostname in {"localhost", "127.0.0.1", "::1"})
            if (not cfg.gateway_ready or not os.path.isabs(cfg.gateway_ssh_key_path)
                    or cli_access.normalize_public_key(cfg.gateway_ssh_public_key) is None
                    or not secure or not public.hostname or public.username or public.password
                    or public.path or public.query or public.fragment):
                raise ConfigError("CCFLEET_GATEWAY needs its private service key path, "
                                  "public key and HTTPS public URL (loopback HTTP for tests)")
        from . import trial_runtime
        trial_runtime.validate(cfg)
        from . import stripe_config
        stripe_config.validate(cfg)
        from . import subscription_config
        subscription_config.validate(cfg)
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
