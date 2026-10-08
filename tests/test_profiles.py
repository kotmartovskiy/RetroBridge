"""Capability profile loading, validation and selection (PROTOCOL §4)."""
from __future__ import annotations

import json

import pytest

from core.profiles import (
    DEFAULT_PROFILE_ID,
    PROFILE_SCHEMA,
    CapabilityProfile,
    ProfileError,
    ProfileRegistry,
    ProbeResult,
    intersect_profiles,
    select_profile,
)
from tests.conftest import EXAMPLES


def minimal(**overrides):
    document = {
        "schema": PROFILE_SCHEMA,
        "id": "unit-profile",
        "description": "unit",
        "content": {
            "charsets": ["iso-8859-1"],
            "default_charset": "iso-8859-1",
            "media_types": ["text/plain"],
            "max_body_bytes": 1024,
        },
        "version": "0.1.0",
    }
    document.update(overrides)
    return document


# -- loading ----------------------------------------------------------------- #


def test_registry_loads_example_profiles(registry):
    assert registry.ids() == ("generic-constrained", "j2me-midp2-generic")
    assert registry.default.id == DEFAULT_PROFILE_ID
    assert len(registry) == 2
    assert "j2me-midp2-generic" in registry


def test_generic_profile_is_conservative(registry):
    profile = registry.get("generic-constrained")
    assert profile.transport.keep_alive is False          # HTTP/1.0 behaviour
    assert profile.session.scripting is False             # no-JS assumption
    assert profile.content.chunked_responses is False
    assert profile.content.max_body_bytes <= 32768        # small response limit
    assert profile.transport.max_request_header_bytes <= 2048
    assert profile.content.default_charset == "iso-8859-1"  # simple charset
    assert "utf-8" not in profile.content.charsets
    assert profile.transport.protocols == ("http-plain",)
    assert profile.session.cookie_mode == "none"


def test_j2me_profile_roundtrips(registry):
    profile = registry.get("j2me-midp2-generic")
    assert profile.platform.family == "j2me"
    assert profile.platform.generation == "cldc1.1-midp2.x"
    assert profile.platform.runtime == "java-me"
    assert profile.capabilities["java_me"]["midp"] == "2.x"
    assert profile.transport.keep_alive is False
    assert profile.session.scripting is False
    assert "wml-1.x" in profile.content.markup_profiles
    reloaded = CapabilityProfile.from_dict(profile.raw)
    assert reloaded == profile


def test_unknown_id_falls_back_to_default(registry):
    assert registry.get("does-not-exist").id == DEFAULT_PROFILE_ID
    assert registry.get(None).id == DEFAULT_PROFILE_ID


def test_missing_directory_rejected(tmp_path):
    with pytest.raises(ProfileError):
        ProfileRegistry.load_dir(tmp_path / "nope")


def test_empty_directory_rejected(tmp_path):
    with pytest.raises(ProfileError):
        ProfileRegistry.load_dir(tmp_path)


def test_invalid_json_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProfileError):
        ProfileRegistry.load_dir(tmp_path)


def test_wrong_schema_rejected():
    document = minimal()
    document["schema"] = "retrobridge/capability-profile@99"
    with pytest.raises(ProfileError):
        CapabilityProfile.from_dict(document)


def test_missing_id_rejected():
    document = minimal()
    del document["id"]
    with pytest.raises(ProfileError):
        CapabilityProfile.from_dict(document)


def test_unknown_charset_rejected():
    document = minimal()
    document["content"] = {
        "charsets": ["iso-8859-1", "not-a-real-charset"],
        "default_charset": "iso-8859-1",
    }
    with pytest.raises(ProfileError):
        CapabilityProfile.from_dict(document)


def test_default_charset_must_be_listed():
    document = minimal()
    document["content"] = {"charsets": ["utf-8"], "default_charset": "iso-8859-1"}
    with pytest.raises(ProfileError):
        CapabilityProfile.from_dict(document)


