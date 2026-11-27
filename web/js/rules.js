/**
 * Pruner Studio - Rule Engine & Catalog Rules Workshop Drawer
 * Evaluates custom rules with precompiled RegExp caching, runs simulations against catalog,
 * previews matches, and stages selections for batching (never direct cloud writes).
 */
import { api } from './api.js';
import { state } from './state.js';
import { toast } from './toast.js';
import { modal } from './modal.js';
import { getTrackKey, durationSeconds, createElement, formatArtists, formatDuration } from './utils.js';
import { createIcon } from './icons.js';

const VALID_DECIMAL_RE = /^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$/;
const portableEscape = String.raw`\\(?:[dDsSwWbBfnrtv\\.^$|?*+(){}\[\]\/-]|x[0-9A-Fa-f]{2}|u(?:[0-9A-CE-Fa-ce-f][0-9A-Fa-f]{3}|[Dd][0-7][0-9A-Fa-f]{2}))`;
const portableClass = String.raw`\[\^?(?:${portableEscape}|[^\]\\])+\]`;
const PORTABLE_TOKEN = new RegExp(String.raw`(?:\\[1-9][0-9]*|${portableEscape}|${portableClass}|\(\?[:=!]|\((?!\?)|\{[0-9]+(?:,[0-9]*)?\}|[^\\\[\](){}]|\))`, 'uy');

function portableRegex(pattern) {
  const groups = [];
  const closed = new Set();
  let captureCount = 0;
  let pos = 0;
  let previous = '';
  while (pos < pattern.length) {
    PORTABLE_TOKEN.lastIndex = pos;
    const match = PORTABLE_TOKEN.exec(pattern);
    if (!match) return false;
    pos = PORTABLE_TOKEN.lastIndex;
    let part = match[0];
    if (part.startsWith('(')) {
      if (part === '(') captureCount++;
      groups.push([part, captureCount]);
    } else if (part === ')') {
      if (!groups.length) return false;
      const [group, index] = groups.pop();
      if (group === '(') closed.add(index);
      part = ['(?=', '(?!'].includes(group) ? 'assertion' : ')';
    } else if (/^\\[1-9]/.test(part)) {
      if (part.length > 3 || !closed.has(Number(part.slice(1)))) return false;
    } else if (part.startsWith('{') || ['*', '+', '?'].includes(part)) {
      if (previous === 'assertion' || (part === '+' && previous === 'quantifier')) return false;
      if (part.startsWith('{') && part.match(/[0-9]+/g).some(n => Number(n) >= 4294967295)) return false;
      part = 'quantifier';
    } else if (['[]', '[^]', '\\-'].includes(part)) return false;
    previous = part;
  }
  return !groups.length;
}

const MAX_REGEX_CACHE = 256;

export class RuleEngine {
  static _regexCache = new Map();

  /**
   * Returns a cached compiled case-insensitive RegExp (or null for invalid patterns).
   * Prevents recompiling regexes per-song across 10k+ track catalogs.
   * @param {string} pattern
   * @returns {RegExp|null}
   */
  static getCompiledRegex(pattern) {
    const key = String(pattern ?? '');
    if (this._regexCache.has(key)) {
      return this._regexCache.get(key);
    }
    let compiled = null;
    try {
      if (portableRegex(key)) compiled = new RegExp(key, 'iu');
    } catch {
      compiled = null;
    }
    if (this._regexCache.size >= MAX_REGEX_CACHE) {
      const oldest = this._regexCache.keys().next().value;
      this._regexCache.delete(oldest);
    }
    this._regexCache.set(key, compiled);
    return compiled;
  }

