# Acceptance matrix

Completion claims must stop at the strongest level actually observed.

| Level | Required check | Expected evidence | Cleanup |
| --- | --- | --- | --- |
| Source/config | Repository contains no private runtime data; server binds loopback; static allowlist and schemas are enforced | automated tests plus staged-file audit | none |
| Focused behavior | auth, catalog import, rules, strict audio matching, source-scoped removal, partial/unknown results, reconciliation, undo and progress restoration | deterministic mocked tests | temporary test directories removed by tests |
| Build/package | Windows one-file executable built from the documented command | executable path, SHA-256, build log | retain release artifact |
| Local runtime | executable starts one owned process, reaches `/api/health`, opens UI, persists cache, and restarts | process path/command line, health response, runtime-data path | stop only the test process |
| Desktop UI | login, playlist selection, refresh, review, playback controls, rules, catalog, cloud states, responsive layout | real browser screenshots and interaction notes | close only the test tab/window |
| External read | QR login identifies the actual account and fetches actual playlists/catalog | user-visible account and playlist list without logging credentials | logout only when the test calls for it |
| Audio | original source is audible; a qualifying alternative is audible and visibly labelled; bad matches are rejected | observed playback plus non-sensitive diagnostics | stop playback |
| External write | create a temporary private playlist, add known tracks, and verify upstream result | resulting playlist visible in the official account | requires explicit user approval before the test; remove only the test playlist if approved |
| Unlike | selected song is actually removed and failures remain locally | official-account observation and application result | requires the user to nominate a song and authorize the test |

Source, tests, or packaging never substitute for real login, audible playback, or account mutation evidence.

## Current pre-release evidence (2026-08-18)

- Source/tests: 23 backend tests (plus 12 subtests) and 9 frontend suites pass; Python compile, all JavaScript syntax checks, and the forbidden UI-pattern scan pass.
- Local source runtime: the server starts on an isolated loopback port and `/api/health` returns version `0.1.0` with a healthy result.
- Online non-account-write smoke: the current direct transport creates an HTTPS QR payload, renders its local data image, and polls a `waiting` state without saving a Cookie. No QR was scanned.
- Package/runtime: the rebuilt one-file executable (SHA-256 `84EFA3DB10DE92AD16B8CF3F025F3BBED94B0E8A2D4C6D14A18B527A7EC0206D`) contains the current HTML, JavaScript, CSS, Marcellus font, and OFL notice. It started with an isolated data directory, listened only on `127.0.0.1:8891`, returned a healthy `0.1.0` response, served the three Chinese views and the 46,004-byte font with the expected hash, and was then stopped.
- Browser visual acceptance (unauthenticated shell): the packaged app was observed in the Codex in-app browser at 1440×900, 1024×768, and 768×900. Navigation, QR status, QR regeneration, responsive stacking, zero horizontal overflow at 768px, and a visible 2px keyboard-focus outline were observed; no browser warnings or errors were recorded. Public crops that exclude the temporary login QR are stored under `docs/screenshots/`.
- The Antigravity Opus CLI did not return a usable visual-review result, so it is not counted as acceptance evidence.
- Git reproducibility: the evidence above is tied to the repository snapshot containing this file. Use `git rev-parse HEAD` and the executable SHA-256 above to identify the exact source and package under review.
- Not yet accepted: the authenticated review/catalog/cloud screens, scanned-in account identity, real playlist synchronization, audible original/alternative playback, cloud playlist creation, and unlike. These need user participation and, for writes, explicit approval and a nominated test item.
