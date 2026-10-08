# RetroBridge capability profile authoring

Profiles describe capabilities, not product names. Device identity is evidence used for selection; the transformation pipeline must consume capability fields.

## Required workflow

1. Identify family, generation, runtime and stack.
2. Record only capabilities supported by evidence.
3. Prefer the weaker value when evidence is uncertain.
4. Keep model-specific exceptions in quirks, not in transformation code.
5. Add or update a platform/probe fixture under examples/fixtures/.
6. Run the profile and fixture tests before claiming the profile is usable.

## Platform identity

Every profile should provide:
- family: stable family identifier such as j2me, symbian, kaios;
- generation: meaningful OS/runtime generation;
- runtime: execution model such as java-me or gecko;
- stack: relevant transport/application stack.

These fields describe identity. They do not themselves grant capabilities.

## Capability data

Use explicit capability values for decisions that affect rendering or transport. Boolean capabilities are intersected with logical AND, numeric limits with MIN, lists by ordered intersection, and nested objects recursively.

Do not encode "probably supported" as true. Unknown capabilities should remain absent or be represented by a deliberately conservative profile.

## Probe fixtures

The shared fixture schema is retrobridge/platform-probe-fixture@1. Fixtures must contain:
- normalized platform identity;
- sanitized observations;
- expected profile;
- evidence/support status;
- optional notes.

Fixtures are evidence contracts, not compatibility claims. Synthetic fixtures must say so. Hardware captures must be sanitized and must not contain credentials or personal identifiers.

## Profile inheritance

The extends field is reserved for profile-family reuse. Do not use inheritance to hide incompatible transport or charset assumptions. The effective profile engine must remain conservative.

## Review checklist

- Does the profile avoid capabilities not supported by evidence?
- Are transport, charset, body-size and session limits explicit?
- Is the default charset listed in content.charsets?
- Are optional JSRs/runtime APIs represented as capabilities?
- Does a fixture reproduce profile selection from its stated observations?
- Is the support level stated honestly: catalogued, profiled, probed, adapted, fixture-tested, or hardware-verified?
