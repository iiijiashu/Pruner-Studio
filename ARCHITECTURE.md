# Architecture and API contract

## Runtime layout

- Package: `src/pruner_studio/`
- Static UI: `web/`
- Tests: `tests/`
- Runtime data: platform user-data directory named `Pruner Studio`, never the repository or static directory.

The backend uses a loopback-only local HTTP server. It exposes an explicit API router and an allowlisted static router. API and frontend share one origin; CORS is not enabled.

## Required API

- `GET /api/health`
- `GET /api/status`
- `POST /api/auth/qr`
- `GET /api/auth/qr/{key}`
- `POST /api/auth/logout`
- `GET /api/playlists`
- `POST /api/playlists/{id}/sync`
- `POST /api/catalog/import`
- `GET /api/catalog`
- `GET /api/lyrics?id={song_id}`
- `GET /api/audio?id={song_id}`
- `GET /api/rules`
- `PUT /api/rules`
- `POST /api/operations/backup`
- `POST /api/operations/create-playlist`
- `POST /api/operations/unlike`
- `POST /api/playlists/{id}/remove-tracks`
- `POST /api/operations/reconcile`
- `GET /api/operations/pending`
- `POST /api/operations/{operation_id}/reconcile`
- `POST /api/operations/{operation_id}/ignore`

## Domain objects

- `AccountSummary`: user id, display name, avatar URL. No raw cookie fields.
- `PlaylistSummary`: id, name, track count, kind, privacy.
- `Song`: id, title, artists, album, duration, cover URL, source playlist id, availability, optional chorus range.
- `Rule`: id, name, enabled, match clauses, output playlist name.
- `OperationResult`: operation id, requested IDs, successful IDs, definite failed items, unknown items, pending operation IDs, message.

## Correctness rules

- A successful HTTP transport is not a successful NetEase operation; validate upstream response codes and payloads.
- Destructive requests must include `source_playlist_id`. Unlike is valid only for the authenticated account's real liked playlist; removal is valid only for that exact owned playlist. The special local source `imported` is never writable.
- Never remove failed or unknown IDs from the local catalog. An unknown result is frozen and reconciled through fresh remote reads; it is never blindly retried.
- Every remote mutation is preceded by an atomic, account-scoped write-ahead record. Active records survive restart; explicit ignore leaves a durable semantic tombstone. A confirmed create operation is atomically converted into its add-tracks record before the add request can be sent.
- One process owns a canonical runtime data directory at a time through an OS byte-range lock (`msvcrt` on Windows, `flock` on POSIX) held for the entire server lifecycle. Remote writes are additionally serialized within that process.
- Account session selection, remote prechecks, mutation, verification, reconciliation, and logout share the authentication lock so one operation cannot cross accounts.
- Playlist prechecks, write verification, and reconciliation bypass the short-lived read cache.
- Catalog, rules, and the operation journal use temp-file plus fsync and atomic replace. Malformed catalog/rules/journal data fails closed without overwriting the original.
- Session persistence is outside the repository and web root. Confirmed corrupt or invalid session data is quarantined; unreadable data is preserved and reported. Logout removes only the application's session file.
- Input bodies and batch sizes are bounded; IDs are positive integers and deduplicated.
- State-changing HTTP requests require exact loopback Host/Origin/Fetch-Metadata checks and JSON content type. Frontend API callers reject malformed success envelopes instead of inventing success.
- The frontend renders imported strings with text nodes and restricts external URLs to HTTP(S).

## Release inputs

- `src/pruner_studio/version.py` is the single application version source used by package metadata, health responses, HTTP headers, and Windows version resources.
- `requirements-build.lock` pins the Windows x64 / CPython 3.11 packaging wheels with SHA-256 hashes.
- `Pruner-Studio.spec` declares the one-file bundle and explicit dynamic QR image import; Pillow's maintained PyInstaller hooks collect the required image plugins.
