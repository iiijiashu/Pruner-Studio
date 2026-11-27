/**
 * Pruner Studio - Toast Notifications
 * Quiet Bone/Ash alerts with crisp 1.5px SVG icons, zero emojis,
 * max 3 visible cap (evicting oldest), 2s dedupe on title+description,
 * and toast.quiet(title) (1.2s, max 1 stacked).
 */
import { createElement } from './utils.js';
import { createIcon } from './icons.js';

const MAX_VISIBLE_TOASTS = 3;
const DEDUPE_WINDOW_MS = 2000;

class ToastManager {
  constructor() {
    this.container = null;
    this.activeToasts = []; // Array<{ el, isQuiet, remove }>
    this.recentShown = new Map(); // key -> timestamp
  }

  ensureContainer() {
    if (typeof document === 'undefined') return null;

    if (!this.container || (document.body && !document.body.contains(this.container))) {
      let existing = document.getElementById('toast-container');
      if (!existing) {
        existing = createElement('div', {
          className: 'toast-container',
          attributes: { id: 'toast-container', 'aria-live': 'polite' },
        });
        if (document.body) {
          document.body.appendChild(existing);
        }
      }
      this.container = existing;
    }
    return this.container;
  }

  evictToastRecord(record, immediate = false) {
    if (!record) return;
    this.activeToasts = this.activeToasts.filter(item => item !== record);
    if (record.timerId) {
      clearTimeout(record.timerId);
      record.timerId = null;
    }
    const el = record.el;
    if (!el) return;
    if (immediate) {
      if (el.parentNode) el.parentNode.removeChild(el);
      return;
    }
    if (el.classList?.contains('toast-hiding')) return;
    el.classList?.add('toast-hiding');
    setTimeout(() => {
      if (el.parentNode) {
        el.parentNode.removeChild(el);
      }
    }, 150);
  }

  /**
   * Show a toast message with 2s deduplication and max-3 visible cap.
   * @param {'info'|'success'|'warning'|'error'} type
   * @param {string} title
   * @param {string} [description]
   * @param {number} [duration=3500]
   * @param {{ isQuiet?: boolean }} [opts]
   */
  show(type = 'info', title = '', description = '', duration = 3500, opts = {}) {
    const container = this.ensureContainer();
    if (!container) return null;

    const isQuiet = Boolean(opts?.isQuiet);
    const now = Date.now();
    const dedupeKey = `${type}\0${String(title || '')}\0${String(description || '')}`;

    // Prune expired dedupe keys
    for (const [k, ts] of this.recentShown.entries()) {
      if (now - ts >= DEDUPE_WINDOW_MS) this.recentShown.delete(k);
    }

    if (!isQuiet) {
      const prevTs = this.recentShown.get(dedupeKey);
      if (prevTs !== undefined && now - prevTs < DEDUPE_WINDOW_MS) {
        return null;
      }
      this.recentShown.set(dedupeKey, now);
    } else {
      // Max 1 quiet toast stacked at a time
      const existingQuiet = this.activeToasts.filter(item => item.isQuiet);
      existingQuiet.forEach(rec => this.evictToastRecord(rec, true));
    }

    // Enforce max 3 visible toasts (evict oldest first)
    while (this.activeToasts.length >= MAX_VISIBLE_TOASTS) {
      const oldest = this.activeToasts[0];
      this.evictToastRecord(oldest, true);
    }

    const iconMap = {
      success: 'check-circle',
      error: 'x-circle',
      warning: 'alert-circle',
      info: 'info',
    };

    const iconWrap = createElement('span', {
      className: `toast-icon toast-icon-${type}`,
    }, [createIcon(iconMap[type] || 'info', { size: 15 })]);

    const contentEl = createElement('div', { className: 'toast-content' });
    const titleEl = createElement('div', { className: 'toast-title', text: title });
    contentEl.appendChild(titleEl);

    if (description) {
      const descEl = createElement('div', { className: 'toast-desc', text: description });
      contentEl.appendChild(descEl);
    }

    const toastEl = createElement('div', {
      className: `toast toast-${type}${isQuiet ? ' toast-quiet' : ''}`,
      attributes: { role: 'status' },
    }, [iconWrap, contentEl]);

    container.appendChild(toastEl);

    const record = { el: toastEl, isQuiet, timerId: null };
    this.activeToasts.push(record);

    const removeToast = () => this.evictToastRecord(record, false);
    toastEl.addEventListener('click', removeToast);

    if (duration > 0) {
      record.timerId = setTimeout(removeToast, duration);
    }

    return toastEl;
  }

  /**
   * Compact 1.2s quiet toast (max 1 stacked) for frequent playback micro-actions.
   * @param {string} title
   * @param {string} [description='']
   */
  quiet(title, description = '') {
    return this.show('info', title, description, 1200, { isQuiet: true });
  }

  clearAll() {
    const copy = [...this.activeToasts];
    copy.forEach(rec => this.evictToastRecord(rec, true));
    this.recentShown.clear();
  }

  success(title, description, duration) {
    return this.show('success', title, description, duration);
  }

  error(title, description, duration) {
    return this.show('error', title, description, duration);
  }

  warning(title, description, duration) {
    return this.show('warning', title, description, duration);
  }

  info(title, description, duration) {
    return this.show('info', title, description, duration);
  }
}

export const toast = new ToastManager();
