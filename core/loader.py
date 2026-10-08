"""Loader for adapter modules.

Adapters live under ``device-adapters/`` and ``service-adapters/`` whose
directory names are not importable Python package names (hyphens), matching
the repository layout contract in the README. This loader imports individual
adapter files by path and caches the result so every consumer shares one
module object.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType

_LOCK = threading.Lock()
_CACHE: dict = {}

#: Repository root (this file lives in <root>/core/).
REPO_ROOT = Path(__file__).resolve().parent.parent


def load_module(path: Path, module_name: str) -> ModuleType:
    """Import ``path`` as ``module_name``; cached per resolved path."""
    resolved = Path(path).resolve()
    key = str(resolved)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        if not resolved.is_file():
            raise FileNotFoundError("adapter module not found: %s" % resolved)
        spec = importlib.util.spec_from_file_location(module_name, resolved)
        if spec is None or spec.loader is None:
            raise ImportError("cannot load adapter module: %s" % resolved)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        _CACHE[key] = module
        return module


def load_device_adapter(name: str) -> ModuleType:
    """Load ``device-adapters/<name>/adapter.py``."""
    path = REPO_ROOT / "device-adapters" / name / "adapter.py"
    return load_module(path, "retrobridge_device_%s" % name.replace("-", "_"))


def load_service_adapter(name: str) -> ModuleType:
    """Load ``service-adapters/<name>/adapter.py``."""
    path = REPO_ROOT / "service-adapters" / name / "adapter.py"
    return load_module(path, "retrobridge_service_%s" % name.replace("-", "_"))
