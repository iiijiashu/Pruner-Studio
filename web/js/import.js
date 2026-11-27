/**
 * Pruner Studio - Catalog Import & Offline Storage
 * Handles JSON/CSV file uploads, validation, and local persistence.
 */
import { api } from './api.js';
import { state } from './state.js';
import { toast } from './toast.js';
import { modal } from './modal.js';

class ImportManager {
  constructor() {
    this.dropzone = null;
    this.fileInput = null;
  }

  init(dropzoneId, fileInputId) {
    this.dropzone = document.getElementById(dropzoneId);
    this.fileInput = document.getElementById(fileInputId);
    this.initEvents();
    globalThis.window?.addEventListener('pruner:modal-closed', event => {
      if (event.detail?.id !== 'import-modal') return;
      if (this.fileInput) this.fileInput.value = '';
      const replace = document.getElementById('import-replace');
      if (replace) replace.checked = false;
      this.dropzone?.classList.remove('drag-over');
    });
  }

  initEvents() {
    if (this.dropzone && this.fileInput) {
      this.dropzone.addEventListener('click', () => this.fileInput.click());
      this.dropzone.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          this.fileInput.click();
        }
      });

      this.dropzone.addEventListener('dragover', (e) => {
        e.preventDefault();
        this.dropzone.classList.add('drag-over');
      });

      this.dropzone.addEventListener('dragleave', () => {
        this.dropzone.classList.remove('drag-over');
      });

      this.dropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        this.dropzone.classList.remove('drag-over');
        if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
          this.handleFile(e.dataTransfer.files[0]);
        }
      });

      this.fileInput.addEventListener('change', (e) => {
        if (e.target.files && e.target.files.length > 0) {
          this.handleFile(e.target.files[0]);
        }
      });
    }
  }

  async handleFile(file) {
    if (!file) return;
    if (this.fileInput) this.fileInput.value = '';
    const scope = state.writeScope();
    const name = file.name.toLowerCase();
    const isJson = name.endsWith('.json');
    const isCsv = name.endsWith('.csv');

    if (!isJson && !isCsv) {
      toast.warning('文件格式不支持', '请选择 .json 或 .csv 格式的曲库导出文件。');
      return;
    }

    const format = isJson ? 'json' : 'csv';
    toast.info('正在读取文件', `加载 ${file.name}...`);

    try {
      const text = await file.text();
      // Client-side validation
      if (isJson) {
        try {
          const parsed = JSON.parse(text);
          if (!Array.isArray(parsed) && !parsed?.tracks && !parsed?.songs && parsed?.schema_version !== 2) {
            toast.warning('数据格式警告', 'JSON 文件应包含曲目数组或 { tracks: [...] } 结构。');
          }
        } catch {
          toast.error('解析失败', '无效的 JSON 语法。');
          return;
        }
      }

      toast.info('正在导入', '提交至曲库导入接口...');
      const mode = globalThis.document?.getElementById('import-replace')?.checked ? 'replace' : 'merge';
      const res = await api.importCatalog({ format, content: text, mode, ...scope });

      if (scope.session_generation !== state.get('sessionGeneration')) return;
      if (res.ok && res.data) {
        const level = res.data.failed_count ? 'warning' : 'success';
        toast[level]('导入完成', `新增 ${res.data.added_count ?? res.data.imported_count ?? 0} 首 · 已有 ${res.data.existing_count || 0} 首 · 失败 ${res.data.failed_count || 0} 首`);
        const replace = globalThis.document?.getElementById('import-replace');
        if (replace) replace.checked = false;

        // The import response contains only the imported tracks; re-fetch the
        // full catalog so other playlists do not vanish from the local views.
        try {
          const catRes = await api.getCatalog();
          if (scope.session_generation !== state.get('sessionGeneration')) return;
          if (catRes.ok && catRes.data) {
            const tracks = Array.isArray(catRes.data.tracks)
              ? catRes.data.tracks
              : (Array.isArray(catRes.data) ? catRes.data : []);
            state.set({ catalog: tracks });
            state.emit('catalog:loaded', tracks);
          }
        } catch (err) {
          console.warn('Catalog refresh after import failed:', err);
        }
        modal.close('#import-modal');
      } else {
        toast.error('导入失败', res.error?.message || '无法处理该曲库文件。');
      }
    } catch (err) {
      toast.error('文件处理错误', err.message);
    } finally { if (this.fileInput) this.fileInput.value = ''; }
  }
}

export const importManager = new ImportManager();
