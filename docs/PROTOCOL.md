# RetroBridge — Protocol

This document defines the gateway's protocol surface: the device-facing wire behaviour,
the internal representation (IR) exchanged between adapters and core, the capability
profile schema, transformation rules, the error model, and header policy.

Everything here is **specified, not yet implemented** (Phase 0).

---

## 1. Scope of the wire

RetroBridge exposes three logical surfaces:

| Surface | Audience | Description |
| --- | --- | --- |
| **Device-facing** | legacy clients | Conservative HTTP/1.0-style request/response, plus optional WSP/WTP gatewaying for WAP 1.x devices. |
| **Internal** | core ↔ adapters | The IR (§3) — in-process, never on a socket. |
| **Upstream** | service adapters → Internet | Modern HTTP/1.1+, modern TLS, per service adapter. |

The gateway **does not** attempt HTTP/2, HTTP/3, WebSockets, or server-sent events on the
device-facing surface.

## 2. Device-facing wire behaviour

### 2.1 Transport

- Default listener: `127.0.0.1`, plain TCP, port configurable (example: `8080`).
- Optional LAN listener: explicit config, logged at startup, warned about at every boot.
- Optional legacy-TLS listener: only if a target device family provably needs it and the
  runtime can offer an acceptable cipher *without weakening the upstream leg*. Enabling it
  is a documented, deliberate config action.

### 2.2 Request handling (HTTP-ish devices)

- Accept `GET`, `HEAD`, `POST`. Other methods → `405` (§7) rendered for the device.
- Request line tolerance: `HTTP/1.0` and `HTTP/1.1` versions; missing/extra spaces per
  adapter notes; absolute-form request targets are normalized (and must pass policy).
- Header limits: max header count and max total header bytes per profile; excess → `431`
  or `400` as the profile allows.
- Body limits: max body bytes per profile and per policy; excess → `413`.
- **Connection semantics:** default to `Connection: close` (safest for old clients);
  honour keep-alive only when the profile declares support.
- **No chunked responses** unless the profile explicitly declares support; otherwise
  buffer the (size-limited) body and send `Content-Length`.
- Percent-encoding: decode per profile charset before routing; reject malformed
  sequences rather than guessing.

### 2.3 WSP/WTP (WAP 1.x)

- A dedicated listener terminates WSP sessions and maps them onto the IR. WML/WBXML
  binary payloads are decoded by the device adapter; the gateway responds in the same
  dialect.
- Session state held by the device (deck/card variables, connectionless transaction ids)
  is tracked per session and never assumed to be visible server-side.
- Detailed WSP opcode/transaction mapping is deferred to Phase 2 design; the contract is
  "one WSP request → one IR request → one WSP response".

### 2.4 Response rules

- Status line: `HTTP/1.0 <code> <reason>` for HTTP/1.0 clients; HTTP/1.1 clients get
  `HTTP/1.1`.
- Always emit `Content-Type` with an explicit `charset` when a body is present.
- Emit `Content-Length` when the body length is known (the normal case).
- Keep the header set small — a fixed, documented allowlist (§6).
- Never emit headers the profile forbids (e.g. `Transfer-Encoding` for
  non-supporting devices).

## 3. Internal representation (IR)

The IR is the *only* structure passed between device adapters, core, and service
adapters. Shapes are language-agnostic here; exact typing follows the Phase 0 language
decision.

### 3.1 `IrRequest`

```
IrRequest
├── method            : "GET" | "POST" | "HEAD" | <normalized>
├── target            : { scheme, host, path, query, fragment? }   # normalized, no userinfo
├── headers           : ordered map (lowercased names), policy-sanitized
├── body              : bytes + declared media type + charset (if any)
├── origin            :                       # what the device *actually* said
│   ├── adapter_id    : e.g. "j2me/1.0"
│   ├── raw_target    : original request target (truncated to a safe length)
│   ├── raw_headers   : device header names + sizes (values redacted where sensitive)
│   ├── transport     : http-plain | http-legacy-tls | wsp-wtp | ...
│   └── profile_id    : capability profile selected for this session
└── trace             : trace id, monotonic sequence, timestamps
```

### 3.2 `IrResponse`

