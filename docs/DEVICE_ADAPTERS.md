# RetroBridge — Device Adapters

A **device adapter** is the component that understands one legacy device family or
transport stack and speaks to it in its own dialect. Everything device-specific lives
here; nothing device-specific is allowed to leak into `core/` or into service adapters.

Code lives under `device-adapters/`, one directory per family:

```
device-adapters/
├── <family>/            # e.g. wap-wsp/, j2me/, symbian/, winmo-ce/, psp/, console/
│   ├── adapter          # implementation of the device adapter contract
│   ├── probe            # how sessions of this family are recognized
│   └── notes            # quirks, references, observed behaviour
└── ...
```

*(Directories are placeholders at Phase 0; contract below is the target.)*

---

## 1. Responsibilities

A device adapter owns:

1. **Framing & transport** — accepting/parsing the device's connection style: conservative
   HTTP/1.0-ish requests, connection-close semantics, WSP/WTP (WAP 1.x) sessions,
   OBEX-ish transfers where relevant, quirks in request line and header formatting.
2. **Character decoding** — request bodies/URIs in the device's charset (percent-encoding
   semantics, `Content-Type; charset=` handling, header latin1 vs UTF-8 assumptions).
3. **Parsing to IR** — producing the normalized internal representation described in
   `docs/PROTOCOL.md` §3, plus an `origin` block recording exactly what the device sent.
4. **Capability probing** — supplying evidence used to select a capability profile
   (User-Agent patterns, protocol version, TLS/cipher observed, accept headers).
5. **Response serialization** — rendering the pipeline's output back into the device's
   dialect: status line format the device tolerates, minimal/known headers, correct
   charset and markup profile, chunking-or-close decisions.
6. **Error mapping** — converting internal/upstream failures into small, device-legible
   responses (never upstream stack traces, internal paths, or JSON error objects the
   device cannot parse).
7. **Timeouts & limits** — enforcing per-family read/write timeouts, max request sizes,
   and slow-reader protection (some devices crawl).

A device adapter does **not**: fetch remote resources, hold service credentials, decide
egress destinations, or apply business rules. It ends at the IR.

## 2. Contract (target interface)

Conceptual; exact signatures depend on the Phase 0 language decision.

```
DeviceAdapter
├── id()                          -> stable identifier, e.g. "wap-wsp/1.0"
├── transports()                  -> which listeners it can serve
├── probe(session_observations)   -> match score + suggested profile id (or "no match")
├── open(session)                 -> begin handling (allocate per-session state)
├── read_request(session)         -> DeviceRequest | WouldBlock | Fatal(DeviceError)
├── to_ir(device_request)         -> IrRequest        (pure, no I/O)
├── render(ir_response, profile)  -> DeviceResponse   (pure, no I/O)
├── write_response(session, dev)  -> Done | WouldBlock | Fatal(DeviceError)
└── close(session, reason)        -> release resources, emit redacted trace
```

Notes:

- `probe`, `to_ir` and `render` are **pure** — unit-testable without sockets.
- `WouldBlock` is a first-class result: adapters must tolerate slow or half-duplex
  devices rather than blocking a worker thread.
- Adapters must be safe for concurrent sessions with no shared mutable per-session state.

## 3. Contract tests every adapter must pass

`tests/` will hold a shared adapter conformance suite; an adapter is only "supported" once
it passes:

1. **Golden request/response fixtures** — recorded (sanitized) conversations render
   byte-identically.
2. **Round-trip fidelity** — `render(to_ir(x))` preserves method, target, and body bytes
   for device-legible inputs.
3. **Error mapping table** — every internal error class maps to a defined device error.
4. **Limits** — oversized request, slow client, abrupt disconnect, and header-flood cases
   terminate cleanly without leaking session state.
5. **No secret leakage** — assertion that no configured secret material appears in any
   rendered response or log line for the session.
6. **Concurrency** — N simultaneous sessions with no cross-talk.

## 4. Family notes

These are *design inputs*, gathered from public protocol specifications and common
device behaviour. They are hypotheses to verify against real hardware in later phases —
not guarantees.

