/**
 * Pruner Studio - Modal Dialog Manager
 */

class ModalManager {
  constructor() {
    this.activeModal = null;
    this.previouslyFocused = null;
    if (typeof document !== 'undefined') {
      this.initGlobalListeners();
    }
  }

  initGlobalListeners() {
    if (typeof document === 'undefined') return;

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Tab' || !this.activeModal) return;
      const focusables = this.getFocusableElements(this.activeModal);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (!focusables.includes(active)) {
        e.preventDefault?.();
        (e.shiftKey ? last : first).focus?.();
      } else if (e.shiftKey && active === first) {
        e.preventDefault?.();
        last.focus?.();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault?.();
        first.focus?.();
      }
    });

    document.addEventListener('click', (e) => {
      if (e.target.classList && e.target.classList.contains('modal-backdrop')) {
        this.close(e.target);
      }
      if (e.target.closest && e.target.closest('[data-modal-close]')) {
        const modal = e.target.closest('.modal-backdrop');
        if (modal) this.close(modal);
      }
    });
  }

  getFocusableElements(modal) {
    if (!modal || typeof modal.querySelectorAll !== 'function') return [];
    const selector = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';
    return [...modal.querySelectorAll(selector)].filter(el => {
      if (!el || el.disabled || el.hidden) return false;
      if (el.getAttribute?.('aria-hidden') === 'true') return false;
      if (typeof el.closest === 'function' && el.closest('[hidden], .hidden, [aria-hidden="true"]')) return false;
      if (typeof window !== 'undefined' && typeof window.getComputedStyle === 'function') {
        try {
          const style = window.getComputedStyle(el);
          if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
        } catch {}
      }
      return true;
    });
  }

  /**
   * Open a modal element by selector or HTMLElement.
   * @param {string|HTMLElement} modalOrSelector
   * @param {HTMLElement|null} [triggerElement=null]
   */
  open(modalOrSelector, triggerElement = null) {
    if (typeof document === 'undefined') return;

    const modal = typeof modalOrSelector === 'string'
      ? document.querySelector(modalOrSelector)
      : modalOrSelector;

    if (!modal) return;

    if (this.activeModal && this.activeModal !== modal) {
      this.close(this.activeModal);
    }

    modal.classList.add('open');
    this.activeModal = modal;
    this.previouslyFocused = triggerElement || document.activeElement;

    // Focus first visible enabled focusable element
    const focusable = this.getFocusableElements(modal)[0];
    if (focusable?.focus) {
      focusable.focus();
    }
  }

  /**
   * Close a modal element.
   * @param {string|HTMLElement} [modalOrSelector]
   */
  close(modalOrSelector) {
    if (typeof document === 'undefined') return;

    const modal = modalOrSelector
      ? (typeof modalOrSelector === 'string' ? document.querySelector(modalOrSelector) : modalOrSelector)
      : this.activeModal;

    if (!modal) return;

    modal.classList.remove('open');
    window.dispatchEvent(new CustomEvent('pruner:modal-closed', { detail: { id: modal.id || null } }));
    if (this.activeModal === modal) {
      this.activeModal = null;
    }
    if (this.previouslyFocused?.focus) this.previouslyFocused.focus();
    this.previouslyFocused = null;
  }
}

export const modal = new ModalManager();