```
IrResponse
├── status            : integer upstream/gateway status
├── headers           : ordered map, upstream-derived, policy-filtered
├── body              : bytes (raw upstream representation)
├── meta              : { adapter_id, fetched_at, cache: hit|miss|bypass,
│                        upstream_host, bytes_in }
└── errors            : zero or more internal error classes (see §7)
```

`IrResponse` carries **raw** upstream content; device shaping happens exclusively in the
transformation pipeline (`ARCHITECTURE.md` §7).

### 3.3 Invariants

1. Adapters and core exchange IR only — no device-specific or service-specific fields
   beyond `origin`/`meta`.
2. IR never contains secret material (tokens, cookies for upstream services, API keys).
3. `origin.raw_headers` is redacted: `Authorization`, `Cookie`, and similar values are
   recorded as `<redacted>`.
4. All IR objects are bounded: raw target and header samples are truncated to fixed
   lengths so a hostile device cannot exhaust memory via IR construction.

## 4. Capability profiles

Profiles are declarative data (JSON with a `schema` field; other serializations
acceptable if schema-conformant).

### 4.1 Schema (illustrative, draft)

```json
{
  "schema": "retrobridge/capability-profile@1",
  "id": "j2me-midp2-generic",
  "extends": null,
  "description": "Generic MIDP 2.0 handset, conservative defaults",
  "transport": {
    "protocols": ["http-plain"],
    "keep_alive": false,
    "max_concurrent_connections": 1,
    "max_request_header_bytes": 2048,
    "max_response_header_bytes": 4096,
    "read_timeout_ms": 15000,
    "write_timeout_ms": 15000
  },
  "tls": { "device_side": "none" },
  "content": {
    "markup_profiles": ["html-3.2", "wml-1.x"],
    "charsets": ["iso-8859-1", "utf-8"],
    "default_charset": "iso-8859-1",
    "media_types": ["text/html", "text/vnd.wap.wml", "image/gif", "image/png", "image/jpeg"],
    "max_body_bytes": 65536,
    "chunked_responses": false,
    "supports_content_length": true
  },
  "session": {
    "cookie_mode": "none | header-only | minimal",
    "redirect_mode": "manual | gateway-follow",
    "max_redirects": 3,
    "scripting": false
  },
  "device": {
    "viewport": { "width": 240, "height": 320 },
    "color_depth": 16,
    "image": { "max_width": 230, "max_height": 300, "max_bytes": 20480,
               "formats": ["gif", "png", "jpeg"] }
  },
  "version": "0.1.0"
}
```

### 4.2 Selection rules

1. Adapter `probe` returns a score and a suggested profile id.
2. Highest score wins; ties resolve to the **weaker/more constrained** profile.
3. No match → the documented `generic-constrained` profile.
4. User config may pin or override a profile per device (by address, UA pattern, or
   adapter id).
5. Profile `version` is recorded in the session trace so fixture failures are diagnosable.

### 4.3 Baseline profiles to author (examples/)

`generic-constrained`, `wap-wml-classic`, `j2me-midp2-generic`, `symbian-s60-browser`,
`winmo-ie-classic`, `psp-browser`, `console-homebrew` — plus a "wide-screen HTML" profile
for capable devices. Authoring these is a Phase 1–2 deliverable; they are data, reviewable
in pull requests.

## 5. Transformation pipeline rules

Stages run in fixed order (see `ARCHITECTURE.md` §7). Normative rules:

1. **Media-type selection** — choose the device representation class from
   `content.markup_profiles` and the upstream content type. Unsupported upstream type →
   best available conversion (e.g. HTML → text), else `406`.
2. **Markup down-conversion** — output must validate against the target profile's
   grammar. Undefined/legacy constructs degrade to plain text rather than invalid markup.
3. **Charset** — decode upstream as UTF-8 unless a valid upstream charset says otherwise;
   encode output as `content.default_charset`; emit matching `Content-Type; charset=`.
   Characters that cannot be represented → documented substitution (never a crash, never
   mojibake).
4. **Media rewriting** — images exceeding `device.image` limits are resized/re-encoded to
   a supported format; beyond that, replaced by a placeholder plus alt text. Non-decodable
   formats → placeholder. **No video/audio** unless a future profile explicitly declares
   decode support.
5. **Script stripping** — `<script>`, event handlers, and modern dynamic markup removed;
   resulting page must remain navigable as links/forms.