  /**
   * Evaluate a single clause against a song.
   * @param {Song} song
   * @param {RuleClause} clause
   * @returns {boolean}
   */
  static validClause(clause) {
    if (!clause || !['string', 'number', 'boolean'].includes(typeof clause.value) || String(clause.value).trim() === '') return false;
    const {field, operator, value} = clause;
    const equality = ['equals', 'not_equals'].includes(operator);
    if (field === 'liked') return equality && ['true', 'false'].includes(String(value).toLowerCase());
    if (typeof value === 'boolean') return false;
    if (field === 'duration') {
      if (!(equality || ['lt', 'lte', 'gt', 'gte'].includes(operator))) return false;
      if (typeof value === 'number') return Number.isFinite(value);
      return typeof value === 'string' && VALID_DECIMAL_RE.test(value) && Number.isFinite(Number(value));
    }
    return ['title', 'artist', 'artists', 'album', 'availability'].includes(field) &&
      (equality || ['contains', 'not_contains', 'starts_with', 'ends_with'].includes(operator) ||
       (operator === 'regex' && !!this.getCompiledRegex(value)));
  }

  static evaluateClause(song, clause) {
    if (!song || !this.validClause(clause)) return false;
    if (clause.field === 'liked') {
      const liked = state.isSongLiked(song) || song.liked === true;
      return (liked === (String(clause.value).toLowerCase() === 'true')) === (clause.operator === 'equals');
    }

    let targetVal = song[clause.field];
    if (clause.field === 'artist' || clause.field === 'artists') {
      targetVal = formatArtists(song.artists);
    }
    if (clause.field === 'duration') {
      targetVal = durationSeconds(song);
    }

    const op = clause.operator;
    const clauseVal = clause.value;

    // Numeric Comparisons
    if (['gt', 'lt', 'gte', 'lte'].includes(op) || typeof targetVal === 'number') {
      const numTarget = Number(targetVal) || 0;
      const numClause = Number(clauseVal) || 0;

      switch (op) {
        case 'gt': return numTarget > numClause;
        case 'lt': return numTarget < numClause;
        case 'gte': return numTarget >= numClause;
        case 'lte': return numTarget <= numClause;
        case 'equals': return numTarget === numClause;
        case 'not_equals': return numTarget !== numClause;
        default: return false;
      }
    }

    // String / General Comparisons
    const strTarget = String(targetVal || '').toLowerCase();
    const strClause = String(clauseVal || '').toLowerCase();

    switch (op) {
      case 'equals':
        return strTarget === strClause;
      case 'not_equals':
        return strTarget !== strClause;
      case 'contains':
        return strTarget.includes(strClause);
      case 'not_contains':
        return !strTarget.includes(strClause);
      case 'starts_with':
        return strTarget.startsWith(strClause);
      case 'ends_with':
        return strTarget.endsWith(strClause);
      case 'regex': {
        const regex = this.getCompiledRegex(clauseVal);
        return regex ? regex.test(String(targetVal || '')) : false;
      }
      default:
        return false;
    }
  }

  /**
   * Evaluate if a song matches a rule (all clauses must match).
   * @param {Song} song
   * @param {Rule} rule
   * @returns {boolean}
   */
  static evaluateRule(song, rule) {
    if (!rule || !rule.enabled) return false;
    if (!Array.isArray(rule.match_clauses) || rule.match_clauses.length === 0) return false;

    const matches = clause => this.evaluateClause(song, clause);
    return rule.match_mode === 'any' ? rule.match_clauses.some(matches) : rule.match_clauses.every(matches);
  }

  /**
   * Find all rules matching a song.
   * @param {Song} song
   * @param {Rule[]} rules
   * @returns {Rule[]}
   */
  static getMatchingRules(song, rules) {
    if (!rules || rules.length === 0) return [];
    return rules.filter(r => r.enabled && this.evaluateRule(song, r));
  }

