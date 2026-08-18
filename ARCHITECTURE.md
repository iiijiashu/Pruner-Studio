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

## Domain objects

- `AccountSummary`: user id, display name, avatar URL. No raw cookie fields.
- `PlaylistSummary`: id, name, track count, kind, privacy.
- `Song`: id, title, artists, album, duration, cover URL, source playlist id, availability, optional chorus range.
- `Rule`: id, name, enabled, match clauses, output playlist name.
- `OperationResult`: operation id, requested IDs, successful IDs, definite failed items, unknown items, message.

## Correctness rules

- A successful HTTP transport is not a successful NetEase operation; validate upstream response codes and payloads.
- Destructive requests must include `source_playlist_id`. Unlike is valid only for the authenticated account's real liked playlist; removal is valid only for that exact owned playlist. The special local source `imported` is never writable.
- Never remove failed or unknown IDs from the local catalog. An unknown result is frozen and reconciled through fresh remote reads; it is never blindly retried.
- Writes are serialized within the single-user process.
- Playlist prechecks, write verification, and reconciliation bypass the short-lived read cache.
- Catalog and rules use temp-file plus atomic replace.
- Session persistence is outside the repository and web root. Logout removes only the application's session file.
- Input bodies and batch sizes are bounded; IDs are positive integers and deduplicated.
- The frontend renders imported strings with text nodes and restricts external URLs to HTTP(S).
