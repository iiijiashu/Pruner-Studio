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

## Current pre-release evidence (2026-08-30)

- Source/tests: 45 backend tests and 9 frontend suites pass. Python compilation, every `web/js/*.js` syntax check, the forbidden UI-pattern scan, and `git diff --check` pass.
- Recovery and safety: deterministic tests cover account/session races, restart-persistent unknown writes, create/add/unlike/remove reconciliation without write retries, fail-closed corrupt data, non-expiring ignored tombstones, atomic create-to-add journal conversion, preserved create guards, request-origin validation, and exclusive data-directory ownership.
- Reproducible build inputs: the Windows x64 / CPython 3.11 wheel lock is exact and hash-enforced; clean-style dependency resolution verified all ten hashes. PyInstaller is `6.22.2`, its hooks bundle is `2026.6`, and `pip check` reports no broken requirements.
- Package: `dist\Pruner-Studio.exe` is a 16,917,671-byte one-file version `0.2.0` artifact with SHA-256 `0D653E0B364CFE38A6103BFFF8C683FD656015D5E4069854280DCA44041F962E`. Windows metadata reports FileVersion/ProductVersion `0.2.0`, ProductName `Pruner Studio`, OriginalFilename `Pruner-Studio.exe`, and CompanyName `Pruner Studio contributors`.
- Packaged local runtime: the artifact started as one owned process with an isolated data directory, listened only on `127.0.0.1:18991`, returned healthy version `0.2.0`, served the current page, and generated a local PNG QR image. A second artifact process using the same data directory and port base `18992` exited before startup with the single-instance message instead of changing ports or touching the journal. The owned process was stopped and both test ports were confirmed closed.
- Online non-account-write smoke: the packaged direct transport created a fresh HTTPS QR payload and one poll returned `waiting`. No QR was scanned, no Cookie was saved, and no authenticated or write endpoint was called.
- Browser acceptance of the same packaged artifact (unauthenticated shell): 1440×900, 1024×768, and 768×900 all rendered the QR state with zero horizontal overflow. QR regeneration returned to `waiting`, keyboard navigation exposed a solid 2px focus outline, and the browser recorded no warning or error entries. The temporary QR was not saved or published.
- CI hardening: workflow permissions are read-only, checkout credentials are not persisted, and all third-party actions are pinned to verified commit SHAs. The Ubuntu job imports the source and exercises the POSIX lock; the Windows job runs the complete tests and rebuilds the executable. Hosted results must be checked after the commit is pushed.
- Evidence invalidation: any later executable source, web asset, dependency lock, spec, entry point, or version-resource change invalidates the package hash and requires the package/runtime rows to be repeated.
- Not yet accepted: scanned-in account identity, authenticated review/catalog/cloud views, real playlist synchronization, audible original/alternative playback, cloud playlist creation/addition, remove-from-playlist, and unlike. Those boundaries require user participation; mutation acceptance additionally requires a nominated disposable target.