  /**
   * Run simulation over full catalog.
   * @param {Song[]} catalog
   * @param {Rule[]} rules
   * @returns {{ ruleMatches: Map<string, Song[]>, allMatchedSongs: Song[], totalCount: number }}
   */
  static runSimulation(catalog, rules) {
    const ruleMatches = new Map();
    const seen = new Set();
    const allMatchedSongs = [];

    const activeRules = (rules || []).filter(r => r.enabled);
    activeRules.forEach(r => ruleMatches.set(r.id, []));

    for (const song of (catalog || [])) {
      const key = getTrackKey(song);
      if (seen.has(key)) continue;
      seen.add(key);
      let songMatched = false;
      for (const rule of activeRules) {
        if (this.evaluateRule(song, rule)) {
          ruleMatches.get(rule.id).push(song);
          songMatched = true;
        }
      }
      if (songMatched) {
        allMatchedSongs.push(song);
      }
    }

    return {
      ruleMatches,
      allMatchedSongs,
      totalCount: allMatchedSongs.length,
    };
  }
}

class RulesViewManager {
  constructor() {
    this.currentEditingRule = null;
    this.container = null;
    this.playlistCreatePending = false;
  }

  init(containerId) {
    this.container = document.getElementById(containerId);
  }

  openDrawer() {
    const drawer = document.getElementById('rules-drawer-panel');
    if (drawer) {
      drawer.classList.add('open');
      state.set({ isRulesDrawerOpen: true });
      this.render();
    }
  }

  closeDrawer() {
    const drawer = document.getElementById('rules-drawer-panel');
    if (drawer) {
      drawer.classList.remove('open');
      state.set({ isRulesDrawerOpen: false });
    }
  }

  async loadRules(isCurrent = () => true) {
    try {
      const res = await api.getRules();
      if (!isCurrent()) return false;
      if (res.ok && res.data && Array.isArray(res.data.rules)) {
        state.set({ rules: res.data.rules });
      } else if (res.ok && Array.isArray(res.data)) {
        state.set({ rules: res.data });
      } else {
        const defaultRules = [
          {
            id: 'rule_short',
            name: '短片段曲目 (< 60秒)',
            enabled: true,
            match_clauses: [{ field: 'duration', operator: 'lt', value: 60 }],
            output_playlist_name: '短片与过渡曲',
          },
          {
            id: 'rule_unavailable',
            name: '无音源 / 灰色曲目',
            enabled: true,
            match_clauses: [{ field: 'availability', operator: 'equals', value: 'unavailable' }],
            output_playlist_name: '已失效歌曲',
          },
          {
            id: 'rule_live_remix',
            name: '现场版 / 混音重置版',
            enabled: false,
            match_clauses: [{ field: 'title', operator: 'regex', value: '(live|remix|acoustic|instrumental)' }],
            output_playlist_name: 'Live 与伴奏',
          },
        ];
        state.set({ rules: defaultRules });
      }
      this.render();
      return true;
    } catch (err) {
      if (isCurrent()) console.warn('Failed to load rules:', err);
      return false;
    }
  }

