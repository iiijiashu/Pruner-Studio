# Security Policy

Pruner Studio is a local-first single-user music hub and playlist organizer designed to run on a personal desktop and optionally pair with a personal mobile companion over a trusted local area network (LAN). It is **not** designed to be exposed to the public internet or operated as a multi-tenant hosted service.

## Supported Version

Only the latest release is supported.

## Reporting a Vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting feature when enabled for the repository. Do **not** include live session cookies, channel credentials, pairing tokens, QR codes, account identifiers, personal catalogs, or operation logs in public issues.

## Threat Model & Security Boundaries

### 1. Local Loopback Server (`127.0.0.1`)
- Binds exclusively to `127.0.0.1` under an OS data-directory lock; never enables CORS.
- Enforces exact loopback `Host`, `Origin`, and `Sec-Fetch-Site` checks plus `application/json` content-type validation on mutations.
- Protects live `/api/*` endpoints with a per-process ephemeral 256-bit local API token (`X-Pruner-Token` header injected into served HTML pages and `pruner_local_token` `HttpOnly; SameSite=Strict` cookie for SSE/audio/cover subresources).
- `.instance.json` is written inside the user's private application-data directory with owner-only file ACLs on Windows (`icacls`) and `0600` permissions on POSIX.

### 2. Optional LAN Companion Listener (`0.0.0.0` TLS 1.2+ & UDP Discovery)
- **Disabled by default**; activated only when explicitly enabled by the user.
- Serves strictly over **TLS 1.2+** using a locally generated persistent ECDSA P-256 identity (`lan_tls_key.pem` with owner-only permissions).
- Uses **SPKI Public-Key SHA-256 pinning** (`spki_fingerprint` 64-char hex and RFC 7469 `spki_pin` `sha256/<base64>`) alongside backward-compatible certificate fingerprint metadata so paired devices remain authenticated even when the desktop's LAN IP/Wi-Fi/VPN changes and certificate SANs rotate.
- Pairing requires a 6-digit single-use code valid for 5 minutes with brute-force lockout (5 failed attempts -> 180s cooldown).
- Paired device bearer tokens are stored only as salted SHA-256 hashes; revoking a device immediately invalidates future requests and aborts any in-flight audio streams.
- Rejects all requests carrying a browser `Origin` header (`403`) and all `OPTIONS` preflight requests (`405`).
- LAN APIs never expose desktop filesystem paths, NetEase cookies, or third-party channel credentials.

### 3. Credential Isolation & At-Rest Protection
- On Windows, NetEase session cookies (`SessionStore`) and per-channel / protocol credentials (`ChannelCredentialVault`) are encrypted at rest with **Windows DPAPI** (`CryptProtectData` with `CRYPTPROTECT_UI_FORBIDDEN`).
- On macOS/Linux, Pruner Studio uses the OS keyring (`keyring`) when available; in headless environments without a system keyring, credential files are written with strict POSIX `0600` permissions and surfaced explicitly as `plaintext_file_0600` protection status.
- Each external channel, OpenSubsonic server, WebDAV server, and HTTP Resolver v2 endpoint receives **only** its own scoped credentials, sent exclusively to its validated origin, and stripped on cross-origin redirects. Shared HTTP client cookie jars are cleared between requests to prevent cross-account or cross-provider leakage.

### 4. SSRF, DNS Pinning, HTTP Resolver v2 & Optional Sidecar Boundary
- Public remote URLs (external channels, Resolver v2, cover proxies, audio streams) are validated against SSRF and resolved via DNS-pinned sockets rejecting private, loopback (unless explicitly configured), link-local, multicast, and reserved IP addresses.
- Self-hosted OpenSubsonic and WebDAV servers explicitly permit user-configured private LAN/loopback URLs while blocking cloud metadata/link-local ranges (`169.254.0.0/16`, `fe80::/10`) and embedded URL credentials.
- **Unified HTTP Resolver v2** endpoints operate over HTTP/JSON with zero NetEase credentials, zero cookies, strict response bounds, and per-channel circuit breakers.
- **Optional Sidecar Bridge**: runs out-of-process on `127.0.0.1` with a stripped minimal environment (zero host credentials/tokens) and per-session `X-Resolver-Token` authentication. Users should only load community sidecar adapters from sources they explicitly trust.

### 5. WebDAV Decentralized Encrypted Sync
- Client-side sync envelopes are encrypted before upload using **AES-256-GCM** with keys derived from the user's passphrase via **PBKDF2-HMAC-SHA256** (200,000 iterations, random 128-bit salt, authenticated AAD). WebDAV storage providers never receive plaintext history, playlists, feedback, or rules.

## Local Data Boundary

Authentication state, SQLite databases, audio/cover caches, rules, and operation journals reside in the current OS user's application-data directory. Never commit them to version control or place them in `web/`. If a session credential is accidentally exposed, revoke/log out immediately and scrub it from Git history.
