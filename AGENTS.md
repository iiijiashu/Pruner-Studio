# Pruner Studio implementation contract

This repository is the clean open-source rebuild. Never copy credentials, account identifiers, personal catalog exports, QR images, operation logs, or song lists from the parent directory.

## Product decisions

- Local single-user tool with QR login and remembered session.
- Bind only to `127.0.0.1`; serve only explicit files under `web/`.
- Keep full playback and alternative-source lookup. Alternative matches must agree on normalized title, primary artist, and duration tolerance, and the UI must label them.
- Single-track destructive actions default to a visible instant mode with a three-second local soft-commit window. `Z` cancels the newest uncommitted action; after commit, the UI waits for a verified remote result and never fabricates an undo. Batch mode remains available and always requires grouped confirmation.
- In the liked playlist the destructive action means unlike; in an owned playlist it means remove from that playlist. Bulk and rule-derived actions never bypass confirmation.
- The primary UI is Chinese-first with three top-level workspaces: 整理、曲库、云端. Follow `DESIGN.md`; do not reintroduce the English four-tab dashboard, emoji-led labels, glassmorphism, or generic blue SaaS styling.
- Load liked music and all user-created playlists; support JSON/CSV import, local cache, manual refresh, editable rules, and offline use.
- Cross-platform source; Windows ships as one executable. MIT license.

## Ownership

- Backend worker: `src/**`, `tests/backend/**`, `pyproject.toml`.
- Frontend worker: `web/**`, `tests/frontend/**`.
- Integration owner: root files, docs, build scripts, CI, packaging, final cross-scope fixes.

Workers must not edit paths outside their ownership, initialize or modify Git history, install global software, access the parent directory's private data, call live NetEase write endpoints, or publish anything.

## API contract

All JSON responses use an actual HTTP status and this shape:

```json
{"ok": true, "data": {}, "error": null}
```

Failures use:

```json
{"ok": false, "data": null, "error": {"code": "STABLE_CODE", "message": "User-facing message"}}
```

Required routes are documented in `ARCHITECTURE.md`.
