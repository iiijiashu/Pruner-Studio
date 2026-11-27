/**
 * Pruner Studio - Unified Right-Click Song Context Menu
 * Mounts into `#context-menu-host` with viewport clamping, full keyboard navigation
 * (ArrowUp/ArrowDown/Home/End/Enter/Space/Escape), and click-outside dismissal.
 */
import { api } from './api.js';
import { state } from './state.js';
import { player } from './player.js';
import { toast } from './toast.js';
import { getTrackKey, writableNeteaseTrack, createElement, formatArtists } from './utils.js';
import { createIcon } from './icons.js';

export function quickNextButton(song) {
  return createElement('button', {
    className: 'table-btn quick-next', attributes: { type: 'button', title: '下一首', 'aria-label': `下一首：${song.title || '歌曲'}` },
    events: { click: event => { event.stopPropagation(); player.enqueueNext(song); } },
    children: [createIcon('skip-next', { size: 15 })],
  });
}

let activeMenuState = null;
let defaultHandlers = {
  onAddToPlaylist: null,
  onNotInterested: null,
};

export function initSongContextMenu({ onAddToPlaylist = null, onNotInterested = null } = {}) {
  if (typeof onAddToPlaylist === 'function') defaultHandlers.onAddToPlaylist = onAddToPlaylist;
  if (typeof onNotInterested === 'function') defaultHandlers.onNotInterested = onNotInterested;
}

async function copySongTitleArtist(song) {
  const text = `${song?.title || '未知曲目'} - ${formatArtists(song?.artists)}`;
  let copied = false;
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      await navigator.clipboard.writeText(text);
      copied = true;
    }
  } catch {}

  if (!copied && typeof document !== 'undefined' && typeof document.createElement === 'function') {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', 'readonly');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body?.appendChild(ta);
      ta.select?.();
      copied = Boolean(document.execCommand?.('copy'));
      ta.parentNode?.removeChild(ta);
    } catch {}
  }

  const notify = typeof toast.quiet === 'function'
    ? (msg) => toast.quiet(msg)
    : (msg) => toast.info('已复制', msg);
  if (copied) {
    notify(`已复制：${text}`);
  } else {
    toast.warning('复制失败', '当前环境不支持剪贴板写入。');
  }
}

export function buildSongContextMenuItems(song, { source = 'generic', origin = '', onAddToPlaylist = null, onNotInterested = null, queue = null, index = 0 } = {}) {
  if (!song || typeof song !== 'object') return [];
  const hasNeteaseId = writableNeteaseTrack(song);
  const trackKey = getTrackKey(song);
  const isLiked = hasNeteaseId ? state.isSongLiked(song.id) : state.isSongLiked(song);
  const isOffline = Boolean(state.get('offlineMode'));
  const queueOrigin = origin || (source === 'search' ? '搜索结果' : '播放队列');

  const items = [
    {
      id: 'play',
      label: '播放',
      icon: 'play',
      disabled: false,
      action: () => {
        if (source === 'queue') {
          const live = state.get('playQueue') || [];
          const row = live[index];
          const position = row && (row === song || getTrackKey(row) === getTrackKey(song)) ? index : live.indexOf(song);
          if (position >= 0) {
            state.set({playQueueIndex: position});
            player.playSong(song, true, {preserveQueue: true});
          }
        } else if (Array.isArray(queue) && queue.length > 0) {
          player.playQueue(queue, Math.max(0, index), 'order', queueOrigin);
        } else {
          player.playSong(song, true, { origin: queueOrigin });
        }
      },
    },
    {
      id: 'enqueue_next',
      label: '下一首播放',
      icon: 'queue',
      disabled: false,
      action: () => player.enqueueNext(song),
    },
    { type: 'separator' },
    {
      id: 'add_to_playlist',
      label: '加入歌单',
      icon: 'plus-list',
      disabled: !hasNeteaseId || isOffline,
      action: () => {
        const handler = onAddToPlaylist || defaultHandlers.onAddToPlaylist;
        if (typeof handler === 'function') {
          handler(song);
        } else if (typeof window !== 'undefined' && typeof CustomEvent !== 'undefined') {
          window.dispatchEvent(new CustomEvent('pruner:add-to-playlist', { detail: { song } }));
        }
      },
    },
    {
      id: 'toggle_like',
      label: isLiked ? '取消喜欢' : '喜欢',
      icon: isLiked ? 'heart-filled' : 'heart',
      disabled: hasNeteaseId ? isOffline : !trackKey,
      action: () => player.toggleLikeCurrentSong(song),
    },
  ];

  if (source === 'discover') {
    items.push({
      id: 'not_interested',
      label: '不再推荐',
      icon: 'ban',
      danger: true,
      disabled: false,
      action: async () => {
        const handler = onNotInterested || defaultHandlers.onNotInterested;
        if (typeof handler === 'function') {
          await handler(song);
          return;
        }
        if (hasNeteaseId && state.get('authenticated') && !isOffline) {
          await api.submitFeedback(song.id, 'not_interested', 'discover').catch(() => {});
        }
        toast.info('已标记不再推荐', `"${song.title || '曲目'}" 将减少出现在推荐中。`);
        if (typeof window !== 'undefined' && typeof CustomEvent !== 'undefined') {
          window.dispatchEvent(new CustomEvent('pruner:not-interested', { detail: { song } }));
        }
      },
    });
  }

  items.push(
    {
      id: 'similar',
      label: '查看相似',
      icon: 'sparkles',
      disabled: !hasNeteaseId,
      action: () => player.openSimilarSongsModal(song),
    },
    { type: 'separator' },
    {
      id: 'copy',
      label: '复制歌名-歌手',
      icon: 'copy',
      disabled: false,
      action: () => copySongTitleArtist(song),
    },
  );

  return items;
}

