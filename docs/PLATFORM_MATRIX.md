# RetroBridge — Platform / Generation / Capability Matrix

This matrix is the architectural catalog for legacy client support. A row becomes supported only after its adapter, profile and conformance fixtures exist.

Core rule: platform family -> generation -> runtime/protocol stack -> capabilities -> transformation plan.

## Platform families

| Family | Generations / branches | Stack class | Priority |
|---|---|---|---|
| WAP | 1.x, 2.x | WSP/WTP/WTLS or HTTP | P0 |
| Java ME | CLDC 1.0/1.1; MIDP 1.x/2.x/3.x; JSRs | constrained Java + HTTP/HTTPS | P0 |
| Symbian | S60/S80/UIQ generations | native/mobile web + HTTP | P0 |
| Windows CE / Mobile | CE, Pocket PC, Smartphone, WM 2003/5/6/6.5 | WinInet / Pocket IE / .NET CF | P0 |
| PSP | firmware/browser generations | constrained HTTP/web | P1 |
| Palm OS | Palm OS 3.x–5.x | NetLib/Blazer | P1 |
| BlackBerry | BBOS 4.x–7.x | Java/Browser/BIS-BES | P1 |
| BlackBerry | BlackBerry 10 | QNX/WebWorks/HTML5/native | P2 |
| BREW | BREW generations / BREW MP | Qualcomm feature-phone runtime | P1 |
| Maemo | 770/N7x/N8x/N900 | Linux + web | P2 |
| MeeGo | Harmattan / variants | Linux + web | P2 |
| webOS | webOS generations | Linux + WebKit/web | P2 |
| bada | 1.x/2.x | Samsung feature/smartphone stack | P2 |
| Firefox OS / B2G | Gecko generations | web runtime | P2 |
| KaiOS | 2.5, 2.6, 3.x, 4.x | Gecko/web runtime | P1 |
| Other constrained clients | device-specific | profile-driven | P3 |

## Capability dimensions

### Transport
- protocol family: HTTP, WSP/WTP, other
- HTTP generation and quirks
- persistent connections
- request/response header limits
- connection concurrency
- timeout tolerance
- TLS availability, versions and cipher constraints
- certificate validation behaviour

### Content
- markup: WML, cHTML, XHTML-MP, HTML 3.2/4, HTML5 subset
- scripting and DOM/API level
- character sets
- media types
- image formats, dimensions and byte limits
- maximum document size
- compression and chunked-transfer support

### Session
- cookies
- redirects
- cache semantics
- forms
- authentication mechanisms
- local storage
- service workers
- offline support

### Device
- viewport and colour depth
- input model
- memory constraints
- media/audio/video constraints

### Application/runtime
- native APIs
- Java ME / JSR availability
- Web API generation
- packaging model
- privilege model
- sandbox and permissions

## Mandatory architectural splits

### Java ME
Do not use one generic j2me profile. Distinguish CLDC generation, MIDP generation, optional JSR set, HTTP/HTTPS/TLS capabilities and size/encoding limits.

### Symbian
At minimum distinguish S60, S80 and UIQ plus relevant OS/browser generations.

### Windows
Keep Windows CE, Pocket PC, Windows Mobile and Windows Phone separate.

### Palm
Keep Palm OS and webOS separate.

### BlackBerry
Keep BBOS and BlackBerry 10 separate. BB10 is a QNX-based generation with a substantially different application/web stack.

### KaiOS
Minimum profiles: 2.5, 2.6, 3.x and 4.x. KaiOS belongs to the web-runtime branch, not the J2ME branch.

## Effective profile

An effective profile is the result of:
1. platform-family evidence
2. generation evidence
3. observed transport
4. observed headers
5. explicit deployment override
6. known device quirks

When evidence conflicts, choose the more constrained safe interpretation.

Conceptually: observations -> candidate profiles -> capability intersection -> effective profile.

The effective profile is what the transformation pipeline consumes.

## Capability-first rule

A device name may be used for diagnostics and profile selection, but transformation decisions must use capabilities.

Bad: model-specific branching in the transformation pipeline.

Good: a transformation stage checks the effective capability, and model-specific exceptions live in quirks.

## Phase 2 implementation order

1. Formalize profile identity and capability schema.
2. Add matrix/catalog validation.
3. Add capability intersection/effective-profile logic.
4. Add shared profile/probe fixtures.
5. Implement WAP/WSP.
6. Implement J2ME profiles.
7. Implement Symbian profiles.
8. Implement Windows CE/Mobile profiles.
9. Add Palm OS and BlackBerry BBOS profiles.
10. Add KaiOS 2.5/2.6/3.x/4.x web-runtime profiles.
11. Add PSP.
12. Add BREW, Maemo, MeeGo, webOS, bada and Firefox OS/B2G.
13. Add real-device conformance fixtures as hardware becomes available.

## Status vocabulary

- catalogued — architecture recorded, no implementation
- profiled — capability profile exists
- probed — detection/probe exists
- adapted — device adapter exists
- fixture-tested — conformance fixtures pass
- hardware-verified — tested against real hardware

A platform is not supported merely because it is catalogued.
