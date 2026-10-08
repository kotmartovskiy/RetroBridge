# Platform/probe fixtures

Fixtures use retrobridge/platform-probe-fixture@1.

They capture observed evidence, not marketing claims. A fixture contains:
- platform: normalized family/generation/runtime/stack identity;
- observations: transport, HTTP version, headers, UA, or other adapter evidence;
- expected_profile: the profile that evidence should select;
- expected_status: evidence/support level;
- notes: provenance or limitations.

Keep observations sanitized: never store cookies, authorization values, API keys, personal identifiers, or complete network captures.

A fixture is valid only when its profile selection is reproducible from the stated evidence. Real-device captures should be added only after sanitization and should record firmware/browser generation in the platform identity where known.
