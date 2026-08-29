/**
 * Pruner Studio - Review Workspace Engine
 * 42/58 Workspace layout, exact keyboard shortcuts, 3-second soft commit countdown,
 * skip-to-end, account-scoped progress persistence, and 8 distinct state representations.
 */
import { api } from './api.js';
import { state } from './state.js';
import { player } from './player.js';
import { toast } from './toast.js';
import { modal } from './modal.js';
import { RuleEngine } from './rules.js';
import { lyricsManager } from './lyrics.js';
import {
  createElement,
  formatDuration,
  formatArtists,
  getSafeImageUrl,
  isSafeUrl,
  estimateRemainingTime,
  getCoverAccent,
} from './utils.js';

class DeckReviewEngine {
  constructor() {
    this.container = null;
    this.isDragging = false;
    this.startX = 0;
    this.startY = 0;
    this.currentX = 0;
    this.currentY = 0;
    this.dragThreshold = 90;
    this.activeDragCard = null;
    this.sessionSnapshotReady = false;
    this.sessionSnapshotPromise = null;
  }

  init(containerId) {
    this.container = document.getElementById(containerId);
    this.initKeyboardControls();
  }

  initKeyboardControls() {
    document.addEventListener('keydown', (e) => {
      // Ignore if focus is in an input/textarea or any modal is open
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
      if (document.querySelector('.modal-backdrop.open')) return;

      // Only handle if deck view is active
      const deckPanel = document.getElementById('view-deck');
      if (!deckPanel || !deckPanel.classList.contains('active')) return;

      switch (e.key) {
        case 'ArrowLeft':
        case 'a':
        case 'A':
          e.preventDefault();
          this.handleDestructiveAction();
          break;
        case 'ArrowRight':
        case 'd':
        case 'D':
          e.preventDefault();
          this.handleKeepAction();
          break;
        case ' ':
          e.preventDefault();
          this.handleSkipToEndAction();
          break;
        case 'p':
        case 'P':
          e.preventDefault();
          player.togglePlay();
          break;
        case 'ArrowUp':
        case 'w':
        case 'W':
        case 'c':
        case 'C':
          e.preventDefault();
          player.jumpToChorus();
          break;
        case 'z':
        case 'Z':
          e.preventDefault();
          this.handleUndoAction();
          break;
      }
    });
  }

  /**
   * Filter and order catalog songs for the current review queue.
   * @param {Array} catalog
   * @param {number|string|null} playlistId
   * @param {string} filter
   * @param {string} order
   * @returns {Array}
   */
  filterAndOrderTracks(catalog, playlistId, filter = 'undecided', order = 'recent') {
    if (!catalog || catalog.length === 0) return [];
    const rules = state.get('rules') || [];
    const keptIds = state.get('keptIds') || new Set();
    const batchQueue = state.get('batchQueue') || [];
    const stagedKeys = new Set(batchQueue.map(b => state.reviewSongKey(b.song, b.playlistId)));
    const likedPl = state.getRealLikedPlaylist();
    const likedPlId = likedPl ? String(likedPl.id) : null;

    // 1. Playlist match
    let list = catalog.filter(song => {
      if (playlistId === 'all') return true;
      if (playlistId) {
        return String(song.source_playlist_id) === String(playlistId);
      }
      if (likedPlId) {
        return String(song.source_playlist_id) === likedPlId;
      }
      return true;
    });

    // 2. Factual filter
    list = list.filter(song => {
      const songIdStr = String(song.id);
      if (filter === 'undecided') {
        return !keptIds.has(songIdStr) && !stagedKeys.has(state.reviewSongKey(song, playlistId));
      }
      if (filter === 'skipped') {
        const skipped = state.get('skippedIds') || new Set();
        return skipped.has(songIdStr);
      }
      if (filter === 'rules') {
        return RuleEngine.getMatchingRules(song, rules).length > 0;
      }
      if (filter === 'vip') {
        return song.availability === 'vip';
      }
      if (filter === 'unavailable') {
        return song.availability === 'unavailable';
      }
      if (filter === 'short') {
        const sec = song.duration > 10000 ? song.duration / 1000 : song.duration;
        return sec < 90;
      }
      if (filter === 'long') {
        const sec = song.duration > 10000 ? song.duration / 1000 : song.duration;
        return sec > 300;
      }
      return true; // 'all'
    });

    // 3. Sorting
    const sorted = [...list];
    if (order === 'recent') {
      sorted.reverse();
    } else if (order === 'oldest') {
      // standard order
    } else if (order === 'title') {
      sorted.sort((a, b) => (a.title || '').localeCompare(b.title || ''));
    } else if (order === 'duration') {
      sorted.sort((a, b) => (b.duration || 0) - (a.duration || 0));
    }

    return sorted;
  }

