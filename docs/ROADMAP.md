# RetroBridge — Roadmap

Phased plan from architecture bootstrap to a working local gateway. Phases are ordered by
dependency, not by calendar; no dates are promised. Each phase has **exit criteria** — a
phase is done when the criteria are met, not when time passes.

Current status: **Phase 2B — legacy markup output and shared platform fixtures implemented; WAP/WSP adapter work in progress**.

Phase 0 and Phase 1 implementation commits are complete. Phase 2A capability-model consolidation and the initial Phase 2B markup/fixture work are complete. The next vertical slice is WAP 1.x/WSP, followed by the remaining content and device adapters.

---

## Phase 0 — Bootstrap ✅ (this commit)

Establish the design contract and repository skeleton. No functional code.

- [x] `README.md` — problem framing, principles, explicit non-goals
- [x] `LICENSE` — MIT, 2026 kotmartovskiy
- [x] `docs/ARCHITECTURE.md` — components, data flow, trust zones, extension points
- [x] `docs/DEVICE_ADAPTERS.md` — device adapter contract + family notes
- [x] `docs/SERVICE_ADAPTERS.md` — service adapter contract + egress/auth policy
- [x] `docs/PROTOCOL.md` — wire behaviour, IR, profile schema, transformation rules
- [x] `docs/ROADMAP.md` — this document
- [x] Empty-but-trackable `core/`, `device-adapters/`, `service-adapters/`, `gateway/`,
      `tests/`, `examples/` (`.gitkeep`)

**Exit criteria:** docs are internally consistent; repository checks pass; commit pushed
to `origin/main`.

**Explicitly out of scope for Phase 0:** any implementation, any claim of device
compatibility, any wire-level behavior.

---

## Phase 1 — Core skeleton + first vertical slice

Goal: prove the shape end-to-end on loopback with a single, deliberately simple path.

- [ ] Language/runtime decision recorded in `docs/ARCHITECTURE.md` §10 (candidates:
      legacy-TLS feasibility, low memory, cross-compile, test tooling)
- [ ] `gateway/` — config loading, loopback listener, graceful shutdown, structured
      redacted logging
- [ ] `core/` — session manager, router (pure), policy checks, error model (PROTOCOL §7)
- [ ] Capability profile engine + `generic-constrained` and one real profile authored in
      `examples/`
- [ ] First device adapter: conservative plain HTTP/1.0-ish (GET/POST, connection-close)
- [ ] First service adapter: `origin-http` with host allowlist + private-range blocking
- [ ] Transformation pipeline with stages 1, 3, 5, 7 (media-type, charset, script strip,
      size enforcement) wired; fixture tests for each
- [ ] Conformance suite skeleton in `tests/` (round-trip, error table, redaction)
- [ ] One `examples/` walkthrough: "point a browser at the gateway, fetch an allowlisted
      page"

**Exit criteria:** a device-legible GET works locally end-to-end against a fake origin;
all fixtures green in CI; no secret appears in any response or log in the redaction test.

---

## Phase 2 — Capability matrix + real device coverage

### Phase 2A — Capability model consolidation

- [x] Platform/generation/runtime/stack identity added to capability profiles
- [x] Platform capability catalog added in `examples/platform-matrix.json`
- [x] Platform/generation matrix documented in `docs/PLATFORM_MATRIX.md`
- [x] Java ME profile explicitly identifies CLDC/MIDP generation and capability data
- [x] Capability intersection / effective-profile engine
- [x] Conservative intersection tests for transport, limits, charset, session and declarative capabilities
- [x] Shared platform/probe fixture schema
- [x] Profile authoring guide updated around capability-first decisions

### Phase 2B — Protocol and device coverage

- [x] WML/cHTML/XHTML MP output profiles in the pipeline (stage 2)
- [ ] Charset normalization across legacy single-byte charsets (stage 3 completed)
- [ ] Media rewriting: image resize/re-encode + placeholders (stage 4)
- [ ] Cookie/redirect rewriting (stage 6)
- [ ] Device adapter: **WAP 1.x / WSP** gatewaying (PROTOCOL §2.3)
- [ ] Device adapter: **J2ME** (MIDP/CLDC) with profile `j2me-midp2-generic`
- [ ] Device adapter: **Symbian** browser-era HTTP quirks, profile `symbian-s60-browser`
- [ ] Adapter conformance suite enforced for all of the above
- [ ] Capability profile authoring guide in `examples/`