  render() {
    if (!this.container) return;
    this.container.textContent = '';

    const rules = state.get('rules') || [];
    const catalog = state.getRuleEvaluationPool();
    const sim = RuleEngine.runSimulation(catalog, rules);

    const simSummaryEl = document.getElementById('rules-sim-summary');
    if (simSummaryEl) {
      simSummaryEl.textContent = `已命中 ${sim.totalCount} 首歌曲 (共 ${rules.filter(r => r.enabled).length} 条启用规则 / 曲库 ${catalog.length} 首)`;
    }

    if (rules.length === 0) {
      const emptyEl = createElement('div', {
        className: 'empty-state',
        children: [
          createElement('div', { className: 'empty-state-title', text: '暂无自定义规则' }),
          createElement('div', { className: 'empty-state-desc', text: '创建筛选规则，按时长或关键词找出歌曲，再加入待处理队列。' }),
          createElement('button', {
            className: 'btn btn-primary btn-sm',
            text: '新建第一条规则',
            events: { click: () => this.openRuleEditor(null) },
          }),
        ],
      });
      this.container.appendChild(emptyEl);
      return;
    }

    rules.forEach(rule => {
      const matchedSongs = sim.ruleMatches.get(rule.id) || [];

      const switchInput = createElement('input', {
        attributes: { type: 'checkbox', 'aria-label': `启用规则 ${rule.name}` },
        events: {
          change: async (e) => {
            const previous = rule.enabled;
            rule.enabled = e.target.checked;
            if (!(await this.persistRules())) {
              rule.enabled = previous;
              toast.error('保存失败', '规则状态修改未成功同步。');
            }
            this.render();
          },
        },
      });
      if (rule.enabled) switchInput.checked = true;

      const switchSlider = createElement('span', { className: 'switch-slider' });
      const switchLabel = createElement('label', { className: 'switch' }, [switchInput, switchSlider]);

      const clauseTags = (rule.match_clauses || []).map(c => createElement('span', {
        className: 'clause-tag',
        text: `${c.field} ${c.operator} ${c.value}`,
      }));

      if (rule.output_playlist_name) {
        clauseTags.push(createElement('span', {
          className: 'rule-output-tag',
          text: `输出目标: ${rule.output_playlist_name}`,
        }));
      }

      const matchCountBadge = createElement('span', {
        className: `badge ${matchedSongs.length > 0 ? 'badge-warning' : 'badge-muted'}`,
        text: `${matchedSongs.length} 首命中`,
      });

      const previewBtn = createElement('button', {
        className: 'btn btn-ghost btn-sm',
        text: '预览',
        events: { click: () => this.previewMatchedSongs(rule, matchedSongs) },
      });

      const stageBtn = createElement('button', {
        className: 'btn btn-ghost btn-sm',
        text: '加入批处理',
        attributes: { title: '将全部有效命中歌曲加入待提交队列' },
        events: {
          click: () => {
            if (matchedSongs.length === 0) {
              toast.warning('无匹配歌曲', '当前规则未命中任何歌曲。');
              return;
            }
            let stagedCount = 0;
            let skippedCount = 0;
            let already = 0;
            matchedSongs.forEach(s => {
              const resolved = state.resolveDestructiveAction(s, s.source_playlist_id);
              if (resolved.valid) {
                const targetTk = getTrackKey(s) || String(s.id);
                if (state.stageBatchAction(s, resolved.action, resolved.playlistId, resolved.targetName)) stagedCount++;
                else if ((state.get('batchQueue') || []).some(row => (getTrackKey(row.song) || String(row.song.id)) === targetTk &&
                  String(row.playlistId) === String(resolved.playlistId) && row.action === resolved.action)) already++;
                else skippedCount++;
              } else {
                skippedCount++;
              }
            });

            toast.info('批处理结果', `新增 ${stagedCount} 首 · 已在队列 ${already} 首 · 跳过 ${skippedCount} 首`);
          },
        },
      });

      const editBtn = createElement('button', {
        className: 'btn btn-ghost btn-sm',
        text: '编辑',
        events: { click: () => this.openRuleEditor(rule) },
      });

      const deleteBtn = createElement('button', {
        className: 'btn btn-ghost btn-sm text-danger',
        text: '删除',
        events: { click: () => this.deleteRule(rule.id) },
      });

      const card = createElement('div', {
        className: `rule-card ${rule.enabled ? '' : 'disabled'}`,
        children: [
          createElement('div', {
            className: 'rule-main-info',
            children: [
              switchLabel,
              createElement('div', {
                className: 'rule-content',
                children: [
                  createElement('div', {
                    className: 'rule-name-row',
                    children: [
                      createElement('span', { className: 'rule-name', text: rule.name }),
                      matchCountBadge,
                    ],
                  }),
                  createElement('div', { className: 'rule-clauses-preview' }, clauseTags),
                ],
              }),
            ],
          }),
          createElement('div', {
            className: 'rule-card-actions',
            children: [previewBtn, createElement('button', {className: 'btn btn-secondary btn-sm', text: '生成歌单',
              attributes: {disabled: matchedSongs.length ? null : 'disabled'},
              events: {click: () => state.emit('playlist:create-from-rule', { songs: matchedSongs, name: rule.output_playlist_name || rule.name })} }),
              stageBtn, editBtn, deleteBtn],
          }),
        ],
      });

      this.container.appendChild(card);
    });
  }