  /**
   * Loads tracks into the review queue with strict progress restoration order.
   */
  reloadDeck() {
    const catalog = state.get('catalog') || [];
    const selectedPlaylistId = state.get('selectedPlaylistId');
    const filter = state.get('deckFilter') || 'undecided';
    const order = state.get('deckOrder') || 'recent';

    // 1. FIRST: Load saved progress for the current account + playlist + filter + order
    const saved = state.loadReviewProgress(selectedPlaylistId, filter, order);

    // 2. Initialize fresh Sets to prevent leakage across context switches
    const keptIds = new Set(Array.isArray(saved?.keptIds) ? saved.keptIds.map(String) : []);
    const skippedIds = new Set(Array.isArray(saved?.skippedIds) ? saved.skippedIds.map(String) : []);
    state.set({ keptIds, skippedIds });

    // 3. Construct filtered tracks using the newly loaded kept/skipped sets
    const tracks = this.filterAndOrderTracks(catalog, selectedPlaylistId, filter, order);

    // 4. Set deckQueue and start index
    let startIndex = 0;
    if (saved?.currentSongKey) {
      const anchoredIndex = tracks.findIndex(song => state.reviewSongKey(song, selectedPlaylistId) === saved.currentSongKey);
      startIndex = anchoredIndex >= 0 ? anchoredIndex : 0;
    } else if (saved && typeof saved.index === 'number') {
      startIndex = Math.max(0, Math.min(saved.index, tracks.length));
    }

    state.set({
      deckQueue: tracks,
      deckIndex: startIndex,
      deckHistory: [],
    });

    this.render();

    // Auto-play first song if enabled
    const current = this.getCurrentSong();
    if (current && state.get('autoplayEnabled')) {
      player.playSong(current, true);
    }
  }

  getCurrentSong() {
    const queue = state.get('deckQueue') || [];
    const index = state.get('deckIndex') || 0;
    return queue[index] || null;
  }

  /**
   * Determine the current active UI state among 8 distinct states + unknown_result state.
   * @returns {'loading'|'unauthenticated'|'not_synced'|'true_empty'|'filter_empty'|'complete'|'error'|'unknown_result'|'active'}
   */
  determineWorkspaceState() {
    if (state.get('hasUnknownResult') || state.get('destructiveFrozen')) return 'unknown_result';
    if (state.get('catalogLoading')) return 'loading';
    if (!state.get('authenticated')) return 'unauthenticated';
    if (state.get('catalogError')) return 'error';

    const playlists = state.get('playlists') || [];
    const selectedId = state.get('selectedPlaylistId');
    const currentPl = playlists.find(p => String(p.id) === String(selectedId));

    const catalog = state.get('catalog') || [];
    const queue = state.get('deckQueue') || [];
    const index = state.get('deckIndex') || 0;

    // Check if playlist is not yet synced in local cache
    const likedPl = state.getRealLikedPlaylist();
    const targetPlId = selectedId || (likedPl ? likedPl.id : null);
    const plTracksInCatalog = catalog.filter(s => String(s.source_playlist_id) === String(targetPlId));

    if (currentPl && currentPl.track_count > 0 && plTracksInCatalog.length === 0) {
      return 'not_synced';
    }

    if (plTracksInCatalog.length === 0 && (!currentPl || currentPl.track_count === 0)) {
      return 'true_empty';
    }

    if (queue.length === 0 && plTracksInCatalog.length > 0) {
      return 'filter_empty';
    }

    if (index >= queue.length && queue.length > 0) {
      return 'complete';
    }

    return 'active';
  }

  render() {
    if (!this.container) return;
    this.container.textContent = ''; // Safe clear

    const workspaceState = this.determineWorkspaceState();
    this.updateProgressInfo();

    if (workspaceState !== 'active') {
      this.renderStatePlaceholder(workspaceState);
      return;
    }

    const currentSong = this.getCurrentSong();
    if (!currentSong) {
      this.renderStatePlaceholder('complete');
      return;
    }

    // Render 42% / 58% Review Workspace Layout
    const workspaceEl = this.createReviewWorkspaceLayout(currentSong);
    this.container.appendChild(workspaceEl);

    // Sync lyrics into the fresh DOM container
    lyricsManager.renderDeckLyrics();
    if (player && player.audio && !player.audio.paused) {
      lyricsManager.updateTime(player.audio.currentTime);
    }

    // Update Action Button Labels (e.g. 取消喜欢 vs 移出歌单)
    this.updateDestructiveButtonLabel();
  }

  /**
   * Update Progress and Remaining time header
   */
  updateProgressInfo() {
    const queue = state.get('deckQueue') || [];
    const index = state.get('deckIndex') || 0;
    const countDisplay = document.getElementById('deck-count-display');
    const remainingDisplay = document.getElementById('deck-remaining-display');
    const progressBar = document.getElementById('deck-progress-bar-fill');

    const total = queue.length;
    const currentNum = total > 0 ? Math.min(index + 1, total) : 0;
    const remaining = Math.max(0, total - index);

    if (countDisplay) {
      countDisplay.textContent = total > 0 ? `${currentNum} / ${total}` : '0 / 0';
    }
    if (remainingDisplay) {
      remainingDisplay.textContent = total > 0 ? `剩余 ${remaining} 首 · ${estimateRemainingTime(remaining)}` : '全部已整理';
    }
    if (progressBar) {
      const pct = total > 0 ? Math.min(100, Math.round((index / total) * 100)) : 0;
      progressBar.style.width = `${pct}%`;
    }
  }