6. **Cookie/redirect rewriting** — upstream `Set-Cookie` is handled by the gateway (not
   forwarded to the device unless the profile allows cookie headers); `Location` targets
   are rewritten to gateway-relative form; cross-host redirects are re-checked against
   egress policy.
7. **Size enforcement** — if output exceeds `content.max_body_bytes`, truncate at a
   markup-safe boundary and append a visible truncation marker with the gateway's footer.

Each rule has fixture pairs: `tests/fixtures/<rule>/<case>.in` → `.out`.

## 6. Header policy

**Stripped from device → upstream** (examples): `Host` (recomputed), `Connection`,
`Keep-Alive`, `Proxy-*`, `TE`, `Trailer`, `Transfer-Encoding`, `Upgrade`, `X-Forwarded-*`
(unless explicitly configured), any `Authorization`/`Cookie` supplied by the device
unless an adapter explicitly maps it, and all unrecognized hop-by-hop headers.

**Stripped from upstream → device** (examples): `Set-Cookie*` (gateway-owned),
`Transfer-Encoding`, `Content-Encoding` (after decoding), `Content-Security-Policy`,
`Strict-Transport-Security`, `Alt-Svc`, `Server` details, compression-related headers
(after decoding), and any header the target profile does not declare support for.

**Allowlisted toward the device** (typical): `Content-Type`, `Content-Length`,
`Location`, `Cache-Control`, `Date`, `Last-Modified`, `Retry-After`, plus documented
`X-RetroBridge-*` diagnostics if enabled.

**Gateway diagnostics headers** (opt-in, never on by default for untrusted LANs):

```
X-RetroBridge-Version: <version>
X-RetroBridge-Profile: <profile_id>
X-RetroBridge-Trace: <trace_id>
```

## 7. Error model

Internal error classes map deterministically to device-legible responses:

| Internal class | HTTP-ish status | Body rendered as |
| --- | --- | --- |
| `MalformedRequest` | 400 | short plain-text line |
| `AuthRequiredLocal` | 401 | minimal login prompt / instructions |
| `PolicyDenied` (egress, method, host) | 403 | "destination not allowed by gateway policy" |
| `NotFound` | 404 | short plain-text line |
| `MethodNotAllowed` | 405 | short plain-text line |
| `RequestTooLarge` | 413 | short plain-text line |
| `UnsupportedByProfile` (e.g. media) | 406 | explanation + best-effort text |
| `HeaderTooLarge` | 431 or 400 | short plain-text line |
| `UpstreamTimeout` | 504 | "upstream did not respond" |
| `UpstreamFailure` | 502 | "upstream request failed" |
| `RateLimited` | 429 + `Retry-After` | "gateway rate limit reached" |
| `Internal` | 500 | "internal gateway error" + trace id only |

Rules: error bodies are tiny, plain text (or the profile's minimal markup), contain **no**
stack traces, filesystem paths, hostnames of internal services, or secrets; they always
carry a trace id for local log correlation. WSP-facing devices receive the equivalent
status in their own dialect.

## 8. Versioning & compatibility

- Profile schema: `retrobridge/capability-profile@<major>` — additive changes bump minor;
  breaking changes bump major and require a migration note in `docs/`.
- Protocol document revisions are tracked by git; normative sections state their intent
  ("MUST/SHOULD/MAY") so implementation review can test against them.
- Wire behaviour may only be loosened (more tolerance) or tightened (more limits) with a
  fixture update and a note in `ROADMAP.md`'s change log section.

## 9. Security requirements on the wire

1. Device-facing listeners default to loopback; non-loopback binding is explicit and
   logged (`ARCHITECTURE.md` §6.1).
2. No secret material may appear in any device-facing bytes — enforced by the conformance
   suite's redaction test.
3. All device-supplied inputs are length-capped before parsing into IR (§3.3.4).
4. Redirects and absolute-form targets are re-validated against egress policy after every
   hop (§5.6).
5. Header names/values from devices are rejected if they contain CR/LF or NUL.
6. The gateway never downgrades its upstream TLS to accommodate a device.

## 10. Related documents

- `docs/ARCHITECTURE.md` — components, pipeline overview, trust zones
- `docs/DEVICE_ADAPTERS.md` — who implements the device-facing surface
- `docs/SERVICE_ADAPTERS.md` — who implements the upstream surface
