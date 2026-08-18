/**
 * Pruner Studio - Cloud Operations & Grouped Batch Management
 * Handles playlist synchronization, grouped batch action confirmation,
 * automatic snapshot backup, and 100-record operation history logging.
 */
import { api } from './api.js';
import { state } from './state.js';
import { toast } from './toast.js';
import { modal } from './modal.js';
import { createElement, formatArtists, formatDuration } from './utils.js';

class CloudOperationsManager {
  constructor() {
    this.container = null;
    this.pendingOperations = new Set();
  }

  init(containerId) {
    this.container = document.getElementById(containerId);
    this.initAccordion();
  }

  initAccordion() {
    const toggleBtn = document.getElementById('cloud-more-tools-toggle');
    const contentPanel = document.getElementById('cloud-more-tools-panel');

    if (toggleBtn && contentPanel) {
      toggleBtn.addEventListener('click', () => {
        const isOpen = contentPanel.classList.toggle('open');
        toggleBtn.classList.toggle('expanded', isOpen);
      });
    }
  }

  /**
   * Fetch playlists list from backend and render.
   */
  async loadPlaylists() {
    try {
      const res = await api.getPlaylists();
      if (res.ok && res.data) {
        const playlists = Array.isArray(res.data.playlists) ? res.data.playlists : (Array.isArray(res.data) ? res.data : []);
        const canonicalId = state.resolveCanonicalPlaylistId(playlists);
        state.set({ playlists, selectedPlaylistId: canonicalId });
        this.renderPlaylists(playlists);
        this.populatePlaylistDropdowns(playlists, canonicalId);
      }
    } catch (err) {
      console.warn('Failed to fetch playlists:', err);
    }
  }

  populatePlaylistDropdowns(playlists, selectedDeckId = state.get('selectedPlaylistId')) {
    const deckSelect = document.getElementById('deck-playlist-select');
    const catalogSelect = document.getElementById('catalog-playlist-filter');

    const updateSelect = (selectEl, includeAll = false, desiredValue = null) => {
      if (!selectEl) return;
      selectEl.textContent = ''; // Safe clear

      if (includeAll) {
        selectEl.appendChild(createElement('option', { text: '全部歌单', attributes: { value: 'all' } }));
      }

      playlists.forEach(pl => {
        const kindLabel = pl.kind === 'liked' ? ' (我喜欢的音乐)' : '';
        const opt = createElement('option', {
          text: `${pl.name}${kindLabel} (${pl.track_count || 0})`,
          attributes: { value: String(pl.id) }
        });
        selectEl.appendChild(opt);
      });

      const normalizedDesired = desiredValue !== null && desiredValue !== undefined ? String(desiredValue) : '';
      if (normalizedDesired && Array.from(selectEl.options).some(o => o.value === normalizedDesired)) {
        selectEl.value = normalizedDesired;
      }
    };

    updateSelect(deckSelect, false, selectedDeckId);
    updateSelect(catalogSelect, true, state.get('catalogSelectedPlaylistId') || 'all');
  }