  openRuleEditor(rule = null) {
    this.currentEditingRule = rule ? JSON.parse(JSON.stringify(rule)) : {
      id: `rule_${Date.now()}`,
      name: '新建规则',
      enabled: true,
      match_clauses: [{ field: 'duration', operator: 'lt', value: 90 }],
      output_playlist_name: '',
    };

    const nameInput = document.getElementById('rule-edit-name');
    const outputInput = document.getElementById('rule-edit-output');
    const clausesContainer = document.getElementById('rule-edit-clauses-container');

    if (nameInput) nameInput.value = this.currentEditingRule.name;
    if (outputInput) outputInput.value = this.currentEditingRule.output_playlist_name || '';

    this.renderClauseBuilders(clausesContainer);
    modal.open('#rule-editor-modal');
  }

  renderClauseBuilders(container) {
    if (!container) return;
    container.textContent = '';

    (this.currentEditingRule.match_clauses || []).forEach((clause, index) => {
      const fieldSelect = createElement('select', {
        className: 'clause-select',
        attributes: { 'aria-label': '匹配字段' },
        events: { change: (e) => { clause.field = e.target.value; } },
      });
      [
        { val: 'duration', label: '时长 (秒)' },
        { val: 'title', label: '歌曲标题' },
        { val: 'artist', label: '歌手 / 艺术家' },
        { val: 'album', label: '专辑名' },
        { val: 'availability', label: '可用状态' },
        { val: 'liked', label: '红心状态' },
      ].forEach(f => {
        const opt = createElement('option', { text: f.label, attributes: { value: f.val } });
        if (clause.field === f.val) opt.selected = true;
        fieldSelect.appendChild(opt);
      });

      const opSelect = createElement('select', {
        className: 'clause-select',
        attributes: { 'aria-label': '匹配操作' },
        events: { change: (e) => { clause.operator = e.target.value; } },
      });
      [
        { val: 'equals', label: '等于' },
        { val: 'not_equals', label: '不等于' },
        { val: 'contains', label: '包含' },
        { val: 'not_contains', label: '不包含' },
        { val: 'starts_with', label: '开头为' },
        { val: 'ends_with', label: '结尾为' },
        { val: 'regex', label: '正则匹配' },
        { val: 'gt', label: '大于 >' },
        { val: 'lt', label: '小于 <' },
        { val: 'gte', label: '大于等于 >=' },
        { val: 'lte', label: '小于等于 <=' },
      ].forEach(op => {
        const opt = createElement('option', { text: op.label, attributes: { value: op.val } });
        if (clause.operator === op.val) opt.selected = true;
        opSelect.appendChild(opt);
      });

      const valueInput = createElement('input', {
        className: 'clause-input',
        attributes: { type: 'text', value: String(clause.value ?? ''), placeholder: '匹配值' },
        events: { input: (e) => { clause.value = e.target.value; } },
      });

      const removeBtn = createElement('button', {
        className: 'btn btn-ghost btn-icon btn-sm',
        attributes: { title: '删除此条件', 'aria-label': '删除此条件' },
        events: {
          click: () => {
            this.currentEditingRule.match_clauses.splice(index, 1);
            this.renderClauseBuilders(container);
          },
        },
        children: [createIcon('close', { size: 14 })],
      });

      const row = createElement('div', {
        className: 'clause-builder-row',
        children: [fieldSelect, opSelect, valueInput, removeBtn],
      });

      container.appendChild(row);
    });

    const addClauseBtn = createElement('button', {
      className: 'btn btn-secondary btn-sm',
      events: {
        click: () => {
          this.currentEditingRule.match_clauses.push({ field: 'title', operator: 'contains', value: '' });
          this.renderClauseBuilders(container);
        },
      },
      children: [createIcon('plus', { size: 14 }), ' 添加匹配条件'],
    });

    container.appendChild(addClauseBtn);
  }

