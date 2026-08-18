# Pruner Studio design contract

## Subject, audience, and single job

Pruner Studio is a private listening desk for one person pruning a large NetEase music collection. Its single job is to let the owner hear enough of the current song, understand what it is, and make one confident decision without leaving the workspace.

The interface is a tool, not a portfolio page. “Simple premium” means precise typography, quiet surfaces, cover-led color, and short state transitions. It does not mean decorative glass, generic dashboard cards, emoji, WebGL, cursor followers, or hidden efficiency controls.

## Visual system

### Palette

- Night `#0c0d0b`: application background.
- Carbon `#161713`: primary working surface.
- Bone `#f1efe7`: primary text and high-contrast controls.
- Ash `#96938a`: secondary text.
- Keep `#789c7c`: fixed semantic keep state.
- Prune `#ce6f64`: fixed semantic destructive state.

Hairlines use Bone at 10–16% opacity. Elevated surfaces may mix Carbon toward Bone by no more than 5%.

The current cover supplies one non-semantic accent. Normalize it to readable saturation and lightness, expose it through `--cover-accent`, and use it only for playback/progress/focus and the ambient cover field. It must never replace Keep or Prune.

### Typography

- Chinese and body UI: `"Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", sans-serif`.
- Brand and sparse display numerals: locally bundled `Marcellus`, licensed under OFL.
- Utility numerals and timing: `"Cascadia Mono", "SFMono-Regular", Consolas, monospace` with tabular numerals.

Use the display face only for `Pruner Studio`, the current sequence number, and rare section markers. Song titles and controls remain in the body stack.

### Geometry and motion

- Spacing scale: 4, 8, 12, 16, 24, 32, 48px.
- Control radius: 6px. Surface radius: 10px. Never use pill shapes for ordinary controls.
- Fast feedback: 120ms. Standard transition: 180ms. Song decision/cover change: 260ms.
- Motion uses opacity and small transforms only. No bounce curves. `prefers-reduced-motion` removes transforms and collapses durations to near-zero.
- Visible keyboard focus uses a 2px cover-accent outline plus a Night separation ring.

## Signature

The signature is the **cover field**: a low-opacity, heavily softened continuation of the current album cover behind the working plane. It changes once when the song changes and stays still while the user decides. The large cover remains crisp. This makes every screen belong to the music being judged rather than to a reusable dashboard template.

## Layouts

### Login

```text
┌────────────────────────────────────────────────────────────┐
│ PRUNER STUDIO                                      本机运行 │
│                                                            │
│  让曲库只留下                 ┌─────────────────────────┐   │
│  你还会再听的歌。             │        QR CODE          │   │
│                               │                         │   │
│  private listening desk       │  等待扫码 / 手机确认    │   │
│  [quiet cover texture]        │  重新生成               │   │
│                               └─────────────────────────┘   │
└────────────────────────────────────────────────────────────┘
```

### Review workspace

```text
┌ Brand ─ 整理 曲库 云端 ─── user · total · kept · pending ┐
├ playlist | factual filter | order | reset ─────────────────┤
├ progress 128/500 ─────────── remaining 372 · about 18 min ┤
│                                                             │
│  ┌────────────────── 42% ┐  ┌────────────────────── 58% ┐   │
│  │                      │  │ TITLE                     │   │
│  │     ALBUM COVER      │  │ artist / album / timing  │   │
│  │                      │  │ source / availability    │   │
│  │                      │  │                           │   │
│  └──────────────────────┘  │ previous lyric           │   │
│                            │ CURRENT LYRIC             │   │
│                            │ translated lyric          │   │
│                            │ next lyric                │   │
│                            └───────────────────────────┘   │
├ mode: 立即同步(3秒) ─ prune · undo · skip · play · keep ──┤
└─────────────────────────────────────────────────────────────┘
```

### Catalog

```text
┌ search ─ playlist ─ status ─ sort ─ [table|cards] ─ rules ┐
├ selection bar appears only when needed                     ┤
│ table: dense rows, 50/page                                  │
│ cards: five compact columns, same result order and page     │
├─────────────────────────────────────────────────────────────┤
│ compact player exists only in this workspace                │
└─────────────────────────────────────────────────────────────┘
```

### Cloud