  renderPlaylists(playlists) {
    const container = document.getElementById('cloud-playlists-grid');
    if (!container) return;
    container.textContent = ''; // Safe clear

    if (playlists.length === 0) {
      container.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: '暂无云端歌单' }),
          createElement('div', { className: 'empty-state-desc', text: '登录网易云音乐账号后将自动拉取红心与自建歌单。' }),
        ]
      }));
      return;
    }

    playlists.forEach(pl => {
      const syncBtn = createElement('button', {
        className: 'btn btn-secondary btn-sm',
        text: '同步',
        events: {
          click: () => this.syncPlaylist(pl.id, pl.name)
        }
      });

      const kindText = pl.kind === 'liked' ? '喜欢的音乐' : (pl.kind === 'self_created' ? '自建歌单' : '收藏歌单');

      const card = createElement('div', {
        className: 'playlist-card-item',
        children: [
          createElement('div', {
            className: 'playlist-info',
            children: [
              createElement('div', { className: 'playlist-name', text: pl.name }),
              createElement('div', {
                className: 'playlist-meta',
                children: [
                  createElement('span', { text: `${pl.track_count || 0} 首曲目` }),
                  createElement('span', { className: 'badge badge-muted', text: kindText }),
                ]
              })
            ]
          }),
          syncBtn
        ]
      });

      container.appendChild(card);
    });
  }

  async syncPlaylist(playlistId, playlistName = '') {
    const operationKey = `sync:${playlistId}`;
    if (this.pendingOperations.has(operationKey)) return;
    this.pendingOperations.add(operationKey);
    toast.info('正在同步歌单', `正在拉取 "${playlistName || playlistId}" 的最新曲目...`);

    try {
      const res = await api.syncPlaylist(playlistId);
      if (res.ok) {
        state.clearIgnoredUnknownOperationsForPlaylist(playlistId);
        toast.success('同步完成', `已成功同步 ${res.data?.synced_count ?? ''} 首曲目。`);
        await this.reloadCatalog();
      } else {
        toast.error('同步失败', res.error?.message || '未能同步歌单。');
      }
    } catch (err) {
      toast.error('网络同步错误', err.message);
    } finally {
      this.pendingOperations.delete(operationKey);
    }
  }

  async reloadCatalog() {
    try {
      const res = await api.getCatalog();
      if (res.ok && res.data) {
        const tracks = Array.isArray(res.data.tracks) ? res.data.tracks : (Array.isArray(res.data) ? res.data : []);
        state.set({ catalog: tracks });
        state.emit('catalog:loaded', tracks);
      }
    } catch (err) {
      console.warn('Failed to reload catalog:', err);
    }
  }

  /**
   * Render Grouped Pending Batch Actions
   */
  renderGroupedBatchQueue() {
    const container = document.getElementById('cloud-batch-groups-container');
    const queueCountBadge = document.getElementById('cloud-batch-total-count');
    const executeBtn = document.getElementById('cloud-open-batch-confirm-btn');

    const queue = state.get('batchQueue') || [];

    if (queueCountBadge) {
      queueCountBadge.textContent = `${queue.length} 首待处理`;
    }

    if (executeBtn) {
      executeBtn.disabled = queue.length === 0;
    }

    if (!container) return;
    container.textContent = ''; // Safe clear

    if (queue.length === 0) {
      container.appendChild(createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: '暂无待执行的云端变更' }),
          createElement('div', { className: 'empty-state-desc', text: '在“整理”或“曲库”中选择移除的歌曲将暂存在此，经二次确认后批量同步。' }),
        ]
      }));
      return;
    }

    // Group by target
    const groups = new Map();
    queue.forEach(item => {
      const key = `${item.action}:${item.playlistId || 'liked'}`;
      if (!groups.has(key)) {
        groups.set(key, {
          action: item.action,
          playlistId: item.playlistId,
          targetName: item.targetName || (item.action === 'unlike' ? '喜欢的音乐' : `歌单 #${item.playlistId}`),
          items: [],
        });
      }
      groups.get(key).items.push(item);
    });

    groups.forEach((group) => {
      const groupHeader = createElement('div', {
        className: 'batch-group-header',
        children: [
          createElement('div', {
            className: 'batch-group-title',
            children: [
              createElement('strong', { text: group.targetName }),
              createElement('span', {
                className: 'badge badge-danger',
                text: `${group.action === 'unlike' ? '取消喜欢' : '从歌单移出'} · ${group.items.length} 首`
              }),
            ]
          }),
          createElement('button', {
            className: 'btn btn-ghost btn-sm text-danger',
            text: '清空本组',
            events: {
              click: () => {
                group.items.forEach(it => state.unstageBatchAction(it.song.id, it.playlistId));
                this.renderGroupedBatchQueue();
              }
            }
          })
        ]
      });

      const itemsList = createElement('div', { className: 'batch-group-items-list' });
      group.items.forEach(it => {
        const removeBtn = createElement('button', {
          className: 'btn btn-ghost btn-sm',
          text: '×',
          attributes: { title: '从待处理队列移出' },
          events: {
            click: () => {
              state.unstageBatchAction(it.song.id, it.playlistId);
              this.renderGroupedBatchQueue();
            }
          }
        });

        const row = createElement('div', {
          className: 'batch-item-row',
          children: [
            createElement('div', {
              children: [
                createElement('span', { className: 'batch-song-title', text: it.song.title || '未知曲目' }),
                createElement('span', { className: 'batch-song-artist', text: ` - ${formatArtists(it.song.artists)}` }),
              ]
            }),
            createElement('div', {
              style: 'display: flex; align-items: center; gap: 8px;',
              children: [
                ...(it.status === 'unknown'
                  ? [createElement('span', { className: 'badge badge-warning', text: '待只读核对' })]
                  : []),
                createElement('span', { className: 'batch-song-duration', text: formatDuration(it.song.duration) }),
                removeBtn
              ]
            })
          ]
        });

        itemsList.appendChild(row);
      });

      const groupCard = createElement('div', {
        className: 'batch-group-card',
        children: [groupHeader, itemsList]
      });

      container.appendChild(groupCard);
    });
  }

  /**
   * Open the Grouped Batch Confirmation Modal
   */
  openBatchConfirmationModal() {
    const queue = state.get('batchQueue') || [];
    if (queue.length === 0) {
      toast.warning('待处理队列为空', '当前没有需要执行的云端变更。');
      return;
    }

    const summaryContainer = document.getElementById('batch-confirm-summary-list');
    if (summaryContainer) {
      summaryContainer.textContent = ''; // Safe clear

      // Group by target for clean confirmation summary
      const groups = new Map();
      queue.forEach(item => {
        const key = `${item.action}:${item.playlistId || 'liked'}`;
        if (!groups.has(key)) {
          groups.set(key, {
            action: item.action,
            targetName: item.targetName || (item.action === 'unlike' ? '喜欢的音乐' : `歌单 #${item.playlistId}`),
            count: 0,
          });
        }
        groups.get(key).count++;
      });

      groups.forEach(g => {
        const actionLabel = g.action === 'unlike' ? '取消喜欢' : `从 ${g.targetName} 移出`;
        const row = createElement('div', {
          className: 'batch-confirm-row',
          children: [
            createElement('span', { text: actionLabel }),
            createElement('strong', { text: `${g.count} 首` }),
          ]
        });
        summaryContainer.appendChild(row);
      });
    }

    modal.open('#batch-confirm-modal');
  }

  /**
   * Execute Grouped Batch Actions with Automatic Snapshot Backup
   */
  async executeConfirmedBatch() {
    if (this.pendingOperations.has('batch_execute')) return;
    if (state.get('destructiveFrozen') || state.get('hasUnknownResult')) {
      toast.warning('批量操作已冻结', '请先处理当前未确定的云端结果；本次没有发送任何写请求。');
      return;
    }
    const queue = [...(state.get('batchQueue') || [])];
    if (queue.length === 0) return;

    this.pendingOperations.add('batch_execute');
    modal.close('#batch-confirm-modal');
    try {
      const groups = new Map();
      queue.forEach(item => {
        const key = `${item.action}:${item.playlistId || 'liked'}`;
        if (!groups.has(key)) {
          groups.set(key, {
            action: item.action,
            playlistId: item.playlistId,
            targetName: item.targetName || (item.action === 'unlike' ? '喜欢的音乐' : `歌单 #${item.playlistId}`),
            items: [],
          });
        }
        groups.get(key).items.push(item);
      });

      const hasNewWrites = queue.some(item => item.status !== 'unknown');
      if (hasNewWrites) {
        toast.info('自动快照中', '执行批量变更前正在创建本地备份快照...');
        let backupResult;
        try {
          backupResult = await api.backup({ reason: 'auto_snapshot_before_batch_execute' });
        } catch {
          backupResult = { ok: false };
        }
        if (!backupResult.ok) {
          toast.error('自动快照失败', '批量变更未执行，待处理队列保持不变。');
          return;
        }
      }

      let totalSuccess = 0;
      let totalUnresolved = 0;

      for (const group of groups.values()) {
        const writableIds = group.items.filter(item => item.status !== 'unknown').map(item => item.song.id);
        let unknownIds = group.items.filter(item => item.status === 'unknown').map(item => String(item.song.id));
        const successful = new Set();
        const definiteFailed = new Set();

        if (writableIds.length > 0) {
          toast.info('正在提交变更', `正在处理 "${group.targetName}" (${writableIds.length} 首)...`);
          let result;
          try {
            result = group.action === 'unlike'
              ? await api.unlike(writableIds, group.playlistId)
              : await api.removeTracks(group.playlistId, writableIds, group.playlistId);
          } catch {
            result = { ok: false, data: null };
          }

          if (result.ok && result.data) {
            const resultSuccess = Array.isArray(result.data.successful_ids) ? result.data.successful_ids.map(String) : [];
            resultSuccess.forEach(id => successful.add(id));

            const failedItems = Array.isArray(result.data.failed_items) ? result.data.failed_items : [];
            failedItems.forEach(item => {
              const id = String(item.id);
              if (String(item.code || '').endsWith('_RESULT_UNKNOWN')) unknownIds.push(id);
              else definiteFailed.add(id);
            });

            const unknownItems = Array.isArray(result.data.unknown_items) ? result.data.unknown_items : [];
            unknownItems.forEach(item => unknownIds.push(String(item.id)));

            writableIds.map(String).forEach(id => {
              if (!successful.has(id) && !definiteFailed.has(id) && !unknownIds.includes(id)) unknownIds.push(id);
            });
          } else {
            unknownIds.push(...writableIds.map(String));
          }
        }

        unknownIds = [...new Set(unknownIds)].filter(id => !successful.has(id));
        let stillUnknown = [...unknownIds];
        if (unknownIds.length > 0) {
          let reconcileResult;
          try {
            reconcileResult = await api.reconcileOperations(
              group.action,
              group.playlistId,
              unknownIds,
              group.playlistId,
            );
          } catch {
            reconcileResult = { ok: false, data: null };
          }
          if (reconcileResult.ok && reconcileResult.data) {
            const reconciledSuccess = Array.isArray(reconcileResult.data.successful_ids)
              ? reconcileResult.data.successful_ids.map(String)
              : [];
            reconciledSuccess.forEach(id => successful.add(id));
            const reconciledFailed = Array.isArray(reconcileResult.data.failed_items)
              ? reconcileResult.data.failed_items.map(item => String(item.id))
              : [];
            reconciledFailed.forEach(id => definiteFailed.add(id));
            const reconciledUnknown = Array.isArray(reconcileResult.data.unknown_items)
              ? reconcileResult.data.unknown_items.map(item => String(item.id))
              : [];
            stillUnknown = unknownIds.filter(id => !successful.has(id) && !definiteFailed.has(id));
            stillUnknown.push(...reconciledUnknown);
            stillUnknown = [...new Set(stillUnknown)];
          }
        }

        const successfulIds = [...successful];
        if (successfulIds.length > 0) {
          state.removeConfirmedSongs(successfulIds, group.playlistId, group.action);
          totalSuccess += successfulIds.length;
        }

        const successfulSet = new Set(successfulIds);
        const unknownSet = new Set(stillUnknown);
        const nextQueue = (state.get('batchQueue') || []).map(item => {
          const sameGroup = item.action === group.action && String(item.playlistId) === String(group.playlistId);
          const id = String(item.song.id);
          if (!sameGroup || successfulSet.has(id)) return item;
          if (unknownSet.has(id)) return { ...item, status: 'unknown' };
          if (item.status === 'unknown') {
            const { status, ...rest } = item;
            return rest;
          }
          return item;
        });
        state.saveBatchQueue(nextQueue.filter(item => !(
          item.action === group.action
          && String(item.playlistId) === String(group.playlistId)
          && successfulSet.has(String(item.song.id))
        )));

        const unresolvedCount = group.items.length - successfulIds.length;
        totalUnresolved += unresolvedCount;
        state.addOperationLog({
          type: group.action === 'unlike' ? '批量取消喜欢' : '批量移出歌单',
          requested: group.items.length,
          successful: successfulIds.length,
          failed: unresolvedCount,
          targetName: group.targetName,
          message: `处理 ${group.targetName}: 成功 ${successfulIds.length}, 保留 ${unresolvedCount}`,
        });
      }

      if (totalUnresolved === 0) {
        toast.success('批量同步完成', `成功处理全部 ${totalSuccess} 首歌曲变更。`);
      } else {
        toast.warning('部分变更完成', `成功同步 ${totalSuccess} 首，${totalUnresolved} 首失败或待核对，已保留在队列中。`);
      }
    } finally {
      this.pendingOperations.delete('batch_execute');
      this.renderGroupedBatchQueue();
      this.renderLogs();
      await this.reloadCatalog();
    }
  }

  renderLogs() {
    const tbody = document.getElementById('cloud-ops-log-body');
    if (!tbody) return;
    tbody.textContent = ''; // Safe clear

    const opsHistory = state.get('opsHistory') || [];

    if (opsHistory.length === 0) {
      tbody.appendChild(createElement('tr', {
        children: [createElement('td', { attributes: { colspan: '5', style: 'text-align: center; color: var(--text-muted);' }, text: '暂无操作记录。' })]
      }));
      return;
    }

    opsHistory.slice(0, 100).forEach(log => {
      const row = createElement('tr', {
        children: [
          createElement('td', { text: log.timestamp || '-' }),
          createElement('td', { text: log.type || '-' }),
          createElement('td', { text: log.targetName || '默认目标' }),
          createElement('td', { text: `${log.successful || 0} / ${log.requested || 0}` }),
          createElement('td', {
            children: [
              createElement('span', {
                className: `badge ${(log.failed || 0) === 0 ? 'badge-success' : 'badge-warning'}`,
                text: (log.failed || 0) === 0 ? '成功' : `部分失败 (${log.failed})`
              })
            ]
          })
        ]
      });
      tbody.appendChild(row);
    });
  }

  async triggerBackup() {
    if (this.pendingOperations.has('backup')) return;
    this.pendingOperations.add('backup');
    toast.info('正在创建备份', '正在生成服务端快照并准备下载本地文件...');

    try {
      // 1. Server snapshot
      const res = await api.backup({ format: 'json' });
      if (!res.ok) {
        toast.error('备份失败', res.error?.message || '服务端创建快照失败。');
        return;
      }

      // 2. Fetch full catalog content to generate client-side downloadable UTF-8 JSON Blob
      let catalogPayload = { tracks: state.get('catalog') || [] };
      try {
        const catRes = await api.getCatalog();
        if (catRes.ok && catRes.data) {
          catalogPayload = catRes.data;
        }
      } catch {}

      // 3. Trigger Browser Download of UTF-8 JSON Blob
      if (typeof Blob !== 'undefined' && typeof document !== 'undefined') {
        const jsonStr = JSON.stringify(catalogPayload, null, 2);
        const blob = new Blob([jsonStr], { type: 'application/json;charset=utf-8;' });
        const downloadUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        const dateStr = new Date().toISOString().slice(0, 10);
        a.href = downloadUrl;
        a.download = `pruner-studio-backup-${dateStr}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(downloadUrl);
      }

      toast.success('备份已完成并开始下载', '服务端快照已落盘，已触发本地 JSON 文件下载。');
      state.addOperationLog({
        type: '本地备份',
        requested: 1,
        successful: 1,
        failed: 0,
        targetName: '全曲库快照',
        message: '创建服务端快照并触发客户端 JSON 下载',
      });
      this.renderLogs();
    } catch (err) {
      toast.error('备份异常', err.message);
    } finally {
      this.pendingOperations.delete('backup');
    }
  }
}

export const cloudManager = new CloudOperationsManager();
