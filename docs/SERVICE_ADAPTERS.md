# RetroBridge — Service Adapters

A **service adapter** connects RetroBridge to one upstream service or protocol family on
the modern Internet. It owns everything that happens *beyond* the gateway: upstream TLS,
authentication, request shaping, response acquisition, caching hooks, and compliance with
the service's limits and terms.

Code lives under `service-adapters/`, one directory per service:

```
service-adapters/
├── <name>/             # e.g. origin-http/, archives/, search/, messaging/
│   ├── adapter         # implementation of the service adapter contract
│   ├── policy          # egress hosts, methods, limits for this service
│   └── notes           # API observations, rate limits, ToS notes
└── ...
```

*(Directories are placeholders at Phase 0; contract below is the target.)*

---

## 1. Responsibilities

1. **Upstream transport** — modern TLS negotiation on behalf of the device; connection
   pooling/reuse where sensible; sensible timeouts and retry policy (idempotent-only).
2. **Authentication** — obtaining and refreshing credentials from the gateway **secret
   store**. OAuth-style flows (authorization-code, device-code, refresh tokens) are
   performed *by the gateway*, with any user interaction happening on a modern browser the
   user already owns — never on the legacy device.
3. **Request shaping** — converting an `IrRequest` (from `core`) into the service's real
   request: URL building, headers, body, pagination, content negotiation.
4. **Response acquisition** — fetching and normalizing the upstream response into an
   `IrResponse` for the transformation pipeline: status, headers, raw body, plus metadata
   (fetch time, cache status, upstream request id if any).
5. **Egress policy** — connecting **only** to the hosts declared in `policy`. No
   host/header-driven arbitrary outbound requests; no open proxy.
6. **Rate limiting & backoff** — per-service budgets, honored before the request leaves,
   with clear device-visible errors when exhausted.
7. **Caching** (Phase 4+) — an optional local cache layer keyed by canonical request,
   with explicit TTL/invalidation rules. Local-first means cached content remains
   available offline.
8. **Upstream error mapping** — mapping HTTP/API failures to internal error classes that
   `core` converts into device-legible responses.

A service adapter does **not**: know device quirks, render markup, choose charsets, or
read device-supplied headers directly (device input reaches it only as validated `IrRequest`
fields).

## 2. Contract (target interface)

```
ServiceAdapter
├── id()                        -> stable identifier, e.g. "origin-http/1.0"
├── matches(ir_request, rules)  -> whether this adapter handles the request
├── prepare(ir_request, ctx)    -> OutboundRequest   (pure: URL, headers, body)
├── execute(outbound, ctx)      -> IrResponse | Retry | Fatal(ServiceError)
├── authenticate(ctx)           -> Ready | NeedsUserAction | Fatal   (lazy, cached)
└── close()                     -> release pools/timers
```

Notes:

- `prepare` is pure and unit-testable; `execute` is the only I/O point.
- `ctx` carries: selected capability profile (read-only), secret-store handle scoped to
  this adapter, rate budget, cancellation, and trace id.
- **Secrets never leave `ctx`** — an adapter cannot dump them, and `core` redacts them
  from logs.

## 3. Policy file (per adapter)

Each adapter ships a declarative policy:

```yaml
# service-adapters/origin-http/policy (illustrative)
id: origin-http
egress:
  hosts: ["example.test", "*.example.test"]   # allowlist, no wildcards to arbitrary TLDs
  ports: [80, 443]
  deny_private_ranges: true                   # no SSRF into the gateway's own network
methods: [GET, HEAD, POST]
limits:
  max_body_bytes: 1048576
  max_concurrent: 4
  rate: "10 req/min"
auth: none            # | bearer | oauth-device | basic | custom:<name>
secrets: []           # names resolved from the gateway secret store
compliance:
  respect_robots: true
  tos_reference: null  # link recorded when a service has a published ToS
```

`deny_private_ranges: true` by default prevents a device-supplied URL from turning the
gateway into an SSRF trampoline into `127.0.0.1`, RFC1918 space, or cloud metadata
endpoints.

## 4. Candidate adapters and phasing

| Adapter | Phase | Notes |
| --- | --- | --- |
| `origin-http` — generic allowlisted HTTP(S) origin fetch | 1 | The baseline: proves device → core → service → pipeline end to end. GET/POST only, no JS. |
| `archives` — read-only access to public web archives | 3 | Attractive for retro content; read-only, cacheable, generous limits. |
| `search` — results-to-text/plain or results-to-WML | 3 | Depends on a service whose terms permit programmatic access; otherwise requires the user's own API key. |
| `feeds` — RSS/Atom → paginated device text | 3 | Simple, high value, easy capability profiles. |
| `mail`/`notes`-style personal services | later (not scheduled) | Requires careful secret handling and user-owned credentials; no phase assigned yet. |
| **`messaging` research (Telegram)** | **5 (research/prototype)** | **Later research/prototype target only.** Not a current commitment. Any prototype must use official APIs, honor rate limits and ToS, keep credentials on the gateway, and be labelled experimental until reviewed. |
| Console/community service bridging | 5 (research) | Case-by-case; only for services the operator has the right to access. |

Phase numbering follows `docs/ROADMAP.md`.

## 5. Security requirements (service side)

1. **Credential isolation.** Tokens/API keys live in the gateway secret store; adapters
   get scoped access. Secrets are never written to responses, cache entries, or logs.
2. **Modern upstream trust.** The gateway validates upstream certificates with current
   trust stores and current TLS policy; a device's weak crypto never weakens the
   upstream leg (see `docs/ARCHITECTURE.md` §6, rule 3).
3. **Egress allowlist + private-range blocking**, enforced centrally in `core` and
   declared per adapter (policy above). Declared hosts only.
4. **Input validation.** Device-supplied URLs/fields are normalized before `prepare`;
   no device-controlled header injection into upstream requests.
5. **Scoped auth.** Prefer read-only or least-privilege scopes for user credentials.
6. **Rate and cost limits.** Protect the upstream service and the user's quota.
7. **Honest labeling.** Anything experimental (including a future messaging prototype) is
   labeled as such in config and in generated content.

## 6. Testing

- **`prepare` unit tests** — URL/header construction from IR inputs, including hostile
  inputs (CRLF in headers, absolute URLs, credentials-in-URL, redirects to private
  ranges).
- **Contract tests** — every adapter passes the shared suite: allowlist enforcement,
  rate-limit behavior, secret redaction, error mapping table, cancellation.
- **Recorded fixtures** — sanitized upstream request/response pairs replayed by a local
  fake service; no live network in CI.
- **Fakes** — `tests/` provides a tiny local origin and a fake OAuth-ish service so auth
  and refresh paths are exercised deterministically.

## 7. Related documents

- `docs/ARCHITECTURE.md` §3.4, §6 — placement and security boundary
- `docs/PROTOCOL.md` §3 — `IrRequest`/`IrResponse` shapes adapters consume
- `docs/DEVICE_ADAPTERS.md` — the other side of the boundary
- `docs/ROADMAP.md` — phasing