  /**
   * Constructs the 42% / 58% Split Review Layout DOM safely.
   * @param {Song} song
   * @returns {HTMLElement}
   */
  createReviewWorkspaceLayout(song) {
    const root = createElement('div', { className: 'review-workspace-grid' });

    // LEFT: 42% Cover Area
    const leftPane = createElement('div', { className: 'review-left-pane' });
    const coverContainer = createElement('div', {
      className: 'review-cover-card',
      dataset: { songId: String(song.id) }
    });

    const coverImg = createElement('img', {
      className: 'review-cover-img',
      attributes: {
        src: isSafeUrl(song.cover_url) ? song.cover_url : getSafeImageUrl('', '音'),
        alt: song.title || '封面',
        loading: 'eager',
      }
    });

    // Top badges on cover
    const badgesRow = createElement('div', { className: 'review-cover-badges' });
    if (song.availability === 'unavailable') {
      badgesRow.appendChild(createElement('span', { className: 'badge badge-danger', text: '无音源' }));
    } else if (song.availability === 'vip') {
      badgesRow.appendChild(createElement('span', { className: 'badge badge-warning', text: 'VIP' }));
    }

    if (song.source_type === 'alternative' || song.is_alternative) {
      badgesRow.appendChild(createElement('span', { className: 'badge badge-alt', text: '替代音源' }));
    }

    const rules = state.get('rules') || [];
    const matchedRules = RuleEngine.getMatchingRules(song, rules);
    if (matchedRules.length > 0) {
      badgesRow.appendChild(createElement('span', {
        className: 'badge badge-warning',
        text: `规则: ${matchedRules[0].name}`
      }));
    }

    // Optional drag overlay stamps
    const likeIndicator = createElement('div', { className: 'card-action-indicator indicator-like', text: '保留' });
    const dislikeIndicator = createElement('div', { className: 'card-action-indicator indicator-dislike', text: '移除' });

    coverContainer.appendChild(coverImg);
    coverContainer.appendChild(badgesRow);
    coverContainer.appendChild(likeIndicator);
    coverContainer.appendChild(dislikeIndicator);

    // Attach optional drag gestures if enabled
    if (state.get('dragEnabled')) {
      this.attachDragListeners(coverContainer);
    }

    leftPane.appendChild(coverContainer);

    // RIGHT: 58% Song Details & Contextual Translated Lyrics
    const rightPane = createElement('div', { className: 'review-right-pane' });

    // Song Header Info
    const infoHeader = createElement('div', {
      className: 'review-song-info',
      children: [
        createElement('h2', { className: 'review-song-title', text: song.title || '未知曲目' }),
        createElement('div', { className: 'review-song-artist', text: formatArtists(song.artists) }),
        createElement('div', {
          className: 'review-meta-line',
          children: [
            createElement('span', { text: song.album ? `专辑: ${song.album}` : '单曲' }),
            createElement('span', { text: `· 时长: ${formatDuration(song.duration)}` }),
            createElement('span', { text: `· 来源: ${song.source_playlist_name || '歌单'}` }),
          ]
        }),
      ]
    });

    // Synchronized Lyrics Box
    const lyricsBox = createElement('div', {
      className: 'deck-lyrics-wrapper',
      children: [
        createElement('div', { className: 'deck-lyrics-header', text: '同步歌词' }),
        createElement('div', { attributes: { id: 'deck-lyrics-container' }, className: 'deck-lyrics-scroller' }),
      ]
    });

    rightPane.appendChild(infoHeader);
    rightPane.appendChild(lyricsBox);

    root.appendChild(leftPane);
    root.appendChild(rightPane);

    return root;
  }

  /**
   * Updates destructive button text (取消喜欢 vs 移出歌单) based on active playlist.
   */
  updateDestructiveButtonLabel() {
    const dislikeBtn = document.getElementById('deck-dislike-btn');
    if (!dislikeBtn) return;

    const currentSong = this.getCurrentSong();
    const selectedId = state.get('selectedPlaylistId');
    const resolved = state.resolveDestructiveAction(currentSong, selectedId);

    const label = resolved.valid ? (resolved.action === 'unlike' ? '取消喜欢' : '移出歌单') : '不可移除';
    dislikeBtn.title = `${label} [← / A]`;
    const labelSpan = dislikeBtn.querySelector('.btn-label');
    if (labelSpan) labelSpan.textContent = label;
  }

  resetSessionSnapshot() {
    this.sessionSnapshotReady = false;
    this.sessionSnapshotPromise = null;
  }

