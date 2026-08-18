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
      if (e.key === 'Escape' && this.activeModal) {
        this.close(this.activeModal);
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

  /**
   * Open a modal element by selector or HTMLElement.
   * @param {string|HTMLElement} modalOrSelector
   */
  open(modalOrSelector) {
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
    this.previouslyFocused = document.activeElement;

    // Focus first focusable element
    const focusable = modal.querySelector('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
    if (focusable) {
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
