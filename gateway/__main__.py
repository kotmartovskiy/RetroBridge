"""``python -m gateway`` — start the RetroBridge gateway.

Usage::

    python -m gateway --config examples/config.json
    python -m gateway --host 127.0.0.1 --port 8080 --log-level debug

Configuration precedence: defaults → JSON file → environment → CLI flags.
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading

from core import logging as rb_logging
from gateway.config import ConfigError, load_config
from gateway.server import Gateway


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gateway",
        description="RetroBridge: local compatibility gateway for legacy devices.",
    )
    parser.add_argument("--config", help="path to a JSON config file")
    parser.add_argument("--host", help="override listen host")
    parser.add_argument("--port", type=int, help="override listen port")
    parser.add_argument(
        "--log-level",
        choices=("debug", "info", "warning", "error"),
        help="override log level",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("retrobridge: config error: %s" % exc, file=sys.stderr)
        return 2

    overrides = {}
    if args.host:
        overrides["host"] = args.host
    if args.port is not None:
        if not 1 <= args.port <= 65535:
            print("retrobridge: --port must be 1-65535", file=sys.stderr)
            return 2
        overrides["port"] = args.port
    if overrides:
        from dataclasses import replace

        config = replace(config, listen=replace(config.listen, **overrides))
    if args.log_level:
        config = replace(config, log_level=args.log_level)

    rb_logging.setup(config.log_level)
    logger = rb_logging.get_logger("main")

    try:
        gateway = Gateway(config)
    except (ConfigError, ValueError, OSError) as exc:
        print("retrobridge: startup error: %s" % exc, file=sys.stderr)
        return 2

    stop = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("signal", extra={"fields": {"signal": int(signum)}})
        stop.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            try:
                getattr(signal, signal_name)(_handle_signal)
            except (ValueError, OSError):  # pragma: no cover - platform specific
                pass

    try:
        gateway.start_background()
    except OSError as exc:
        logger.error(
            "bind_failed",
            extra={"fields": {"bind": gateway.config.gateway_authority, "error": str(exc)}},
        )
        return 1

    try:
        while not stop.wait(0.5):
            pass
    finally:
        gateway.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