def test_bad_cookie_mode_rejected():
    document = minimal()
    document["session"] = {"cookie_mode": "whatever"}
    with pytest.raises(ProfileError):
        CapabilityProfile.from_dict(document)


def test_negative_body_limit_rejected():
    document = minimal()
    document["content"] = {"charsets": ["utf-8"], "default_charset": "utf-8",
                           "max_body_bytes": -1}
    with pytest.raises(ProfileError):
        CapabilityProfile.from_dict(document)


def test_examples_are_valid_json_files():
    for path in (EXAMPLES / "profiles").glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["schema"] == PROFILE_SCHEMA


# -- selection --------------------------------------------------------------- #


def test_selection_highest_score_wins(registry):
    probes = [
        ProbeResult(score=10, profile_id="generic-constrained"),
        ProbeResult(score=50, profile_id="j2me-midp2-generic"),
    ]
    assert select_profile(registry, probes).id == "j2me-midp2-generic"


def test_selection_tie_resolves_to_weaker(registry):
    probes = [
        ProbeResult(score=20, profile_id="j2me-midp2-generic"),
        ProbeResult(score=20, profile_id="generic-constrained"),
    ]
    chosen = select_profile(registry, probes)
    assert chosen.id == "generic-constrained"
    weaker = registry.get("generic-constrained")
    stronger = registry.get("j2me-midp2-generic")
    assert weaker.capability_score < stronger.capability_score


def test_selection_no_match_uses_default(registry):
    assert select_profile(registry, []).id == DEFAULT_PROFILE_ID
    assert select_profile(registry, [ProbeResult.no_match()]).id == DEFAULT_PROFILE_ID


def test_selection_unknown_profile_id_ignored(registry):
    probes = [ProbeResult(score=99, profile_id="unregistered-profile")]
    assert select_profile(registry, probes).id == DEFAULT_PROFILE_ID


def test_selection_zero_score_ignored(registry):
    probes = [ProbeResult(score=0, profile_id="j2me-midp2-generic")]
    assert select_profile(registry, probes).id == DEFAULT_PROFILE_ID


def test_intersection_is_conservative(registry):
    generic = registry.get("generic-constrained")
    j2me = registry.get("j2me-midp2-generic")
    effective = intersect_profiles(generic, j2me)

    assert effective.id == "effective-generic-constrained-j2me-midp2-generic"
    assert effective.platform.family == "generic"
    assert effective.transport.keep_alive is False
    assert effective.transport.max_request_header_bytes == 2048
    assert effective.content.max_body_bytes == 32768
    assert effective.content.default_charset == "iso-8859-1"
    assert effective.session.cookie_mode == "none"
    assert effective.session.scripting is False
    assert effective.capabilities["transport"]["https"] is False


def test_intersection_rejects_incompatible_transport(registry):
    generic = registry.get("generic-constrained")
    incompatible = CapabilityProfile.from_dict({
        **generic.raw,
        "id": "incompatible",
        "transport": {**generic.raw["transport"], "protocols": ["wsp"]},
        "content": {**generic.raw["content"], "charsets": ["utf-8"], "default_charset": "utf-8"},
    })
    with pytest.raises(ProfileError, match="common transport protocol"):
        intersect_profiles(generic, incompatible)


def test_adapter_probe_selects_by_user_agent(device, registry):
    j2me = device.Observations(version="HTTP/1.0", user_agent="Nokia/3.12 MIDP/2.0")
    plain = device.Observations(version="HTTP/1.0", user_agent="Lynx/2.8")
    assert device.probe(j2me).profile_id == "j2me-midp2-generic"
    assert device.probe(plain).profile_id == "generic-constrained"
    assert select_profile(registry, [device.probe(j2me)]).id == "j2me-midp2-generic"
    assert select_profile(registry, [device.probe(plain)]).id == "generic-constrained"