  async saveCurrentRule() {
    if (!this.currentEditingRule) return;
    const nameInput = document.getElementById('rule-edit-name');
    const outputInput = document.getElementById('rule-edit-output');

    if (nameInput) this.currentEditingRule.name = nameInput.value.trim() || '未命名规则';
    if (outputInput) this.currentEditingRule.output_playlist_name = outputInput.value.trim();

    if (!this.currentEditingRule.match_clauses?.length || !this.currentEditingRule.match_clauses.every(c => RuleEngine.validClause(c))) {
      toast.warning('无法保存规则', '请检查字段、匹配操作和值，红心状态填写 true 或 false。');
      return;
    }
    const previousRules = JSON.parse(JSON.stringify(state.get('rules') || []));
    const rules = [...(state.get('rules') || [])];
    const existingIndex = rules.findIndex(r => r.id === this.currentEditingRule.id);

    if (existingIndex >= 0) {
      rules[existingIndex] = this.currentEditingRule;
    } else {
      rules.push(this.currentEditingRule);
    }

    state.set({ rules: [...rules] });
    if (!(await this.persistRules())) {
      state.set({ rules: previousRules });
      this.render();
      toast.error('保存失败', '规则未能成功保存至本地服务。');
      return;
    }
    modal.close('#rule-editor-modal');
    this.render();
    toast.success('规则已保存', `"${this.currentEditingRule.name}" 更新成功。`);
  }

  async deleteRule(ruleId) {
    const previousRules = JSON.parse(JSON.stringify(state.get('rules') || []));
    const rules = (state.get('rules') || []).filter(r => r.id !== ruleId);
    state.set({ rules });
    if (!(await this.persistRules())) {
      state.set({ rules: previousRules });
      this.render();
      toast.error('删除失败', '规则删除未成功同步。');
      return;
    }
    this.render();
    toast.info('规则已删除', '已移除该筛选规则。');
  }

  async persistRules() {
    try {
      const result = await api.saveRules(state.get('rules'));
      return Boolean(result.ok);
    } catch (err) {
      console.warn('Failed to persist rules to backend:', err);
      return false;
    }
  }

  previewMatchedSongs(rule, matchedSongs, page = 0) {
    const previewList = document.getElementById('rule-preview-songs-list');
    const previewTitle = document.getElementById('rule-preview-title');

    if (previewTitle) {
      previewTitle.textContent = `命中歌曲列表: "${rule.name}" (${matchedSongs.length} 首)`;
    }

    if (previewList) {
      previewList.textContent = '';
      if (matchedSongs.length === 0) {
        previewList.appendChild(createElement('div', {
          className: 'empty-state',
          children: [createElement('div', { className: 'empty-state-title', text: '当前无匹配歌曲' })],
        }));
      } else {
        matchedSongs.slice(page * 100, (page + 1) * 100).forEach(song => {
          const item = createElement('div', {
            className: 'preview-song-row',
            style: 'display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--border-subtle); font-size: var(--text-xs);',
            children: [
              createElement('div', {
                children: [
                  createElement('strong', { text: song.title || '未知曲目' }),
                  createElement('span', { text: ` - ${formatArtists(song.artists)}`, style: 'color: var(--text-secondary); margin-left: 4px;' }),
                ],
              }),
              createElement('span', { text: formatDuration(song), style: 'color: var(--text-muted);' }),
            ],
          });
          previewList.appendChild(item);
        });
      }
    }

    if (previewList && matchedSongs.length > 100) {
      for (const [label, target] of [['上一页', page - 1], ['下一页', page + 1]]) {
        previewList.appendChild(createElement('button', {className: 'btn btn-ghost', text: label,
          attributes: {disabled: target < 0 || target * 100 >= matchedSongs.length ? 'disabled' : null},
          events: {click: () => this.previewMatchedSongs(rule, matchedSongs, target)} }));
      }
    }
    if (modal.activeModal?.id !== 'rule-preview-modal') modal.open('#rule-preview-modal');
  }
}

export const rulesManager = new RulesViewManager();
