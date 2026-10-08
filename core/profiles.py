"""Capability profiles: model, loading and selection (docs/PROTOCOL.md §4).

Profiles are declarative data (JSON, schema ``retrobridge/capability-profile@1``).
Selection is conservative: the highest probe score wins, ties resolve to the
*weaker* (more constrained) profile, and nothing matches the documented
``generic-constrained`` default. Profiles are read-only once loaded.
"""
from __future__ import annotations

import codecs
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

PROFILE_SCHEMA = "retrobridge/capability-profile@1"
DEFAULT_PROFILE_ID = "generic-constrained"
VERSION = "0.1.0"

SUPPORTED_CHARSETS_FALLBACK = "iso-8859-1"


class ProfileError(ValueError):
    """A profile document is missing, malformed or schema-incompatible."""


def _require(mapping: Dict[str, Any], key: str, kind: type, where: str) -> Any:
    if key not in mapping:
        raise ProfileError("%s: missing required field %r" % (where, key))
    value = mapping[key]
    if not isinstance(value, kind):
        raise ProfileError("%s: field %r has wrong type" % (where, key))
    return value


def _str_list(mapping: Dict[str, Any], key: str, where: str, default: Sequence[str] = ()) -> Tuple[str, ...]:
    value = mapping.get(key)
    if value is None:
        return tuple(default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProfileError("%s: field %r must be a list of strings" % (where, key))
    return tuple(value)


def _int_field(mapping: Dict[str, Any], key: str, where: str, default: int) -> int:
    value = mapping.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProfileError("%s: field %r must be a non-negative integer" % (where, key))
    return value


def _bool_field(mapping: Dict[str, Any], key: str, where: str, default: bool) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise ProfileError("%s: field %r must be a boolean" % (where, key))
    return value


@dataclass(frozen=True)
class TransportProfile:
    protocols: Tuple[str, ...] = ("http-plain",)
    keep_alive: bool = False
    max_concurrent_connections: int = 1
    max_request_header_bytes: int = 2048
    max_response_header_bytes: int = 4096
    read_timeout_ms: int = 15000
    write_timeout_ms: int = 15000


@dataclass(frozen=True)
class ContentProfile:
    markup_profiles: Tuple[str, ...] = ("html-3.2",)
    charsets: Tuple[str, ...] = ("iso-8859-1",)
    default_charset: str = SUPPORTED_CHARSETS_FALLBACK
    media_types: Tuple[str, ...] = ("text/html", "text/plain")
    max_body_bytes: int = 65536
    chunked_responses: bool = False
    supports_content_length: bool = True


@dataclass(frozen=True)
class SessionProfile:
    cookie_mode: str = "none"
    redirect_mode: str = "manual"
    max_redirects: int = 3
    scripting: bool = False


@dataclass(frozen=True)
class PlatformIdentity:
    family: str = "generic"
    generation: str = "unknown"
    runtime: str = "unknown"
    stack: str = "unknown"


@dataclass(frozen=True)
class DeviceProfile:
    viewport_width: int = 240
    viewport_height: int = 320
    color_depth: int = 8
    image_formats: Tuple[str, ...] = ()
    image_max_bytes: int = 20480


@dataclass(frozen=True)
class CapabilityProfile:
    id: str
    description: str
    transport: TransportProfile
    content: ContentProfile
    session: SessionProfile
    device: DeviceProfile
    platform: PlatformIdentity = field(default_factory=PlatformIdentity)
    capabilities: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    quirks: Tuple[str, ...] = ()
    schema: str = PROFILE_SCHEMA
    version: str = VERSION
    extends: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def scripting(self) -> bool:
        return self.session.scripting

    @property
    def default_charset(self) -> str:
        return self.content.default_charset

    @property
    def capability_score(self) -> int:
        """Higher means more capable; ties in probe scores resolve to the lower score."""
        score = int(self.content.max_body_bytes)
        if self.transport.keep_alive:
            score += 1_000_000
        if self.session.scripting:
            score += 1_000_000
        score += 1000 * len(self.content.charsets)
        score += 100 * len(self.content.media_types)
        score += 10 * len(self.content.markup_profiles)
        return score

    @classmethod
    def from_dict(cls, document: Any) -> "CapabilityProfile":
        where = "capability profile"
        if not isinstance(document, dict):
            raise ProfileError("%s: document must be a JSON object" % where)
        schema = document.get("schema")
        if schema != PROFILE_SCHEMA:
            raise ProfileError(
                "%s: unsupported schema %r (expected %r)" % (where, schema, PROFILE_SCHEMA)
            )
        profile_id = _require(document, "id", str, where)
        where = "profile %r" % profile_id
        if not profile_id:
            raise ProfileError("%s: id must be non-empty" % where)

        transport_raw = document.get("transport") or {}
        content_raw = document.get("content") or {}
        session_raw = document.get("session") or {}
        device_raw = document.get("device") or {}
        for name, raw in (
            ("transport", transport_raw),
            ("content", content_raw),
            ("session", session_raw),
            ("device", device_raw),
        ):
            if not isinstance(raw, dict):
                raise ProfileError("%s: section %r must be an object" % (where, name))

        transport = TransportProfile(
            protocols=_str_list(transport_raw, "protocols", where, ("http-plain",)),
            keep_alive=_bool_field(transport_raw, "keep_alive", where, False),
            max_concurrent_connections=_int_field(
                transport_raw, "max_concurrent_connections", where, 1
            ),
            max_request_header_bytes=_int_field(
                transport_raw, "max_request_header_bytes", where, 2048
            ),
            max_response_header_bytes=_int_field(
                transport_raw, "max_response_header_bytes", where, 4096
            ),
            read_timeout_ms=_int_field(transport_raw, "read_timeout_ms", where, 15000),
            write_timeout_ms=_int_field(transport_raw, "write_timeout_ms", where, 15000),
        )
        if transport.max_request_header_bytes <= 0:
            raise ProfileError("%s: transport.max_request_header_bytes must be > 0" % where)

        default_charset = content_raw.get("default_charset", SUPPORTED_CHARSETS_FALLBACK)
        if not isinstance(default_charset, str) or not default_charset:
            raise ProfileError("%s: content.default_charset must be a non-empty string" % where)
        content = ContentProfile(
            markup_profiles=_str_list(content_raw, "markup_profiles", where, ("html-3.2",)),
            charsets=_str_list(content_raw, "charsets", where, ("iso-8859-1",)),
            default_charset=default_charset,
            media_types=_str_list(
                content_raw, "media_types", where, ("text/html", "text/plain")
            ),
            max_body_bytes=_int_field(content_raw, "max_body_bytes", where, 65536),
            chunked_responses=_bool_field(content_raw, "chunked_responses", where, False),
            supports_content_length=_bool_field(
                content_raw, "supports_content_length", where, True
            ),
        )
        if content.max_body_bytes <= 0:
            raise ProfileError("%s: content.max_body_bytes must be > 0" % where)
        for charset in set(content.charsets) | {content.default_charset}:
            try:
                codecs.lookup(charset)
            except (LookupError, ValueError) as exc:
                raise ProfileError(
                    "%s: unknown charset %r in content.charsets" % (where, charset)
                ) from exc
        if content.default_charset.lower().replace("_", "-") not in tuple(
            charset.lower().replace("_", "-") for charset in content.charsets
        ):
            raise ProfileError(
                "%s: content.default_charset %r is not listed in content.charsets"
                % (where, content.default_charset)
            )

        cookie_mode = session_raw.get("cookie_mode", "none")
        redirect_mode = session_raw.get("redirect_mode", "manual")
        if cookie_mode not in ("none", "header-only", "minimal"):
            raise ProfileError("%s: session.cookie_mode is invalid" % where)
        if redirect_mode not in ("manual", "gateway-follow"):
            raise ProfileError("%s: session.redirect_mode is invalid" % where)
        session = SessionProfile(
            cookie_mode=cookie_mode,
            redirect_mode=redirect_mode,
            max_redirects=_int_field(session_raw, "max_redirects", where, 3),
            scripting=_bool_field(session_raw, "scripting", where, False),
        )

        viewport = device_raw.get("viewport") or {}
        if not isinstance(viewport, dict):
            raise ProfileError("%s: device.viewport must be an object" % where)
        image = device_raw.get("image") or {}
        if not isinstance(image, dict):
            raise ProfileError("%s: device.image must be an object" % where)
        platform_raw = document.get("platform") or {}
        if not isinstance(platform_raw, dict):
            raise ProfileError("%s: section 'platform' must be an object" % where)
        platform = PlatformIdentity(
            family=str(platform_raw.get("family", "generic")),
            generation=str(platform_raw.get("generation", "unknown")),
            runtime=str(platform_raw.get("runtime", "unknown")),
            stack=str(platform_raw.get("stack", "unknown")),
        )
        for field_name, value in (
            ("family", platform.family),
            ("generation", platform.generation),
            ("runtime", platform.runtime),
            ("stack", platform.stack),
        ):
            if not value:
                raise ProfileError("%s: platform.%s must be non-empty" % (where, field_name))

        capabilities = document.get("capabilities") or {}
        if not isinstance(capabilities, dict):
            raise ProfileError("%s: field 'capabilities' must be an object" % where)
        quirks = _str_list(document, "quirks", where, ())

        device = DeviceProfile(
            viewport_width=_int_field(viewport, "width", where, 240),
            viewport_height=_int_field(viewport, "height", where, 320),
            color_depth=_int_field(device_raw, "color_depth", where, 8),
            image_formats=_str_list(image, "formats", where, ()),
            image_max_bytes=_int_field(image, "max_bytes", where, 20480),
        )

        extends = document.get("extends")
        if extends is not None and not isinstance(extends, str):
            raise ProfileError("%s: extends must be a string or null" % where)

        return cls(
            id=profile_id,
            description=str(document.get("description", "")),
            transport=transport,
            content=content,
            session=session,
            device=device,
            platform=platform,
            capabilities=dict(capabilities),
            quirks=quirks,
            schema=str(schema),
            version=str(document.get("version", VERSION)),
            extends=extends,
            raw=document,
        )

    @classmethod
    def from_file(cls, path: Path) -> "CapabilityProfile":
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ProfileError("cannot read profile %s: %s" % (path, exc)) from exc
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise ProfileError("profile %s is not valid JSON: %s" % (path, exc)) from exc
        return cls.from_dict(document)


@dataclass(frozen=True)
class ProbeResult:
    """What a device adapter's probe reported for one session (PROTOCOL §4.2)."""

    score: int
    profile_id: str = ""

    @classmethod
    def no_match(cls) -> "ProbeResult":
        return cls(score=0, profile_id="")


class ProfileRegistry:
    """Immutable set of loaded profiles with the documented default fallback."""

    def __init__(self, profiles: Iterable[CapabilityProfile], default_id: str = DEFAULT_PROFILE_ID):
        self._profiles: Dict[str, CapabilityProfile] = {}
        for profile in profiles:
            if profile.id in self._profiles:
                raise ProfileError("duplicate profile id %r" % profile.id)
            self._profiles[profile.id] = profile
        if default_id not in self._profiles:
            raise ProfileError("default profile %r is not loaded" % default_id)
        self._default_id = default_id

    @classmethod
    def load_dir(cls, path: Path, default_id: str = DEFAULT_PROFILE_ID) -> "ProfileRegistry":
        directory = Path(path)
        if not directory.is_dir():
            raise ProfileError("profile directory not found: %s" % directory)
        profiles: List[CapabilityProfile] = []
        for file_path in sorted(directory.glob("*.json")):
            profiles.append(CapabilityProfile.from_file(file_path))
        if not profiles:
            raise ProfileError("no *.json profiles found in %s" % directory)
        return cls(profiles, default_id=default_id)

    @property
    def default_id(self) -> str:
        return self._default_id

    @property
    def default(self) -> CapabilityProfile:
        return self._profiles[self._default_id]

    def get(self, profile_id: Optional[str]) -> CapabilityProfile:
        """Return ``profile_id`` or the default profile if missing/unknown."""
        if profile_id:
            found = self._profiles.get(profile_id)
            if found is not None:
                return found
        return self.default

    def ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def __len__(self) -> int:
        return len(self._profiles)

    def __contains__(self, profile_id: object) -> bool:
        return profile_id in self._profiles


def select_profile(
    registry: ProfileRegistry, probes: Sequence[ProbeResult]
) -> CapabilityProfile:
    """PROTOCOL §4.2 selection: highest score, ties → weaker profile, else default.

    ``probes`` holds one result per device adapter that matched the session.
    Unknown or empty suggestions fall through to the default profile.
    """
    best_probe: Optional[ProbeResult] = None
    best_profile: Optional[CapabilityProfile] = None
    for probe in probes:
        if probe.score <= 0 or not probe.profile_id:
            continue
        profile = registry.get(probe.profile_id)
        if profile.id != probe.profile_id:
            continue  # suggested id is unknown: ignore, never trust blindly
        if best_probe is None or probe.score > best_probe.score:
            best_probe, best_profile = probe, profile
            continue
        if probe.score == best_probe.score and profile.capability_score < (
            best_profile.capability_score if best_profile else 0
        ):
            best_profile = profile
    if best_profile is None:
        return registry.default
    return best_profile