  async ensureSessionSnapshot() {
    if (this.sessionSnapshotReady) return true;
    if (!this.sessionSnapshotPromise) {
      this.sessionSnapshotPromise = api.backup({ reason: 'auto_snapshot_before_first_destructive_write' })
        .then((result) => {
          this.sessionSnapshotReady = Boolean(result?.ok);
          return this.sessionSnapshotReady;
        })
        .catch(() => false)
        .finally(() => {
          this.sessionSnapshotPromise = null;
        });
    }
    return this.sessionSnapshotPromise;
  }

  /**
   * Action 1: Destructive Action (Unlike / Remove from Playlist)
   * Instant Mode: advances immediately + 3s local countdown timer.
   * Batch Mode: stages for later confirmation.
   */
  handleDestructiveAction() {
    if (state.get('destructiveFrozen') || state.get('hasUnknownResult')) {
      toast.warning('破坏性操作已冻结', '存在未确定的云端变更，请先完成只读核对。');
      return;
    }

    const song = this.getCurrentSong();
    if (!song) return;

    const selectedId = state.get('selectedPlaylistId');
    const resolved = state.resolveDestructiveAction(song, selectedId);

    if (!resolved.valid) {
      toast.warning('不可移除', resolved.reason || '该歌曲属于本地导入或无远端目标，无法执行云端移除。');
      return;
    }

    // Show first-use explanation once
    this.checkFirstUseExplanation();

    const isInstant = state.get('instantModeEnabled');

    if (isInstant) {
      // INSTANT MODE: Advance UI immediately & register 3s soft commit
      const currentIndex = state.get('deckIndex') || 0;
      state.set({ deckIndex: currentIndex + 1 });
      this.saveCurrentProgress();

      state.createSoftCommit(
        song,
        resolved.action,
        resolved.playlistId,
        resolved.targetName,
        // Commit callback on expiry:
        async () => {
          try {
            if (state.get('destructiveFrozen') || state.get('hasUnknownResult')) {
              this.restoreSongToDeck(song, currentIndex);
              toast.warning('操作已冻结', '检测到未确定的云端结果，本次没有发送写请求。');
              return;
            }
            if (!(await this.ensureSessionSnapshot())) {
              this.restoreSongToDeck(song, currentIndex);
              toast.error('自动快照失败', '本次云端操作未执行。请检查本地数据目录后重试。');
              return;
            }

            let res;
            if (resolved.action === 'unlike') {
              res = await api.unlike([song.id], resolved.playlistId);
            } else {
              res = await api.removeTracks(resolved.playlistId, [song.id], resolved.playlistId);
            }

            const isSuccess = res.ok && res.data && Array.isArray(res.data.successful_ids) && res.data.successful_ids.map(String).includes(String(song.id));
            const unknownItems = res.ok && res.data && Array.isArray(res.data.unknown_items)
              ? res.data.unknown_items
              : [];
            const legacyUnknownItems = res.ok && res.data && Array.isArray(res.data.failed_items)
              ? res.data.failed_items.filter(item => String(item.code || '').endsWith('_RESULT_UNKNOWN'))
              : [];
            const isUnknown = [...unknownItems, ...legacyUnknownItems].some(item => String(item.id) === String(song.id));

            if (isSuccess) {
              state.removeConfirmedSongs([song.id], resolved.playlistId, resolved.action);
              state.addOperationLog({
                type: resolved.action === 'unlike' ? '取消喜欢' : '移出歌单',
                requested: 1,
                successful: 1,
                failed: 0,
                targetName: resolved.targetName,
                message: `成功${resolved.action === 'unlike' ? '取消喜欢' : '移出'}: "${song.title}"`,
              });
              toast.success('已同步至云端', `"${song.title}" 已从${resolved.targetName}同步处理。`);
            } else if (isUnknown) {
              await this.handleCommitFailureWithReconcile(
                song,
                resolved.action,
                resolved.playlistId,
                resolved.targetName,
                currentIndex,
                res.data?.pending_operation_ids?.[0] || null,
              );
            } else if (res.ok && res.data && Array.isArray(res.data.failed_items) && res.data.failed_items.some(f => String(f.id) === String(song.id))) {
              const failReason = res.data.failed_items.find(f => String(f.id) === String(song.id))?.reason || '远程操作被拒绝';
              toast.error('云端同步失败', `未能处理 "${song.title}": ${failReason}`);
              this.restoreSongToDeck(song, currentIndex);
            } else {
              // Non-success / network error / unknown status -> read-only reconcile
              await this.handleCommitFailureWithReconcile(
                song,
                resolved.action,
                resolved.playlistId,
                resolved.targetName,
                currentIndex,
                res.data?.pending_operation_ids?.[0] || null,
              );
            }
          } catch (err) {
            await this.handleCommitFailureWithReconcile(song, resolved.action, resolved.playlistId, resolved.targetName, currentIndex);
          }
        },
        // Cancel callback on undo or window close:
        (softCommit) => {
          this.restoreSongToDeck(song, softCommit?.originalIndex ?? currentIndex);
          toast.info('已取消操作', `已撤销针对 "${song.title}" 的同步。`);
        },
        currentIndex
      );

      this.advanceToNextSong();
    } else {
      // BATCH MODE: stage in batch queue
      state.stageBatchAction(song, resolved.action, resolved.playlistId, resolved.targetName);
      const history = [...(state.get('deckHistory') || []), { song, action: resolved.action, playlistId: resolved.playlistId }];
      const currentIndex = state.get('deckIndex') || 0;

      state.set({
        deckHistory: history,
        deckIndex: currentIndex + 1,
      });
      this.saveCurrentProgress();

      toast.info('已暂存至待执行队列', `"${song.title}" 已加入待提交队列。`);
      this.advanceToNextSong();
    }
  }