### 4.1 WAP 1.x / WSP (WML feature phones)

- Device-side leg is often **WSP over WTP over UDP** (or WSP over WSP/WTP with WTLS),
  not TCP HTTP. The gateway terminates this and speaks HTTP-ish upstream on the device's
  behalf — the classic "WAP gateway" role, modernized and combined with transformation.
- Content is binary-encoded WML (WML 1.x / WBXML); pages are small, scripting absent,
  decks/cards model with `<go>`/`<refresh>` navigation and field variables (session state
  the gateway must not assume is server-visible).
- Header vocabulary is minimal; expect gateway-originated connections (the network
  operator historically originated the HTTP side).
- TLS on the device leg may be WTLS with its own trust model; treat it as untrusted
  regardless.

### 4.2 J2ME (MIDP / CLDC)

- Networking via the GCF connection framework: `Connector.open("http://...")`,
  `https://...`, occasionally `socket://`. `HttpConnection` exposes a small header set and
  `setRequestProperty` restrictions.
- TLS reach is firmware-dependent and in practice often limited to old protocol versions
  and cipher suites; many handsets cannot complete a modern handshake at all.
- Buffers and heap are tiny: large headers, large bodies, or long chunked streams cause
  failures. Response bodies should be kept small and connection-close semantics respected.
- Character encodings are often platform-specific (and `Content-Type` may be absent);
  profile declares the charset.
- MIDP 2.0 adds an optional proxy setting — many devices are configured with an explicit
  proxy host/port, which is a natural place to point at RetroBridge.

### 4.3 Symbian (S60/S80 and friends)

- Browsers and clients with early-2000s TLS/HTTP stacks; cookie handling and cache
  behaviour vary by app rather than by OS.
- Some clients use a system-wide access-point model (the OS picks the bearer) — relevant
  only in that a proxy configuration is device-side, not gateway-side.
- Occasional strictness about status lines and header ordering; fixtures needed.

### 4.4 Windows Mobile / Windows CE

- Pocket IE and bundled browsers: old Trident versions, HTTP/1.0 defaults, rigid
  cookie/redirect handling, and TLS stacks frozen at the device's ROM vintage.
- Certificate handling on-device is weak or user-bypassable; **the gateway must not rely
  on device-side certificate validation** for any security property.
- Wide device variance (Pocket PC 2003 → Windows Mobile 6.5): profile-per-subfamily is
  likely, matching by UA string + observed negotiation.

### 4.5 PlayStation Portable

- Built-in browser with period-appropriate TLS/HTTP assumptions; networking configured
  per-connection (SSID/DNS), proxy configuration possible in some contexts.
- Reasonable screen (480×272) but limited decoders and small buffers; expect PNG/JPEG
  support but constrained sizes, and no useful scripting for gateway purposes.
- Homebrew clients are a more tractable target than the retail browser and are a good
  Phase 3 test vehicle.

### 4.6 Old consoles (PlayStation 2, Nintendo DS/Wii, Xbox, …)

- First-party online services are discontinued; what remains is either homebrew clients
  or community-rebuilt services.
- Very limited TLS/HTTP in stock firmware; some titles used proprietary binary protocols
  over UDP.
- Scope for RetroBridge is **protocol adaptation for homebrew/community clients**, not
  reviving publisher servers. Any restoration effort must respect the IP and ToS of the
  service involved.

## 5. What "supported" means

Per family, the ladder is:

1. **Documented** — notes + capability profile exist (this phase contributes notes only).
2. **Probed** — adapter can identify sessions and select a profile.
3. **Functional** — GET/POST of small documents works end-to-end against a test service.
4. **Hardened** — passes the conformance suite and the limits tests.
5. **Verified on hardware** — exercised against real devices, with fixture updates.

No family is advertised above level 1 at Phase 0.

## 6. Related documents

- `docs/ARCHITECTURE.md` §3.3, §4 — where adapters sit in the data flow
- `docs/PROTOCOL.md` §3 — the IR adapters produce and consume
- `docs/PROTOCOL.md` §4 — capability profiles they select
- `docs/ROADMAP.md` — which family lands in which phase