```text
┌ synced playlists / refresh ───── pending grouped actions ┐
│ liked + owned playlists         unlike / remove by target │
├───────────────────────────────────────────────────────────┤
│ More tools ▸  backup · local import · operation history   │
└───────────────────────────────────────────────────────────┘
```

### Pending drawer and confirmation

```text
┌ 待执行 4 ───────────────────────────────┐
│ 即将提交 (3 秒)                         │
│  Song A · 取消喜欢          02.4s  取消 │
│ 稍后确认                                 │
│  喜欢的音乐 · 2 首                 移除 │
│  Road Trip · 1 首                  移除 │
└──────────────────────────────────────────┘

┌ 确认云端变更 ───────────────────────────┐
│ 取消喜欢                         12 首   │
│ 从 Road Trip 移出                 4 首   │
│ 自动快照将在执行前创建                    │
│                         取消  确认执行   │
└──────────────────────────────────────────┘
```

## Interaction contract

- New accounts resolve the real `kind=liked` playlist ID, sync it, order recent-first, and enter the first song. Never use a synthetic `liked` ID.
- Review controls stay visible. Factual filters include undecided, skipped, rule matched, VIP, unavailable, and duration ranges.
- `←` destructive, `→` keep, `Space` skip-to-end, `P` play/pause, `C` or `↑` chorus, `Z` undo newest uncommitted action.
- Single destructive actions default to visible instant mode. The UI advances immediately, holds the remote write for three seconds, and shows a cancellable countdown. Closing the application cancels soft commits; internal navigation does not.
- Drag gestures default off. The first destructive action shows one concise explanation, once.
- In liked music, destructive means unlike. In an owned playlist, it means remove from that playlist. The label must change with context.
- Batch mode persists across restarts. Bulk catalog and rule-derived actions always enter grouped batch confirmation.
- A remote write with an unknown result freezes local destructive state and triggers read-only reconciliation. Never blind-retry it and never remove unknown IDs locally.
- Keep completes review. Skip moves the song to the end of the current round. Progress is stored per account + playlist + filter + order, with an explicit reset.
- Loading, unauthenticated, not synced, truly empty, filter empty, review complete, failure, and unknown remote result are distinct states with a relevant next action.
- Catalog table and card views share search, filters, sort, page, selection, and playback state. They use 50 items per page. View switching preserves selection; changing search/filter/playlist clears it with notice.
- Card cover play button plays, checkbox selects, and title opens details. Rules only preview/select/stage; they never execute cloud writes.

## Module direction

- Retain and adapt: `api.js`, `auth.js` QR lifecycle, `player.js` transport/playback, `lyrics.js` parsing/sync, safe DOM helpers in `utils.js`.
- Rewrite: `index.html`, CSS system, `app.js` routing/bootstrap, `deck.js` review workflow, `catalog.js` shared table/card state, `cloud.js` grouped operations.
- Merge state ownership: move duplicated catalog filter/search/sort/view state into `state.js`; add account-scoped persistence, batch queue, soft commits, loading/error state, and canonical playlist selection.
- Move rules from a top-level panel into a catalog drawer while retaining `rules.js` matching and editor behavior.
- Delete after migration: English four-tab markup, global player outside catalog, standalone top-level rules panel, duplicate deck/global playback controls, emoji-led headings, obsolete dashboard CSS.

## Acceptance checklist

- Login resolves and syncs the real liked playlist regardless of playlist/catalog response order.
- Login/loading/not-synced/empty/filter-empty/complete/error states are not conflated.
- Review shows the confirmed layout at 2560, 1920, 1280, and 1024 widths; narrow layout stacks without hiding actions.
- All keyboard mappings and visible focus states work; reduced motion is honored.
- Keep, skip-to-end, reset, and account-scoped progress survive restart as specified.
- Soft commit visibly counts down, `Z` cancels it, navigation preserves it, and application close prevents submission.
- Liked unlike and owned-playlist removal call different verified operations.
- Failed and unknown operations never disappear locally. Unknown triggers read-only reconciliation only.
- Table/card view state is shared; filters clear hidden selections; both paginate the same 50-result slice.
- Rule and bulk operations cannot bypass grouped confirmation.
- Automatic snapshots, export, import source isolation, and the last 100 operation records behave as labelled.
- No emoji, glass panels, generic blue accent system, persistent ambient animation, or unexplained technical English remains in primary UI.
