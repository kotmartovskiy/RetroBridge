"""RetroBridge gateway core.

Pure, socket-free building blocks shared by the device adapters, the service
adapters and the gateway runtime:

- ``errors``       internal error model with deterministic device mapping (PROTOCOL §7)
- ``ir``           the internal representation exchanged between adapters (PROTOCOL §3)
- ``logging``      structured, redacted JSON-line logging
- ``profiles``     capability profile model + selection engine (PROTOCOL §4)
- ``policy``       egress/method/header policy checks (ARCHITECTURE §6)
- ``pipeline``     ordered transformation stages (PROTOCOL §5)
- ``router``       pure routing decision (ARCHITECTURE §3.2)
- ``session``      per-connection session records (ARCHITECTURE §3.2)
- ``loader``       filesystem loader for adapter modules outside importable packages
"""

__all__ = [
    "errors",
    "ir",
    "logging",
    "profiles",
    "policy",
    "pipeline",
    "router",
    "session",
    "loader",
]