  /**
   * Restores a song back to the current active review position.
   * @param {Song} song
   * @param {number|null} targetIndex
   */
  restoreSongToDeck(song, targetIndex = null) {
    const queue = state.get('deckQueue') || [];
    const songKey = state.reviewSongKey(song, state.get('selectedPlaylistId'));
    const songIndex = queue.findIndex(s => state.reviewSongKey(s, state.get('selectedPlaylistId')) === songKey);
    let newIndex = targetIndex !== null ? targetIndex : (state.get('deckIndex') || 0) - 1;
    if (songIndex >= 0) {
      newIndex = songIndex;
    }
    newIndex = Math.max(0, Math.min(newIndex, Math.max(0, queue.length - 1)));

    state.set({ deckIndex: newIndex });
    this.saveCurrentProgress();
    this.render();

    const current = this.getCurrentSong();
    if (current && state.get('autoplayEnabled')) {
      player.playSong(current, true);
    }
  }

  /**
   * Handles commit failure by running read-only reconciliation. Freezes on doubt.
   * @param {Song} song
   * @param {string} action
   * @param {number|string|null} playlistId
   * @param {string} targetName
   */
  async handleCommitFailureWithReconcile(song, action, playlistId, targetName, originalIndex = null, operationId = null) {
    toast.info('正在只读核对', `网络未确认 "${song.title}" 状态，正在核对云端结果...`);
    try {
      const recRes = await api.reconcileOperations(action, playlistId, [song.id], playlistId);
      if (recRes.ok && recRes.data && Array.isArray(recRes.data.successful_ids) && recRes.data.successful_ids.map(String).includes(String(song.id))) {
        state.removeConfirmedSongs([song.id], playlistId, action);
        state.set({ hasUnknownResult: false, destructiveFrozen: false, unknownResultDetail: null });
        toast.success('已核对确认', `"${song.title}" 经只读核对已成功在远端处理。`);
        this.render();
        return;
      }
      if (recRes.ok && recRes.data && Array.isArray(recRes.data.failed_items) && recRes.data.failed_items.some(item => String(item.id) === String(song.id))) {
        this.restoreSongToDeck(song, originalIndex);
        state.set({ hasUnknownResult: false, destructiveFrozen: false, unknownResultDetail: null });
        toast.info('云端未发生变更', `"${song.title}" 已保留，可以继续整理。`);
        return;
      }
    } catch (err) {
      console.warn('Reconciliation call failed:', err);
    }

    // Remote status remains unknown: freeze destructive actions and display unknown_result state
    this.restoreSongToDeck(song, originalIndex);
    let pendingOperation = null;
    try {
      const pending = await api.getPendingOperations();
      if (pending.ok && Array.isArray(pending.data?.operations)) {
        state.restorePendingOperations(pending.data.operations);
        pendingOperation = pending.data.operations.find(item => item.operation_id === operationId)
          || pending.data.operations.find(item => (
            item.action === action
            && String(item.target?.source_playlist_id ?? item.target?.playlist_id) === String(playlistId)
            && (item.target?.song_ids || []).map(String).includes(String(song.id))
          ))
          || null;
      }
    } catch {}
    state.set({
      hasUnknownResult: true,
      destructiveFrozen: true,
      unknownResultDetail: {
        song,
        action,
        playlistId,
        targetName,
        originalIndex,
        operationId: pendingOperation?.operation_id || operationId,
        pendingOperation,
      },
    });
    toast.error('远端状态未确定', `未能确认 "${song.title}" 的远端执行结果，破坏性操作已冻结。禁止盲目重试。`);
    this.render();
  }

  async refreshRecoveredOperations() {
    const pending = await api.getPendingOperations();
    if (!pending.ok || !pending.data || !Array.isArray(pending.data.operations)) {
      return false;
    }
    state.restorePendingOperations(pending.data.operations);
    this.render();
    return true;
  }

