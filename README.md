# RetroBridge

**A local compatibility gateway between legacy devices/protocols and modern Internet services.**

RetroBridge runs on a machine you control (a PC, a small server, a Raspberry Pi on your
LAN) and sits between old clients — Windows Mobile/CE handhelds, J2ME and Symbian phones,
WAP feature phones, a PlayStation Portable, old consoles — and today's Internet services.
It speaks the limited, aging dialect those devices actually support, and translates on the
way to services that assume modern TLS, HTTP/1.1+, JSON APIs, and OAuth.

> **Status: architecture / bootstrap.** This repository currently contains the design
> documents and the directory skeleton. There is no functional implementation yet. Nothing
> here is claimed to work end-to-end, and RetroBridge does **not** aim to be a transparent
> proxy that gives a 2006 phone full modern web compatibility.

---

## Why

A surprising amount of perfectly usable hardware is locked out of the modern Internet by
narrow, fixable gaps:

| Device class | Typical limitations |
| --- | --- |
| Windows Mobile / Windows CE (Pocket IE, early Opera) | Old TLS stacks, weak ciphers, HTTP/1.0-style behaviour, strict/odd header handling |
| J2ME (MIDP / CLDC, `HttpConnection`) | TLS versions long deprecated, small buffers, restrictive sockets, no modern auth flows |
| Symbian (S60/S80 browsers, clients) | Aging TLS, quirky cookie and charset handling |
| PlayStation Portable | Built-in browser with old TLS/HTTP assumptions, captive-portal-era networking |
| WAP / WML feature phones | WSP/WTP/WTLS stacks, binary XML, gateway-originated connections |
| Old consoles (PlayStation 2, Nintendo DS/Wii, Xbox, …) | Discontinued first-party services, protocol-level assumptions, homebrew stacks |

These devices often cannot negotiate the TLS version, cipher suites, HTTP semantics,
authentication flows, or content formats that current services require. RetroBridge makes
that gap *explicit and manageable* instead of pretending the device can be upgraded.

## What RetroBridge is

- **A gateway you run locally.** It binds to `localhost` or your LAN by default. Your
  devices talk to *it*; it talks to the outside world on their behalf.
- **Adapter-based.** Device-side quirks live in **device adapters**; service-side
  protocols and authentication live in **service adapters**. Neither side leaks into the
  other.
- **Capability-driven.** Every session is matched to a *capability profile* describing
  what the device can really do (protocols, TLS reach, screen, encodings, media).
- **Transforming.** Content is routed through an explicit transformation pipeline
  (markup profile, charset, images, cookies, redirects) rather than passed through blindly.
- **A security boundary.** The untrusted legacy device never holds modern service
  credentials, and never sees raw upstream trust decisions. Upstream TLS terminates at the
  gateway; secrets stay on the gateway host.

## What RetroBridge is not

- **Not full modern web compatibility.** It will not make an old device render today's
  JavaScript-heavy web. Rich, interactive sites are out of scope.
- **Not an anonymizer or a general VPN.** It is a compatibility layer with a deliberately
  small, auditable surface.
- **Not a device emulator.** It adapts protocols and content; it does not emulate the
  device OS.
- **Not a bypass tool.** Service adapters must respect the target service's terms of
  service and rate limits; policy is part of the design, not an afterthought.

## Repository layout

```
RetroBridge/
├── README.md
├── LICENSE
├── docs/
│   ├── ARCHITECTURE.md      # System shape, components, data flow, trust zones
│   ├── DEVICE_ADAPTERS.md   # Contract + per-family notes for the device side
│   ├── SERVICE_ADAPTERS.md  # Contract + responsibilities for the service side
│   ├── PROTOCOL.md          # Wire behaviour, capability profiles, transformation rules
│   └── ROADMAP.md           # Phased delivery plan
├── core/                    # Gateway core: sessions, routing, profiles, pipeline
├── device-adapters/         # One module per device family/protocol stack
├── service-adapters/        # One module per upstream service/protocol
├── gateway/                 # Runtime entry point: listeners, config, lifecycle
├── tests/                   # Unit, fixture and integration tests
└── examples/                # Sample configs, capability profiles, walkthroughs
```

Directories are intentionally empty (`.gitkeep`) at this stage — the layout is part of the
architecture contract, the code is not written yet.

## Design principles

1. **Local-first.** The gateway works with no Internet connection. It is installed,
   configured, inspected and debugged on your own machine; state and logs stay local.
2. **Adapter architecture.** New device families and new services are added as isolated
   adapters behind stable interfaces — never by forking the core.
3. **Capability profiles.** Devices are described by data (what they support), not by
   hardcoded `if (device == "PSP")` branches scattered through the core.
4. **Explicit content transformation.** Anything rewritten on the way through is a
   declared, testable pipeline stage with a documented rule.
5. **Security boundary by default.** Local bind, least privilege, no device-side secret
   material, auditable egress.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data flow, trust zones, extension points |
| [docs/DEVICE_ADAPTERS.md](docs/DEVICE_ADAPTERS.md) | Device adapter contract and per-family notes |
| [docs/SERVICE_ADAPTERS.md](docs/SERVICE_ADAPTERS.md) | Service adapter contract, auth, egress policy |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | Gateway wire behaviour, profiles, transformation rules |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phases, exit criteria, risks |

## Roadmap (summary)

- **Phase 0 — Bootstrap:** this commit. Architecture docs and directory skeleton.
- **Phase 1 — Core skeleton:** session lifecycle, capability profile engine, a
  conservative plain-HTTP/1.0 device adapter, a minimal origin-fetch service adapter.
- **Phase 2 — Real device coverage:** WAP/WSP handling, J2ME and Symbian families,
  charset and markup transformation pipeline.
- **Phase 3 — Harder legacy stacks:** Windows Mobile/CE, PSP, TLS-cipher research for
  devices that only speak long-deprecated suites.
- **Phase 4 — Service depth:** richer service adapters, caching, config surface,
  observability.
- **Phase 5 — Research/prototype track:** *later*, exploratory — **Telegram** is
  explicitly a **later research/prototype target**, not a current commitment. Console-era
  online-service resurrection studies live here too.

See [docs/ROADMAP.md](docs/ROADMAP.md) for scope, exit criteria and the risk register.

## Contributing

At this stage contributions should be documentation-first: design corrections, protocol
references, device capability reports ("here is what my S60 actually negotiates"), and
test fixtures. Code contributions become meaningful from Phase 1 onward — please open an
issue before large implementations.

## License

[MIT](LICENSE) © 2026 kotmartovskiy.
