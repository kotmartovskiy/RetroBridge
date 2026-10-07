# RetroBridge — Architecture

This document describes the intended shape of RetroBridge: components, data flow, trust
boundaries, and extension points. It is a design contract, not a description of code that
exists — as of Phase 0 only the skeleton directories are present.

---

## 1. Problem framing

Legacy clients (Windows Mobile/CE, J2ME, Symbian, PSP, WAP/WML phones, old consoles) speak
a small, frozen subset of networking: old TLS versions and cipher suites, HTTP/1.0-ish
request patterns, restrictive header and cookie handling, fixed charsets, and markup
dialects (WML 1.x, cHTML, XHTML MP, HTML 3.2/4.0) that modern services no longer emit.

Modern services, conversely, assume TLS 1.2+, HTTP/1.1 with chunked encoding and large
headers, JSON bodies, token-based auth (OAuth 2.0 / device flows), and browsers capable of
executing JavaScript.

RetroBridge resolves the mismatch **at a single, inspectable, local hop** rather than by
patching either end.

## 2. High-level view

```
        UNTRUSTED LEGACY ZONE                 |      GATEWAY HOST (local-first)      |      INTERNET
                                              |
  +---------------------+                    |  +----------------------------------+ |
  | WAP phone (WSP/WTP) |                    |  |          gateway/               | |
  +----------+----------+                    |  |  listeners · config · lifecycle | |
             |                               |  +---------------+------------------+ |
  +----------+----------+   device-facing    |                  |                    |
  | J2ME MIDP client    |   protocol         |  +---------------v------------------+ |
  +----------+----------+ ----------------> |  |            core/                 | |
             |        (conservative HTTP /  |  |  session mgr · routing · policy  | |
  +----------+----------+   WSP gatewaying) |  |  capability profile engine       | |
  | Symbian · WinMo/CE  |                    |  |  transformation pipeline         | |
  +----------+----------+                    |  |  secret store · observability    | |
             |                               |  +----+-----------------------+-----+ |
  +----------+----------+                    |       |                       |       |
  | PSP · old consoles  |                    |  +----v-------------+  +------v------+|
  +---------------------+                    |  | device-adapters/ |  | service-    ||
                                             |  | (per family)     |  | adapters/   ||
                                             |  +------------------+  +------+------+-|
                                             |                            |  egress    |
                                             +----------------------------+------------|
                                                                          |
                                                                   +------v------+
                                                                   | modern svc  |
                                                                   | (TLS 1.2+)  |
                                                                   +-------------+
```

The gateway terminates the device-facing connection, resolves a **capability profile**,
parses the request into an **internal representation (IR)**, runs the **transformation
pipeline**, dispatches to a **service adapter**, and reverses the trip for the response.

## 3. Components

### 3.1 `gateway/` — runtime shell

- **Listeners**: bind addresses/ports for each device-facing transport (plain HTTP,
  legacy-TLS HTTP, WAP/WSP gateway endpoint). Default bind is loopback; LAN binding is an
  explicit, logged opt-in.
- **Configuration**: declarative config (file + environment) describing listeners,
  profiles, adapter wiring, egress policy.
- **Lifecycle**: startup order, graceful shutdown, hot-reload of config where safe.
- **Health/diagnostics**: local-only status endpoint, structured logs, per-session traces.

### 3.2 `core/` — the middle

- **Session manager.** Owns a session from accept to close: identity (best-effort), chosen
  transport, negotiated profile, timeouts, byte/queue limits.
- **Router.** Maps `(device capability, requested resource, adapter rules)` to a target
  service adapter and a transformation plan. Pure decision logic, unit-testable with no
  sockets.
- **Capability profile engine.** Loads declarative profiles, matches them to incoming
  sessions (probe result → profile, with a conservative default), exposes them to
  adapters as read-only data.
- **Transformation pipeline.** Ordered, named stages (see `docs/PROTOCOL.md` §5). Each
  stage declares its inputs/outputs and is independently testable against fixtures.
- **Policy / security boundary.** Enforces the rules in §6 before anything leaves the host
  or reaches a device: bind scope, egress allowlist, header stripping, body size caps,
  rate limits, secret access control.
- **Secret store.** Holds service credentials. Readable by service adapters only through a
  narrow interface; never serialized into device-facing responses or logs.
- **Observability.** Structured logging with redaction, counters, per-request trace IDs.

### 3.3 `device-adapters/` — the device side

One module per device family or transport stack. Translates a device-specific
conversation into and out of the IR. Owns: framing, encodings, quirks, timeouts, and
device-side error mapping. See `docs/DEVICE_ADAPTERS.md`.

### 3.4 `service-adapters/` — the service side

One module per upstream service or protocol family. Owns: upstream TLS/auth, request
shaping, response acquisition, upstream error mapping, caching hooks, and egress policy
compliance. See `docs/SERVICE_ADAPTERS.md`.

### 3.5 `tests/` and `examples/`

- `tests/` — unit tests for `core`, contract tests every adapter must pass, recorded
  wire fixtures (sanitized), and a small set of end-to-end tests against local fake
  services.
- `examples/` — sample configs, sample capability profiles, a walkthrough for bringing up
  a first device.

## 4. Data flow (one request)

1. Device connects to a listener; transport-level characteristics are observed
   (protocol, TLS/cipher if any, request syntax, header shape, declared charsets).
2. Session opens; the profile engine selects a capability profile. Selection is
   *conservative*: when unsure, choose the weaker profile.
3. Device adapter parses the request into the **IR** (normalized method/URI/headers/body
   plus an `origin` block describing what the device actually said).