  async reconcileRecoveredOperation(operation) {
    if (!operation?.operation_id) return false;
    const result = await api.reconcilePendingOperation(operation.operation_id);
    if (!result.ok) {
      toast.error('只读核对失败', result.error?.message || '暂时无法确认远端结果，写操作仍保持冻结。');
      return false;
    }
    if (!(await this.refreshRecoveredOperations())) {
      toast.warning('核对结果待刷新', '远端核对已返回，但恢复日志暂时无法刷新；写操作仍保持冻结。');
      return false;
    }
    if ((state.get('pendingOperations') || []).length === 0) {
      toast.success('恢复核对完成', '未确定操作已得到确认，破坏性操作已解除冻结。');
      state.emit('auth:success', state.get('user'));
    }
    return true;
  }

  async ignoreRecoveredOperation(operation) {
    if (!operation?.operation_id) return false;
    const result = await api.ignorePendingOperation(operation.operation_id);
    if (!result.ok) {
      toast.error('无法忽略操作', result.error?.message || '恢复日志未更新，写操作仍保持冻结。');
      return false;
    }
    if (!(await this.refreshRecoveredOperations())) return false;
    toast.info('已明确忽略', '该语义操作仍被后端标记，后续不会盲目重发。');
    return true;
  }

  async dismissUnknownResultLocally() {
    const detail = state.get('unknownResultDetail');
    const operationId = detail?.pendingOperation?.operation_id || detail?.operationId;
    if (operationId) {
      await this.ignoreRecoveredOperation(detail.pendingOperation || { operation_id: operationId });
      return;
    }
    toast.error('无法忽略操作', '恢复日志标识不可用，写操作仍保持冻结。请重新加载后再试。');
  }

  /**
   * Action 2: Keep Action (Keep Song, Advance)
   */
  handleKeepAction() {
    const song = this.getCurrentSong();
    if (!song) return;

    const keptIds = new Set(state.get('keptIds') || []);
    keptIds.add(String(song.id));

    const currentIndex = state.get('deckIndex') || 0;
    const history = [...(state.get('deckHistory') || []), { song, action: 'keep' }];

    state.set({
      keptIds,
      deckHistory: history,
      deckIndex: currentIndex + 1,
    });
    this.saveCurrentProgress();

    this.advanceToNextSong();
  }

  /**
   * Action 3: Skip to End of Current Round (Spacebar)
   */
  handleSkipToEndAction() {
    const song = this.getCurrentSong();
    if (!song) return;

    const queue = [...(state.get('deckQueue') || [])];
    const currentIndex = state.get('deckIndex') || 0;

    // Move current song from current index to the end of the queue
    const skippedSong = queue.splice(currentIndex, 1)[0];
    queue.push(skippedSong);

    const skippedIds = new Set(state.get('skippedIds') || []);
    skippedIds.add(String(song.id));

    state.set({
      deckQueue: queue,
      skippedIds,
      // Keep deckIndex at current position because queue was shifted
    });
    this.saveCurrentProgress();

    toast.info('已跳至本轮末尾', `"${song.title}" 将在最后再次出现。`);
    this.render();

    const nextSong = this.getCurrentSong();
    if (nextSong && state.get('autoplayEnabled')) {
      player.playSong(nextSong, true);
    }
  }

  /**
   * Action 4: Undo Action (Z Key)
   * Cancels newest uncommitted soft commit, or reverts previous batch review action.
   */
  handleUndoAction() {
    const isInstant = state.get('instantModeEnabled');

    if (isInstant) {
      const cancelled = state.cancelNewestCommit();
      if (!cancelled) {
        toast.info('无可撤销项', '当前没有正在倒计时的未提交操作。');
      }
      return;
    }

    // Batch mode undo
    const history = state.get('deckHistory') || [];
    if (history.length === 0) {
      toast.info('无可撤销项', '当前轮次没有可撤销的整理操作。');
      return;
    }

    const lastAction = history[history.length - 1];
    const currentIndex = state.get('deckIndex') || 0;

    if (currentIndex > 0) {
      const nextIndex = currentIndex - 1;
      const nextHistory = history.slice(0, -1);

      if (lastAction.action !== 'keep') {
        state.unstageBatchAction(lastAction.song.id, lastAction.playlistId, lastAction.action);
      } else {
        const keptIds = new Set(state.get('keptIds') || []);
        keptIds.delete(String(lastAction.song.id));
        state.set({ keptIds });
      }

      state.set({
        deckIndex: nextIndex,
        deckHistory: nextHistory,
      });
      this.saveCurrentProgress();

      this.render();
      const song = this.getCurrentSong();
      if (song) {
        player.playSong(song, true);
        toast.info('已撤销', `已恢复 "${song.title}"。`);
      }
    }
  }

  advanceToNextSong() {
    this.render();
    const nextSong = this.getCurrentSong();
    if (nextSong && state.get('autoplayEnabled')) {
      player.playSong(nextSong, true);
    }
  }