export function closeSongContextMenu({ restoreFocus = false } = {}) {
  if (!activeMenuState) return;
  const { host, menu, cleanup, previouslyFocused } = activeMenuState;
  activeMenuState = null;

  if (typeof cleanup === 'function') cleanup();
  if (host) {
    host.textContent = '';
    host.classList?.remove('open');
  } else if (menu?.parentNode) {
    menu.parentNode.removeChild(menu);
  }
  if (restoreFocus && previouslyFocused && typeof previouslyFocused.focus === 'function') {
    try {
      previouslyFocused.focus();
    } catch {}
  }
}

export function openSongContextMenu(event, song, options = {}) {
  if (event && typeof event.preventDefault === 'function') event.preventDefault();
  if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
  if (!song || typeof document === 'undefined') return null;

  closeSongContextMenu({ restoreFocus: false });

  const opts = options && typeof options === 'object' ? options : {};
  const source = opts.source || 'generic';
  const items = buildSongContextMenuItems(song, { ...opts, source });
  if (items.length === 0) return null;

  const host = document.getElementById('context-menu-host') || document.body;
  if (!host) return null;
  if (host.id === 'context-menu-host') {
    host.classList?.add('context-menu-host', 'open');
  }
  host.textContent = '';

  const previouslyFocused = document.activeElement || null;
  const menuButtons = [];
  let activeButtonIndex = -1;

  const setActiveIndex = (nextIdx) => {
    if (menuButtons.length === 0) return;
    const clamped = ((nextIdx % menuButtons.length) + menuButtons.length) % menuButtons.length;
    activeButtonIndex = clamped;
    menuButtons.forEach((btn, i) => btn.classList?.toggle('is-active', i === clamped));
    menuButtons[clamped]?.focus?.();
  };

  const menu = createElement('div', {
    className: 'context-menu',
    attributes: {
      role: 'menu',
      tabindex: '-1',
      'aria-label': `${song.title || '歌曲'} 操作菜单`,
      'data-source': source,
    },
  });

  items.forEach((item) => {
    if (item.type === 'separator') {
      menu.appendChild(createElement('div', {
        className: 'context-menu-sep',
        attributes: { role: 'separator' },
      }));
      return;
    }

    const btn = createElement('button', {
      className: `context-menu-item${item.danger ? ' is-danger' : ''}`,
      attributes: {
        type: 'button',
        role: 'menuitem',
        tabindex: item.disabled ? '-1' : '0',
        'data-menu-id': item.id,
        ...(item.disabled ? { disabled: 'disabled', 'aria-disabled': 'true' } : {}),
      },
      children: [
        createIcon(item.icon, { size: 14 }),
        createElement('span', { text: item.label }),
      ],
      events: {
        click: (e) => {
          e.stopPropagation();
          if (item.disabled) return;
          closeSongContextMenu({ restoreFocus: false });
          Promise.resolve(item.action()).catch(() => {});
        },
        mouseenter: () => {
          if (item.disabled) return;
          const idx = menuButtons.indexOf(btn);
          if (idx >= 0) {
            activeButtonIndex = idx;
            menuButtons.forEach((b, i) => b.classList?.toggle('is-active', i === idx));
          }
        },
      },
    });
    if (!item.disabled) {
      menuButtons.push(btn);
    }
    menu.appendChild(btn);
  });

  host.appendChild(menu);

  // Determine anchor & clamp strictly inside viewport
  let anchorX = Number(event?.clientX);
  let anchorY = Number(event?.clientY);
  if (!Number.isFinite(anchorX) || !Number.isFinite(anchorY) || (anchorX === 0 && anchorY === 0)) {
    const targetRect = event?.currentTarget?.getBoundingClientRect?.()
      || event?.target?.getBoundingClientRect?.();
    if (targetRect) {
      anchorX = targetRect.left + Math.min(24, (targetRect.width || 0) / 2);
      anchorY = targetRect.bottom + 4;
    } else {
      anchorX = 16;
      anchorY = 16;
    }
  }

  const vw = (typeof window !== 'undefined' && Number(window.innerWidth)) || 1280;
  const vh = (typeof window !== 'undefined' && Number(window.innerHeight)) || 800;
  const menuRect = typeof menu.getBoundingClientRect === 'function'
    ? menu.getBoundingClientRect()
    : { width: 204, height: 270 };
  const menuW = Number(menuRect?.width) || 204;
  const menuH = Number(menuRect?.height) || 270;
  const left = Math.max(8, Math.min(anchorX, Math.max(8, vw - menuW - 8)));
  const top = Math.max(8, Math.min(anchorY, Math.max(8, vh - menuH - 8)));

  if (menu.style) {
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(top)}px`;
  }

  const onKeyDown = (e) => {
    if (!activeMenuState) return;
    if (e.key === 'Escape' && !e.repeat && !e.isComposing) {
      e.preventDefault?.();
      e.stopImmediatePropagation?.();
      e.stopPropagation?.();
      closeSongContextMenu({ restoreFocus: true });
      return;
    }
    if (e.key === 'Tab') {
      closeSongContextMenu({ restoreFocus: false });
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault?.();
      setActiveIndex(activeButtonIndex < 0 ? 0 : activeButtonIndex + 1);
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault?.();
      setActiveIndex(activeButtonIndex < 0 ? menuButtons.length - 1 : activeButtonIndex - 1);
      return;
    }
    if (e.key === 'Home') {
      e.preventDefault?.();
      setActiveIndex(0);
      return;
    }
    if (e.key === 'End') {
      e.preventDefault?.();
      setActiveIndex(menuButtons.length - 1);
      return;
    }
    if (e.key === 'Enter' || e.key === ' ') {
      const targetBtn = activeButtonIndex >= 0
        ? menuButtons[activeButtonIndex]
        : (menuButtons.includes(document.activeElement) ? document.activeElement : null);
      if (targetBtn) {
        e.preventDefault?.();
        targetBtn.click?.();
      }
    }
  };

  const onOutsidePointer = (e) => {
    if (!activeMenuState) return;
    if (menu.contains && e?.target && menu.contains(e.target)) return;
    closeSongContextMenu({ restoreFocus: false });
  };

  const onDismissEvent = () => closeSongContextMenu({ restoreFocus: false });

  document.addEventListener?.('keydown', onKeyDown, true);
  document.addEventListener?.('mousedown', onOutsidePointer, true);
  document.addEventListener?.('click', onOutsidePointer, true);
  window?.addEventListener?.('resize', onDismissEvent);
  window?.addEventListener?.('blur', onDismissEvent);
  window?.addEventListener?.('scroll', onDismissEvent, true);

  activeMenuState = {
    host: host.id === 'context-menu-host' ? host : null,
    menu,
    previouslyFocused,
    cleanup: () => {
      document.removeEventListener?.('keydown', onKeyDown, true);
      document.removeEventListener?.('mousedown', onOutsidePointer, true);
      document.removeEventListener?.('click', onOutsidePointer, true);
      window?.removeEventListener?.('resize', onDismissEvent);
      window?.removeEventListener?.('blur', onDismissEvent);
      window?.removeEventListener?.('scroll', onDismissEvent, true);
    },
  };

  if (menuButtons.length > 0) {
    setActiveIndex(0);
  } else {
    menu.focus?.();
  }

  return menu;
}