4. Policy checks run: destination allowed? body within limits? method permitted?
5. Router picks a service adapter and a transformation plan.
6. Service adapter acquires the resource, using stored secrets for auth, applying egress
   and rate policy. Upstream TLS terminates here.
7. Response flows back through the transformation pipeline (markup profile → charset →
   media → cookie/header rewriting), producing a device-appropriate representation.
8. Device adapter serializes the result in the device's dialect and writes it back with
   framing the device tolerates.
9. Session closes (or is reused, if the device's dialect allows); metrics and a redacted
   trace line are emitted.

Failure at any step maps to a device-legible error, never to an upstream stack trace or
internal path.

## 5. Capability profiles (summary)

A capability profile is **data, not code**: declarative description of what a device (or
device class) can do — see `docs/PROTOCOL.md` §4 for the schema. Roughly:

- **Transport**: supported protocols (plain HTTP/1.0-style, HTTP/1.1 subset, WSP/WTP),
  TLS reach and cipher constraints, connection reuse, max concurrent connections.
- **Content**: accepted markup profiles (WML 1.x, cHTML, XHTML MP, HTML 3.2/4.0),
  charsets, media types, max body size, image decode limits.
- **Session**: cookie semantics, redirect handling, timeout/buffer tolerances.
- **Device**: display hints (viewport width, colors, depth) used for media decisions.

Profiles are shared, versioned, and overridable per deployment. Devices map to profiles
via adapter probe rules; unmatched devices get a documented "generic constrained" profile.

## 6. Security boundary

Threat model: the legacy device is **untrusted and possibly compromised or misbehaving**;
the gateway host is trusted; the Internet is untrusted-but-legitimate.

Rules (design requirements, not suggestions):

1. **Local-first / default loopback.** Listeners bind `127.0.0.1` by default. Binding to
   a LAN interface is explicit config and is logged at startup.
2. **No device-held secrets.** Modern credentials (tokens, API keys, cookies for upstream
   services) live only in the gateway's secret store. A device authenticates to the
   gateway with at most a local, revocable, low-privilege credential.
3. **TLS terminates at the gateway.** The device-side leg may be cleartext (that is often
   unavoidable — a 2006 phone may not negotiate anything better) — so the boundary assumes
   nothing about confidentiality on that leg, and secrets are never placed on it. Upstream,
   the gateway negotiates modern TLS on the device's behalf.
4. **Egress allowlist.** Service adapters reach only configured hosts; the gateway is not
   an open proxy for arbitrary destinations.
5. **Sanitization both ways.** Upstream content is reduced to a safe, transformed
   representation; device-supplied input (headers, bodies, URIs) is validated and capped
   before it influences routing.
6. **Header stripping.** hop-by-hop and identity-revealing headers are stripped in each
   direction according to `docs/PROTOCOL.md` §6.
7. **Least privilege and auditability.** Small surface, no dynamic code loading from
   device input, structured redacted logs, everything configurable in a text file you can
   read.

## 7. Content transformation

Transformation is a **declared pipeline**, not ad-hoc string rewriting. Stages (order
fixed, stages selectable per plan):

1. **Media-type selection** — decide target representation class from the profile.
2. **Markup down-conversion** — WML/cHTML/XHTML MP/HTML 3.2–4.0 target, per profile.
3. **Charset normalization** — decode upstream (UTF-8 default) → encode device charset
   (often a legacy single-byte or UTF-16 variant), with correct content-type headers.
4. **Media rewriting** — resize/re-encode images to device-decodable formats and sizes;
   drop formats the profile cannot decode; never promise video the device cannot play.
5. **Script stripping** — client-side scripting and modern dynamic behaviour is removed,
   not emulated.
6. **Cookie / redirect rewriting** — collapse modern cookie flows and absolute redirect
   targets into gateway-relative ones the device can follow.
7. **Size enforcement** — truncate or paginate oversized bodies per profile, with an
   explicit marker rather than a silent cut.

Each stage is fixture-tested: input document → expected output document.

## 8. Non-goals (explicit)

- **No full modern web compatibility.** JavaScript applications, SPA frameworks, HTTP/2,
  HTTP/3, WebSockets-heavy sites, and modern certificate expectations on-device are out of
  scope. Where a device cannot accept a modern site, RetroBridge produces the *best honest
  degradation its profile allows* — or a clear failure — not a fake modern page.
- **No device OS emulation.**
- **No open-proxy behaviour** and no attempt to circumvent a service's access controls.
- **No silent protocol downgrades of upstream trust** — the gateway never weakens its own
  upstream TLS configuration to match a device.

## 9. Extension points

| To add… | You write… | Touch core? |
| --- | --- | --- |
| A device family | one `device-adapters/<family>/` module + its probe rules + a profile | No |
| A service | one `service-adapters/<name>/` module + egress + secret refs | No |
| A transformation | one pipeline stage implementing the stage contract + fixtures | Pipeline registration only |
| A capability profile | declarative data in `examples/` or user config | No |

If an addition requires modifying `core/`, that is a design smell worth an issue first.

## 10. Implementation language

Open decision, to be settled in Phase 0/1 (tracked in `docs/ROADMAP.md`). Candidates are
judged on: ability to serve legacy TLS/cipher suites, deterministic low-memory operation,
cross-compilation to a NAS/Pi, and test tooling. No language-specific API shape is
committed to in these documents yet.

## 11. Related documents

- `docs/DEVICE_ADAPTERS.md` — device-side contract and per-family notes
- `docs/SERVICE_ADAPTERS.md` — service-side contract
- `docs/PROTOCOL.md` — wire behaviour, profile schema, transformation rules
- `docs/ROADMAP.md` — phasing, exit criteria, risk register