**Exit criteria:** each family reaches "Functional" (see `DEVICE_ADAPTERS.md` §5) against
local fakes; at least one family verified against real hardware or a faithful emulator,
with fixtures updated from the observation.

---

## Phase 3 — Harder legacy stacks + service depth

- [ ] Device adapter: **Windows Mobile / Windows CE** (Pocket IE-era), profiles per
      subfamily
- [ ] Device adapter: **PlayStation Portable** (retail browser best-effort + homebrew
      client as primary test vehicle)
- [ ] Legacy-TLS device listener research: which suites devices actually offer; whether a
      dedicated listener is warranted — record findings, do not weaken upstream TLS
- [ ] Service adapter: `archives` (read-only), `feeds` (RSS/Atom → device text)
- [ ] Service adapter: `search` **only if** terms permit programmatic access; otherwise
      user-supplied API key model
- [ ] Redirect-follow policy hardened: per-hop egress revalidation tests, SSRF corpus

**Exit criteria:** Windows Mobile and PSP families at "Functional"; two non-trivial
service adapters passing the shared contract suite; legacy-TLS findings documented as a
research note (pass/fail acceptable — a negative result is a valid outcome).

---

## Phase 4 — Depth, operations, offline-first

- [ ] Local cache layer with explicit TTL/invalidation (offline-capable browsing of
      previously fetched content)
- [ ] Config surface stabilized (file format documented, validated, examples maintained)
- [ ] Local admin/status page (loopback-only): sessions, profiles in use, counters
- [ ] Rate limiting and per-service budgets enforced centrally
- [ ] Performance sanity: bounded memory under many slow clients, soak test
- [ ] Old-console homebrew/community client profile work (`console-homebrew`)

**Exit criteria:** a non-developer can install, configure, and diagnose the gateway from
docs alone; soak test passes with documented limits.

---

## Phase 5 — Research / prototype track (explicitly later)

Nothing in this phase is a commitment; items are exploratory and labeled experimental
until reviewed.

- [ ] **Telegram: research/prototype — a later target.** Investigate feasibility of a
      messaging service adapter using official APIs only: auth flow on the gateway,
      credential storage, rate limits, ToS compliance, and what a device-legible client
      would even look like. Deliverable is a research note first; a prototype only if the
      note says it is reasonable. **Not started; not in any earlier phase.**
- [ ] Console-era online-service resurrection studies (only where the operator has the
      right to access the service)
- [ ] Additional protocol experiments (IR compression for very slow links, offline sync)
- [ ] Any other speculative service adapter follows the same research-first rule

**Exit criteria per item:** written research note with a go/no-go recommendation.

---

## Risk register

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Device TLS reach is too limited for any direct secure listener | Device leg stays cleartext | Already assumed: boundary treats the device leg as untrusted (ARCHITECTURE §6); document clearly |
| Legacy runtime requirements (old ciphers) conflict with modern TLS on the upstream leg | Misconfiguration could weaken upstream security | Two independent TLS configs; upstream config not derivable from device config; conformance test asserts this |
| Capability profiles fragment into unbounded per-device special cases | Core complexity creep | Profiles are data; `extends` inheritance; generic-constrained default; review against "adding a family must not touch core" |
| Service ToS/rate limits block intended adapters | Features unavailable | Research-first (Phase 5), egress policy, user-owned credentials, read-only defaults |
| Scope creep toward "full modern web" | Project fails its purpose | Non-goals in README + ARCHITECTURE §8; PRs claiming JS/SPA support are out of scope |
| No real hardware available for verification | Untested assumptions | Emulators/recorded fixtures as interim evidence; mark verification level per family honestly |
| Single-maintainer bandwidth | Slow phases | Phases ordered by value; Phase 1 slice is intentionally minimal |

## Honesty policy

- A family/adaptor's support level (`DEVICE_ADAPTERS.md` §5) is stated wherever it is
  mentioned — never implied higher than measured.
- Experimental work (Phase 5, including any future Telegram prototype) is labeled in
  config, docs, and generated content.
- RetroBridge does **not** claim modern web compatibility for any device, now or later.

## Change log (protocol/docs)

| Date | Change |
| --- | --- |
| 2026-10-08 | Initial bootstrap: README, LICENSE, five docs, directory skeleton |
| 2026-10-08 | Phase 2B: legacy markup profiles and WSP adapter contract clarified |
