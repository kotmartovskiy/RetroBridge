"""Configuration loading and validation (gateway/config.py)."""
from __future__ import annotations

import json

import pytest

from gateway.config import Config, ConfigError, from_dict, load_config
from tests.conftest import EXAMPLES


def test_defaults():
    config = from_dict({})
    assert config.listen.host == "127.0.0.1"
    assert config.listen.port == 8080
    assert config.egress.allowed_schemes == ("https",)
    assert config.egress.allowed_ports == (80, 443)
    assert config.egress.deny_private_ranges is True
    assert config.timeouts.upstream_s == 10.0
    assert config.limits.max_request_body_bytes == 65536
    assert config.limits.max_response_body_bytes == 1_048_576
    assert config.log_level == "info"


def test_example_config_loads():
    from gateway.config import ListenConfig
    from pathlib import Path

    config = load_config(str(EXAMPLES / "config.json"), env={})
    assert config.listen == ListenConfig(host="127.0.0.1", port=8080)
    assert config.egress.allowed_schemes == ("https",)
    assert "example.com" in config.egress.allowed_hosts
    assert config.log_level == "info"
    # profiles_dir resolves relative to the config file location
    assert config.profiles_dir.endswith("profiles")
    assert Path(config.profiles_dir).is_dir()


def test_missing_config_file_rejected():
    with pytest.raises(ConfigError):
        load_config("does/not/exist.json", env={})


def test_invalid_json_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(bad), env={})


def test_wrong_schema_rejected():
    with pytest.raises(ConfigError):
        from_dict({"schema": "retrobridge/gateway-config@9"})


def test_bad_port_rejected():
    with pytest.raises(ConfigError):
        from_dict({"listen": {"port": 70000}})
    with pytest.raises(ConfigError):
        from_dict({"listen": {"port": -1}})
    with pytest.raises(ConfigError):
        from_dict({"listen": {"port": "8080"}})


def test_ephemeral_port_zero_allowed():
    assert from_dict({"listen": {"port": 0}}).listen.port == 0


def test_unsupported_scheme_rejected():
    with pytest.raises(ConfigError):
        from_dict({"egress": {"allowed_schemes": ["ftp"]}})
    with pytest.raises(ConfigError):
        from_dict({"egress": {"allowed_schemes": ["http", "file"]}})


def test_empty_schemes_rejected():
    with pytest.raises(ConfigError):
        from_dict({"egress": {"allowed_schemes": []}})


def test_wildcard_hosts_rejected():
    with pytest.raises(ConfigError):
        from_dict({"egress": {"allowed_hosts": ["*"]}})


def test_bad_log_level_rejected():
    with pytest.raises(ConfigError):
        from_dict({"log_level": "verbose"})


def test_bad_timeouts_rejected():
    with pytest.raises(ConfigError):
        from_dict({"timeouts": {"upstream_s": 0}})
    with pytest.raises(ConfigError):
        from_dict({"timeouts": {"upstream_s": -1.0}})


def test_bad_limits_rejected():
    with pytest.raises(ConfigError):
        from_dict({"limits": {"max_request_body_bytes": 0}})
    with pytest.raises(ConfigError):
        from_dict({"limits": {"max_header_count": "many"}})


def test_bad_deny_private_flag_rejected():
    with pytest.raises(ConfigError):
        from_dict({"egress": {"deny_private_ranges": "yes"}})


def test_listen_properties():
    assert Config().listen.is_loopback
    assert not from_dict({"listen": {"host": "0.0.0.0"}}).listen.is_loopback
    assert from_dict({"listen": {"host": "0.0.0.0"}}).listen.is_wildcard
    assert from_dict({"listen": {"host": "0.0.0.0"}}).self_addresses == (
        "127.0.0.1", "localhost", "::1")


def test_env_overrides(tmp_path):
    env = {
        "RETROBRIDGE_HOST": "0.0.0.0",
        "RETROBRIDGE_PORT": "9090",
        "RETROBRIDGE_LOG_LEVEL": "debug",
        "RETROBRIDGE_ALLOWED_SCHEMES": "https,http",
        "RETROBRIDGE_ALLOWED_HOSTS": "a.test,*.b.test",
        "RETROBRIDGE_DENY_PRIVATE_RANGES": "false",
    }
    config = load_config(None, env=env)
    assert config.listen.host == "0.0.0.0"
    assert config.listen.port == 9090
    assert config.log_level == "debug"
    assert config.egress.allowed_schemes == ("https", "http")
    assert config.egress.allowed_hosts == ("a.test", "*.b.test")
    assert config.egress.deny_private_ranges is False


def test_env_rejects_bad_values():
    with pytest.raises(ConfigError):
        load_config(None, env={"RETROBRIDGE_PORT": "http"})
    with pytest.raises(ConfigError):
        load_config(None, env={"RETROBRIDGE_LOG_LEVEL": "loud"})
    with pytest.raises(ConfigError):
        load_config(None, env={"RETROBRIDGE_ALLOWED_SCHEMES": "gopher"})
    with pytest.raises(ConfigError):
        load_config(None, env={"RETROBRIDGE_DENY_PRIVATE_RANGES": "maybe"})


def test_env_config_path(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"listen": {"port": 1234}}), encoding="utf-8")
    config = load_config(None, env={"RETROBRIDGE_CONFIG": str(path)})
    assert config.listen.port == 1234


def test_file_overridden_by_env(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"listen": {"port": 1234}}), encoding="utf-8")
    config = load_config(str(path), env={"RETROBRIDGE_PORT": "4321"})
    assert config.listen.port == 4321


def test_config_is_immutable():
    config = from_dict({})
    with pytest.raises(Exception):
        config.log_level = "debug"
