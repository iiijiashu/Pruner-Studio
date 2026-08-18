/**
 * Pruner Studio - Toast Notifications
 * Quiet Bone/Ash alerts with zero emojis.
 */
import { createElement } from './utils.js';

class ToastManager {
  constructor() {
    this.container = null;
  }

  ensureContainer() {
    if (typeof document === 'undefined') return null;

    if (!this.container || !document.body.contains(this.container)) {
      let existing = document.getElementById('toast-container');
      if (!existing) {
        existing = createElement('div', {
          className: 'toast-container',
          attributes: { id: 'toast-container', 'aria-live': 'polite' }
        });
        if (document.body) {
          document.body.appendChild(existing);
        }
      }
      this.container = existing;
    }
    return this.container;
  }

  /**
   * Show a toast message.
   * @param {'info'|'success'|'warning'|'error'} type
   * @param {string} title
   * @param {string} [description]
   * @param {number} [duration=3500]
   */
  show(type = 'info', title = '', description = '', duration = 3500) {
    const container = this.ensureContainer();
    if (!container) return null;

    const icons = {
      success: 'OK',
      error: '×',
      warning: '!',
      info: 'i',
    };

    const iconEl = createElement('span', {
      className: `toast-icon toast-icon-${type}`,
      text: icons[type] || '·'
    });

    const contentEl = createElement('div', { className: 'toast-content' });
    const titleEl = createElement('div', { className: 'toast-title', text: title });
    contentEl.appendChild(titleEl);

    if (description) {
      const descEl = createElement('div', { className: 'toast-desc', text: description });
      contentEl.appendChild(descEl);
    }

    const toastEl = createElement('div', {
      className: `toast toast-${type}`,
      attributes: { role: 'status' }
    }, [iconEl, contentEl]);

    container.appendChild(toastEl);

    const removeToast = () => {
      if (toastEl.classList.contains('toast-hiding')) return;
      toastEl.classList.add('toast-hiding');
      setTimeout(() => {
        if (toastEl.parentNode) {
          toastEl.parentNode.removeChild(toastEl);
        }
      }, 150);
    };

    toastEl.addEventListener('click', removeToast);

    if (duration > 0) {
      setTimeout(removeToast, duration);
    }

    return toastEl;
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
