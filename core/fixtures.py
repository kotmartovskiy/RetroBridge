"""Validation for shared platform/probe conformance fixtures."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence, Tuple

FIXTURE_SCHEMA = "retrobridge/platform-probe-fixture@1"
ALLOWED_STATUS = {"catalogued", "profiled", "probed", "adapted", "fixture-tested", "hardware-verified"}

class FixtureError(ValueError):
    """A platform/probe fixture is malformed or schema-incompatible."""

@dataclass(frozen=True)
class ProbeFixture:
    id: str
    platform: Dict[str, str]
    observations: Dict[str, Any]
    expected_profile: str
    expected_status: str = "probed"
    notes: Tuple[str, ...] = ()
    schema: str = FIXTURE_SCHEMA

    @classmethod
    def from_dict(cls, document: Any) -> "ProbeFixture":
        if not isinstance(document, dict):
            raise FixtureError("fixture: document must be a JSON object")
        if document.get("schema") != FIXTURE_SCHEMA:
            raise FixtureError("fixture: unsupported schema %r" % document.get("schema"))
        fixture_id = document.get("id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise FixtureError("fixture: id must be a non-empty string")
        platform = document.get("platform")
        if not isinstance(platform, dict):
            raise FixtureError("fixture %r: platform must be an object" % fixture_id)
        required_platform = ("family", "generation", "runtime", "stack")
        for key in required_platform:
            value = platform.get(key)
            if not isinstance(value, str) or not value:
                raise FixtureError("fixture %r: platform.%s must be a non-empty string" % (fixture_id, key))
        observations = document.get("observations")
        if not isinstance(observations, dict):
            raise FixtureError("fixture %r: observations must be an object" % fixture_id)
        expected_profile = document.get("expected_profile")
        if not isinstance(expected_profile, str) or not expected_profile:
            raise FixtureError("fixture %r: expected_profile must be a non-empty string" % fixture_id)
        status = document.get("expected_status", "probed")
        if status not in ALLOWED_STATUS:
            raise FixtureError("fixture %r: invalid expected_status %r" % (fixture_id, status))
        notes = document.get("notes", [])
        if not isinstance(notes, list) or not all(isinstance(item, str) for item in notes):
            raise FixtureError("fixture %r: notes must be a list of strings" % fixture_id)
        return cls(id=fixture_id, platform={key: platform[key] for key in required_platform},
                   observations=dict(observations), expected_profile=expected_profile,
                   expected_status=status, notes=tuple(notes))

def load_fixture_documents(documents: Sequence[Mapping[str, Any]]) -> Tuple[ProbeFixture, ...]:
    fixtures = tuple(ProbeFixture.from_dict(document) for document in documents)
    ids = [fixture.id for fixture in fixtures]
    if len(ids) != len(set(ids)):
        raise FixtureError("duplicate fixture id")
    return fixtures
