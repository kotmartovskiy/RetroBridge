"""Gateway runtime shell: configuration (ARCHITECTURE §3.1).

Configuration is a small JSON document plus environment overrides — no extra
dependencies, everything readable in one file. Validation is strict: bad
values fail fast at startup instead of at request time.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from core.logging import LEVELS
from core.policy import EgressPolicy, validate_schemes

CONFIG_SCHEMA = "retrobridge/gateway-config@1"

DEFAULT_ALLOWED_HOSTS: Tuple[str, ...] = ()


class ConfigError(ValueError):
    """Configuration file or environment is invalid."""


@dataclass(frozen=True)
class ListenConfig:
    host: str = "127.0.0.1"
    port: int = 8080

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1", "[::1]")

    @property
    def is_wildcard(self) -> bool:
        return self.host in ("0.0.0.0", "::", "")

    @property
    def authority(self) -> str:
        host = "[%s]" % self.host if ":" in self.host and not self.host.startswith("[") else self.host
        return "%s:%d" % (host, self.port)


@dataclass(frozen=True)
class LimitsConfig:
    max_request_line_bytes: int = 2048
    max_header_line_bytes: int = 2048
    max_header_count: int = 64
    max_request_header_bytes: int = 8192
    max_request_body_bytes: int = 65536
    max_response_body_bytes: int = 1_048_576
    max_response_header_bytes: int = 16_384


@dataclass(frozen=True)
class TimeoutsConfig:
    client_read_s: float = 15.0
    client_write_s: float = 15.0
    upstream_s: float = 10.0


@dataclass(frozen=True)
class Config:
    listen: ListenConfig = ListenConfig()
    limits: LimitsConfig = LimitsConfig()
    timeouts: TimeoutsConfig = TimeoutsConfig()
    egress: EgressPolicy = EgressPolicy()
    profiles_dir: str = ""
    log_level: str = "info"
    diagnostics_headers: bool = False

    @property
    def gateway_authority(self) -> str:
        return self.listen.authority

    @property
    def self_addresses(self) -> Tuple[str, ...]:
        """Hosts that mean "the gateway itself" for the self-target guard."""
        if self.listen.is_wildcard:
            return ("127.0.0.1", "localhost", "::1")
        return (self.listen.host.lower(),)


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError("%s must be a positive integer" % name)
    return value


def _positive_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError("%s must be a positive number" % name)
    return float(value)


def _str_list(value: Any, name: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("%s must be a list of strings" % name)
    return tuple(value)


def from_dict(document: Optional[Mapping[str, Any]], base_dir: Optional[Path] = None) -> Config:
    """Build a validated :class:`Config`; ``base_dir`` resolves relative paths."""
    if document is None:
        document = {}
    if not isinstance(document, Mapping):
        raise ConfigError("config root must be a JSON object")

    schema = document.get("schema", CONFIG_SCHEMA)
    if schema != CONFIG_SCHEMA:
        raise ConfigError("unsupported config schema: %r" % schema)

    listen_raw = document.get("listen", {})
    if not isinstance(listen_raw, Mapping):
        raise ConfigError("listen must be an object")
    host = listen_raw.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("listen.host must be a non-empty string")
    port = listen_raw.get("port", 8080)
    # port 0 = ask the OS for an ephemeral port (used by tests and "any port" setups)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ConfigError("listen.port must be an integer 0-65535")
    listen = ListenConfig(host=host.strip(), port=port)

    limits_raw = document.get("limits", {})
    if not isinstance(limits_raw, Mapping):
        raise ConfigError("limits must be an object")
    limits = LimitsConfig(
        max_request_line_bytes=_positive_int(
            limits_raw.get("max_request_line_bytes", 2048), "limits.max_request_line_bytes"
        ),
        max_header_line_bytes=_positive_int(
            limits_raw.get("max_header_line_bytes", 2048), "limits.max_header_line_bytes"
        ),
        max_header_count=_positive_int(
            limits_raw.get("max_header_count", 64), "limits.max_header_count"
        ),
        max_request_header_bytes=_positive_int(
            limits_raw.get("max_request_header_bytes", 8192), "limits.max_request_header_bytes"
        ),
        max_request_body_bytes=_positive_int(
            limits_raw.get("max_request_body_bytes", 65536), "limits.max_request_body_bytes"
        ),
        max_response_body_bytes=_positive_int(
            limits_raw.get("max_response_body_bytes", 1_048_576),
            "limits.max_response_body_bytes",
        ),
        max_response_header_bytes=_positive_int(
            limits_raw.get("max_response_header_bytes", 16_384),
            "limits.max_response_header_bytes",
        ),
    )

    timeouts_raw = document.get("timeouts", {})
    if not isinstance(timeouts_raw, Mapping):
        raise ConfigError("timeouts must be an object")
    timeouts = TimeoutsConfig(
        client_read_s=_positive_float(
            timeouts_raw.get("client_read_s", 15.0), "timeouts.client_read_s"
        ),
        client_write_s=_positive_float(
            timeouts_raw.get("client_write_s", 15.0), "timeouts.client_write_s"
        ),
        upstream_s=_positive_float(timeouts_raw.get("upstream_s", 10.0), "timeouts.upstream_s"),
    )

    egress_raw = document.get("egress", {})
    if not isinstance(egress_raw, Mapping):
        raise ConfigError("egress must be an object")
    allowed_schemes = _str_list(
        egress_raw.get("allowed_schemes", ["https"]), "egress.allowed_schemes"
    )
    try:
        allowed_schemes = validate_schemes(allowed_schemes)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    allowed_hosts = _str_list(egress_raw.get("allowed_hosts", list(DEFAULT_ALLOWED_HOSTS)),
                              "egress.allowed_hosts")
    allowed_ports = egress_raw.get("allowed_ports", [80, 443])
    if not isinstance(allowed_ports, list) or not allowed_ports:
        raise ConfigError("egress.allowed_ports must be a non-empty list of ports")
    deny_private = egress_raw.get("deny_private_ranges", True)
    if not isinstance(deny_private, bool):
        raise ConfigError("egress.deny_private_ranges must be a boolean")
    ca_file = egress_raw.get("ca_file")
    if ca_file is not None and not isinstance(ca_file, str):
        raise ConfigError("egress.ca_file must be a string or null")
    try:
        egress = EgressPolicy(
            allowed_schemes=allowed_schemes,
            allowed_hosts=allowed_hosts,
            allowed_ports=tuple(allowed_ports),
            deny_private_ranges=deny_private,
            ca_file=ca_file,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    profiles_dir = document.get("profiles_dir", "examples/profiles")
    if not isinstance(profiles_dir, str) or not profiles_dir:
        raise ConfigError("profiles_dir must be a non-empty string")
    profiles_path = Path(profiles_dir)
    if not profiles_path.is_absolute() and base_dir is not None:
        profiles_path = base_dir / profiles_path

    log_level = document.get("log_level", "info")
    if not isinstance(log_level, str) or log_level.lower() not in LEVELS:
        raise ConfigError("log_level must be one of: %s" % ", ".join(sorted(LEVELS)))
    diagnostics = document.get("diagnostics_headers", False)
    if not isinstance(diagnostics, bool):
        raise ConfigError("diagnostics_headers must be a boolean")

    return Config(
        listen=listen,
        limits=limits,
        timeouts=timeouts,
        egress=egress,
        profiles_dir=str(profiles_path),
        log_level=log_level.lower(),
        diagnostics_headers=diagnostics,
    )


def _env_overrides(config: Config, env: Mapping[str, str]) -> Config:
    listen = config.listen
    if "RETROBRIDGE_HOST" in env:
        host = env["RETROBRIDGE_HOST"].strip()
        if not host:
            raise ConfigError("RETROBRIDGE_HOST must not be empty")
        listen = replace(listen, host=host)
    if "RETROBRIDGE_PORT" in env:
        raw_port = env["RETROBRIDGE_PORT"].strip()
        if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
            raise ConfigError("RETROBRIDGE_PORT must be an integer 1-65535")
        listen = replace(listen, port=int(raw_port))

    log_level = config.log_level
    if "RETROBRIDGE_LOG_LEVEL" in env:
        level = env["RETROBRIDGE_LOG_LEVEL"].strip().lower()
        if level not in LEVELS:
            raise ConfigError("RETROBRIDGE_LOG_LEVEL must be one of: %s" % ", ".join(sorted(LEVELS)))
        log_level = level

    egress = config.egress
    if "RETROBRIDGE_ALLOWED_SCHEMES" in env:
        raw = [item.strip() for item in env["RETROBRIDGE_ALLOWED_SCHEMES"].split(",") if item.strip()]
        try:
            egress = replace(egress, allowed_schemes=validate_schemes(raw))
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    if "RETROBRIDGE_ALLOWED_HOSTS" in env:
        raw_hosts = [item.strip() for item in env["RETROBRIDGE_ALLOWED_HOSTS"].split(",") if item.strip()]
        egress = replace(egress, allowed_hosts=tuple(raw_hosts))
    if "RETROBRIDGE_DENY_PRIVATE_RANGES" in env:
        raw_flag = env["RETROBRIDGE_DENY_PRIVATE_RANGES"].strip().lower()
        if raw_flag not in ("0", "1", "true", "false", "yes", "no"):
            raise ConfigError("RETROBRIDGE_DENY_PRIVATE_RANGES must be boolean")
        egress = replace(egress, deny_private_ranges=raw_flag in ("1", "true", "yes"))

    profiles_dir = config.profiles_dir
    if "RETROBRIDGE_PROFILES_DIR" in env:
        profiles_dir = env["RETROBRIDGE_PROFILES_DIR"].strip() or profiles_dir

    return replace(config, listen=listen, log_level=log_level, egress=egress,
                   profiles_dir=profiles_dir)


def load_config(
    path: Optional[str] = None, env: Optional[Mapping[str, str]] = None
) -> Config:
    """defaults → JSON file → environment overrides (in that order)."""
    environ = os.environ if env is None else env
    config_path = path or environ.get("RETROBRIDGE_CONFIG")
    config: Config
    if config_path:
        file_path = Path(config_path)
        try:
            document = json.loads(file_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError("cannot read config %s: %s" % (file_path, exc)) from exc
        except ValueError as exc:
            raise ConfigError("config %s is not valid JSON: %s" % (file_path, exc)) from exc
        config = from_dict(document, base_dir=file_path.parent)
    else:
        config = from_dict({})
    return _env_overrides(config, environ)