  saveCurrentProgress() {
    const playlistId = state.get('selectedPlaylistId');
    const filter = state.get('deckFilter') || 'undecided';
    const order = state.get('deckOrder') || 'recent';
    const index = state.get('deckIndex') || 0;
    const keptIds = Array.from(state.get('keptIds') || []);
    const skippedIds = Array.from(state.get('skippedIds') || []);

    state.saveReviewProgress(playlistId, filter, order, {
      index,
      currentSongKey: state.reviewSongKey(this.getCurrentSong(), playlistId),
      keptIds,
      skippedIds,
      updatedAt: Date.now(),
    });
  }

  checkFirstUseExplanation() {
    const userId = state.getUserId();
    const key = `pruner_destructive_intro_shown_${userId}`;
    if (localStorage.getItem(key)) return;

    localStorage.setItem(key, 'true');
    toast.info('3秒延迟同步说明', '单曲整理默认在 3 秒本地倒计时后同步至云端。倒计时期间可随时按 Z 键或点击取消撤销该操作。', 6000);
  }

  /**
   * Render state placeholders for 8 distinct states + unknown_result
   * @param {string} stateType
   */
  renderStatePlaceholder(stateType) {
    if (!this.container) return;

    const wrapper = createElement('div', { className: 'deck-state-container' });

    if (stateType === 'unknown_result') {
      const detail = state.get('unknownResultDetail');
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'empty-state-title text-danger', text: '远端变更状态未确定' }),
          createElement('div', {
            className: 'empty-state-desc',
            text: `网络异常导致针对 "${detail?.song?.title || '歌曲'}" 的云端操作未收到确定响应。为保护曲库数据，破坏性操作已临时冻结。系统禁止盲目重试。`
          }),
          createElement('div', {
            style: 'display: flex; gap: var(--space-2); margin-top: var(--space-3);',
            children: [
              createElement('button', {
                className: 'btn btn-primary',
                text: '只读核对状态',
                events: {
                  click: async () => {
                    if (detail?.pendingOperation) {
                      await this.reconcileRecoveredOperation(detail.pendingOperation);
                    } else if (detail) {
                      await this.handleCommitFailureWithReconcile(
                        detail.song,
                        detail.action,
                        detail.playlistId,
                        detail.targetName,
                        detail.originalIndex,
                      );
                    }
                  }
                }
              }),
              createElement('button', {
                className: 'btn btn-secondary',
                text: '保留本地并解除冻结',
                events: {
                  click: () => this.dismissUnknownResultLocally()
                }
              })
            ]
          })
        ]
      }));
    } else if (stateType === 'loading') {
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'spinner', style: 'width: 32px; height: 32px;' }),
          createElement('div', { className: 'empty-state-title', text: '正在加载曲库...' }),
          createElement('div', { className: 'empty-state-desc', text: '正在从本地缓存或云端检索歌单曲目。' }),
        ]
      }));
    } else if (stateType === 'unauthenticated') {
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'empty-state-title', text: '请先连接网易云音乐账号' }),
          createElement('div', { className: 'empty-state-desc', text: '扫码登录后即可开始整理个人红心歌单及创建的歌单。' }),
          createElement('button', {
            className: 'btn btn-primary',
            text: '扫码登录',
            events: {
              click: () => modal.open('#login-modal')
            }
          })
        ]
      }));
    } else if (stateType === 'not_synced') {
      const selectedId = state.get('selectedPlaylistId');
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'empty-state-title', text: '当前歌单尚未同步到本地缓存' }),
          createElement('div', { className: 'empty-state-desc', text: '首次整理该歌单需要从网易云拉取一次曲目元数据。' }),
          createElement('button', {
            className: 'btn btn-primary',
            text: '立即同步此歌单',
            events: {
              click: async () => {
                if (selectedId) {
                  toast.info('正在同步', '拉取歌单曲目中...');
                  const res = await api.syncPlaylist(selectedId);
                  if (res.ok) {
                    const catRes = await api.getCatalog();
                    if (catRes.ok && catRes.data) {
                      const tracks = Array.isArray(catRes.data.tracks) ? catRes.data.tracks : catRes.data;
                      state.set({ catalog: tracks });
                      this.reloadDeck();
                    }
                  } else {
                    toast.error('同步失败', res.error?.message || '无法同步歌单。');
                  }
                }
              }
            }
          })
        ]
      }));
    } else if (stateType === 'true_empty') {
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'empty-state-title', text: '歌单暂无歌曲' }),
          createElement('div', { className: 'empty-state-desc', text: '当前所选歌单内没有任何曲目。' }),
        ]
      }));
    } else if (stateType === 'filter_empty') {
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'empty-state-title', text: '当前筛选条件下无匹配歌曲' }),
          createElement('div', { className: 'empty-state-desc', text: '可以尝试切换事实筛选器或查看全部歌曲。' }),
          createElement('button', {
            className: 'btn btn-secondary',
            text: '查看全部歌曲',
            events: {
              click: () => {
                state.set({ deckFilter: 'all' });
                const filterSelect = document.getElementById('deck-filter-select');
                if (filterSelect) filterSelect.value = 'all';
                this.reloadDeck();
              }
            }
          })
        ]
      }));
    } else if (stateType === 'complete') {
      const keptCount = (state.get('keptIds') || new Set()).size;
      const batchCount = (state.get('batchQueue') || []).length;
      const selectedId = state.get('selectedPlaylistId');
      const filter = state.get('deckFilter') || 'undecided';
      const order = state.get('deckOrder') || 'recent';

      wrapper.appendChild(createElement('div', {
        className: 'deck-finished-card animate-fade',
        children: [
          createElement('div', { className: 'finished-title', text: '本轮整理已完成' }),
          createElement('div', { className: 'finished-desc', text: '当前筛选条件下的所有歌曲均已处理完毕。' }),
          createElement('div', {
            className: 'deck-finished-stats',
            children: [
              createElement('div', {
                className: 'finished-stat-box',
                children: [
                  createElement('div', { className: 'finished-stat-val kept', text: String(keptCount) }),
                  createElement('div', { className: 'finished-stat-label', text: '保留歌曲' }),
                ]
              }),
              createElement('div', {
                className: 'finished-stat-box',
                children: [
                  createElement('div', { className: 'finished-stat-val pruned', text: String(batchCount) }),
                  createElement('div', { className: 'finished-stat-label', text: '待提交批处理' }),
                ]
              }),
            ]
          }),
          createElement('div', {
            style: 'display: flex; gap: var(--space-3); margin-top: var(--space-2);',
            children: [
              createElement('button', {
                className: 'btn btn-secondary',
                text: '重置进度并重新整理',
                events: {
                  click: () => {
                    state.resetReviewProgress(selectedId, filter, order);
                    this.reloadDeck();
                  }
                }
              }),
              createElement('button', {
                className: 'btn btn-primary',
                text: '查看云端队列',
                events: {
                  click: () => {
                    window.dispatchEvent(new CustomEvent('switch-tab', { detail: 'cloud' }));
                  }
                }
              }),
            ]
          })
        ]
      }));
    } else if (stateType === 'error') {
      wrapper.appendChild(createElement('div', {
        className: 'empty-state animate-fade',
        children: [
          createElement('div', { className: 'empty-state-title text-danger', text: '加载数据失败' }),
          createElement('div', { className: 'empty-state-desc', text: state.get('catalogError') || '无法连接本地服务。' }),
          createElement('button', {
            className: 'btn btn-secondary',
            text: '重新尝试',
            events: {
              click: () => this.reloadDeck()
            }
          })
        ]
      }));
    }

    this.container.appendChild(wrapper);
  }

  // --- Optional Drag Gesture Physics (Default Off) ---

  attachDragListeners(card) {
    let startX = 0;
    let currentX = 0;
    let dragging = false;

    const likeBadge = card.querySelector('.indicator-like');
    const dislikeBadge = card.querySelector('.indicator-dislike');

    const onPointerDown = (e) => {
      if (e.target.closest('button') || e.target.closest('.badge')) return;
      dragging = true;
      startX = e.clientX || (e.touches && e.touches[0].clientX) || 0;
      card.classList.add('is-dragging');

      document.addEventListener('pointermove', onPointerMove);
      document.addEventListener('pointerup', onPointerUp);
      document.addEventListener('pointercancel', onPointerUp);
    };

    const onPointerMove = (e) => {
      if (!dragging) return;
      const clientX = e.clientX || (e.touches && e.touches[0].clientX) || 0;
      currentX = clientX - startX;

      const rotateDeg = (currentX / 25);
      card.style.transform = `translate(${currentX}px, 0) rotate(${rotateDeg}deg)`;

      if (currentX > 0) {
        const opacity = Math.min(1, currentX / this.dragThreshold);
        if (likeBadge) likeBadge.style.opacity = String(opacity);
        if (dislikeBadge) dislikeBadge.style.opacity = '0';
      } else {
        const opacity = Math.min(1, Math.abs(currentX) / this.dragThreshold);
        if (dislikeBadge) dislikeBadge.style.opacity = String(opacity);
        if (likeBadge) likeBadge.style.opacity = '0';
      }
    };

    const onPointerUp = () => {
      if (!dragging) return;
      dragging = false;
      card.classList.remove('is-dragging');

      document.removeEventListener('pointermove', onPointerMove);
      document.removeEventListener('pointerup', onPointerUp);
      document.removeEventListener('pointercancel', onPointerUp);

      if (currentX > this.dragThreshold) {
        this.handleKeepAction();
      } else if (currentX < -this.dragThreshold) {
        this.handleDestructiveAction();
      } else {
        card.style.transform = 'translate(0, 0) rotate(0deg)';
        if (likeBadge) likeBadge.style.opacity = '0';
        if (dislikeBadge) dislikeBadge.style.opacity = '0';
      }
    };

    card.addEventListener('pointerdown', onPointerDown);
  }
}

export const deckManager = new DeckReviewEngine();
