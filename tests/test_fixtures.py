"""Shared platform/probe fixture schema tests."""
import json
import pytest
from core.fixtures import FIXTURE_SCHEMA, FixtureError, ProbeFixture, load_fixture_documents
from tests.conftest import EXAMPLES

def test_fixture_examples_validate():
    paths = sorted((EXAMPLES / "fixtures").glob("*.json"))
    assert paths
    fixtures = load_fixture_documents([json.loads(p.read_text(encoding="utf-8")) for p in paths])
    assert all(f.schema == FIXTURE_SCHEMA for f in fixtures)
    assert {f.expected_profile for f in fixtures} == {"generic-constrained", "j2me-midp2-generic"}

def test_fixture_requires_platform_identity():
    document = {"schema": FIXTURE_SCHEMA, "id": "broken",
                "platform": {"family": "j2me"}, "observations": {},
                "expected_profile": "j2me-midp2-generic"}
    with pytest.raises(FixtureError, match="platform.generation"):
        ProbeFixture.from_dict(document)

def test_fixture_rejects_unknown_status():
    document = {"schema": FIXTURE_SCHEMA, "id": "broken-status",
                "platform": {"family": "generic", "generation": "unknown", "runtime": "unknown", "stack": "http-plain"},
                "observations": {}, "expected_profile": "generic-constrained",
                "expected_status": "unsupported"}
    with pytest.raises(FixtureError, match="invalid expected_status"):
        ProbeFixture.from_dict(document)

def test_fixture_duplicate_ids_rejected():
    document = {"schema": FIXTURE_SCHEMA, "id": "same",
                "platform": {"family": "generic", "generation": "unknown", "runtime": "unknown", "stack": "http-plain"},
                "observations": {}, "expected_profile": "generic-constrained"}
    with pytest.raises(FixtureError, match="duplicate fixture id"):
        load_fixture_documents([document, dict(document)])
