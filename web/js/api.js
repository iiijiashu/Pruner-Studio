/**
 * Pruner Studio - API Client
 * Strict implementation of API envelope and error contract covering playback,
 * high-res/external audio candidates, discovery recommendations, library, settings, and safe pruning.
 */

import { state } from './state.js';

class ApiClient {
  getLanStatus() { return this.request('/api/lan-sync/status'); }
  toggleLanSync(settings) { return this.lanPost('toggle', settings); }
  startLanPairing() { return this.lanPost('pair/start'); }
  cancelLanPairing() { return this.lanPost('pair/cancel'); }
  revokeLanDevice(id) { return this.request('/api/lan-sync/devices/' + encodeURIComponent(id), { method: 'DELETE' }); }
  pushLanHandoff(session) { return this.lanPost('handoff/push', session); }
  ackLanHandoff(id) { return this.lanPost('handoff/ack', { handoff_id: id }); }
  getLanHandoff() { return this.request('/api/lan-sync/handoff'); }
  lanPost(route, body = {}) {
    return this.request('/api/lan-sync/' + route, { method: 'POST', body: JSON.stringify(body) });
  }
  /**
   * Internal fetch wrapper enforcing JSON envelope contract.
   * @param {string} endpoint
   * @param {RequestInit} [options]
   * @returns {Promise<{ ok: boolean, data: any, error: { code: string, message: string } | null }>}
   */
  async request(endpoint, options = {}) {
    const defaultHeaders = {
      'Accept': 'application/json',
    };
    const method = String(options.method || 'GET').toUpperCase();
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
      defaultHeaders['Content-Type'] = 'application/json';
    }

    const config = {
      ...options,
      headers: {
        ...defaultHeaders,
        ...options.headers,
      },
    };

    try {
      const response = await fetch(endpoint, config);
      let payload = null;

      const contentType = response.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {
        try {
          payload = await response.json();
        } catch {
          payload = null;
        }
      } else {
        const text = await response.text();
        try {
          payload = JSON.parse(text);
        } catch {
          payload = null;
        }
      }

      if (payload && typeof payload === 'object' && typeof payload.ok === 'boolean') {
        if (!response.ok) {
          return {
            status: response.status, ok: false,
            data: null,
            error: payload.error || { code: 'HTTP_ERROR', message: `HTTP ${response.status}` },
          };
        }
        return payload.ok
          ? { ok: true, data: payload.data ?? null, error: null }
          : {
              ok: false,
              data: null,
              error: payload.error || { code: 'API_ERROR', message: '服务器拒绝了请求。' },
            };
      }

      return {
        status: response.status,
        ok: false,
        data: null,
        error: response.ok
          ? { code: 'API_CONTRACT_INVALID', message: '服务器返回了无效的 API 响应。' }
          : { code: 'HTTP_ERROR', message: `HTTP ${response.status}` },
      };
    } catch (err) {
      return {
        status: 0,
        ok: false,
        data: null,
        error: {
          code: 'NETWORK_ERROR',
          message: err instanceof Error ? err.message : '网络请求失败',
        },
      };
    }
  }

  // --- Auth & System ---

  async health() {
    return this.request('/api/health');
  }

  async getStatus() {
    return this.request('/api/status');
  }

  async createQr() {
    return this.request('/api/auth/qr', { method: 'POST' });
  }

  async checkQr(key) {
    return this.request(`/api/auth/qr/${encodeURIComponent(key)}`);
  }

  getAccounts() {
    return this.request('/api/accounts');
  }

  selectOfflineAccount(accountId) {
    return this.request('/api/accounts/offline', {method: 'POST', body: JSON.stringify({account_id: accountId})});
  }

  switchAccount(accountId) {
    return this.request('/api/accounts/switch', { method: 'POST', body: JSON.stringify({account_id: accountId}) });
  }

  deleteSavedAccount(accountId) {
    return this.request('/api/accounts/' + encodeURIComponent(accountId), { method: 'DELETE' });
  }

  useLocalAccount() {
    return this.request('/api/accounts/local', {method: 'POST'});
  }

  async logout() {
    return this.request('/api/auth/logout', { method: 'POST' });
  }

  // --- Playlists & Catalog ---

  async getPlaylists(options = null) {
    if (options && typeof options === 'object' && options.include_subscribed !== undefined) {
      const flag = options.include_subscribed ? '1' : '0';
      return this.request(`/api/playlists?include_subscribed=${flag}`);
    }
    return this.request('/api/playlists');
  }

  async syncPlaylist(playlistId) {
    return this.request(`/api/playlists/${encodeURIComponent(playlistId)}/sync`, {
      method: 'POST',
    });
  }

  async getCatalog(params = null) {
    if (!params || typeof params !== 'object') {
      return this.request('/api/catalog');
    }
    const qs = new URLSearchParams();
    for (const [key, val] of Object.entries(params)) {
      if (val !== null && val !== undefined && val !== '') {
        qs.set(key, String(val));
      }
    }
    const query = qs.toString();
    return this.request(query ? `/api/catalog?${query}` : '/api/catalog');
  }

  async importCatalog(format, content, mode = 'merge') {
    return this.request('/api/catalog/import', {
      method: 'POST',
      body: JSON.stringify(typeof format === 'object' ? format : { format, content, mode }),
    });
  }

  async getCloudSongs(limit = 200, offset = 0, full = false) {
    const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (full) qs.set("full", "1");
    return this.request(`/api/cloud?${qs.toString()}`);
  }

  async search(query, limit = 30, offset = 0) {
    const qs = new URLSearchParams({
      q: String(query || ''),
      limit: String(limit),
      offset: String(offset),
    });
    return this.request(`/api/search?${qs.toString()}`);
  }

  async searchMulti(query, channels = null, limit = 30, offset = 0, cursors = null) {
    const qs = new URLSearchParams({
      q: String(query || ''),
      limit: String(limit),
      offset: String(Math.max(0, Number(offset) || 0)),
    });
    if (cursors) qs.set("cursors", JSON.stringify(cursors));
    if (Array.isArray(channels) && channels.length > 0) {
      qs.set('channels', channels.join(','));
    } else if (typeof channels === 'string' && channels.trim()) {
      qs.set('channels', channels.trim());
    }
    return this.request(`/api/search/multi?${qs.toString()}`);
  }

  // --- Media & Audio ---

  async getLyrics(songId, audioId = null, trackMeta = null) {
    if (trackMeta && typeof trackMeta === 'object' && trackMeta.provider && trackMeta.provider !== 'netease') {
      const qs = new URLSearchParams();
      qs.set('provider', String(trackMeta.provider));
      if (trackMeta.external_id) qs.set('external_id', String(trackMeta.external_id));
      if (trackMeta.track_key) qs.set('track_key', String(trackMeta.track_key));
      if (trackMeta.title) qs.set('title', String(trackMeta.title));
      if (trackMeta.artist) qs.set('artist', String(trackMeta.artist));
      return this.request(`/api/lyrics?${qs.toString()}`);
    }
    let url = `/api/lyrics?id=${encodeURIComponent(songId)}`;
    if (audioId !== null && audioId !== undefined && String(audioId) !== '' && String(audioId) !== String(songId)) {
      url += `&audio_id=${encodeURIComponent(audioId)}`;
    }
    return this.request(url);
  }

  async getAudio(songId, options = null) {
    const qs = new URLSearchParams();
    if (songId !== null && songId !== undefined && songId !== '') {
      qs.set('id', String(songId));
    }
    if (options && typeof options === 'object') {
      if (options.quality) qs.set('quality', String(options.quality));
      if (options.allow_external !== undefined) qs.set('allow_external', options.allow_external ? '1' : '0');
      if (options.force_refresh) qs.set('force_refresh', '1');
      if (options.candidate_id) qs.set('candidate_id', String(options.candidate_id));
      if (options.provider) qs.set('provider', String(options.provider));
      if (options.external_id) qs.set('external_id', String(options.external_id));
      if (options.title) qs.set('title', String(options.title));
      if (options.artist) qs.set('artist', String(options.artist));
      if (options.duration) qs.set('duration', String(options.duration));
      if (options.stream_url_hint) qs.set('stream_url_hint', String(options.stream_url_hint));
    }
    return this.request(`/api/audio?${qs.toString()}`);
  }

  async getAudioCandidates(songId) {
    return this.request(`/api/audio/candidates?id=${encodeURIComponent(songId)}`);
  }

  getCoverProxyUrl(url, size = 64) {
    return `/api/cover?url=${encodeURIComponent(url)}&size=${encodeURIComponent(size)}`;
  }

  // --- Multi-Channel Hub & Audio Disk Cache ---

  async getChannels() {
    return this.request('/api/channels');
  }

  async updateChannel(channelId, payload) {
    return this.request(`/api/channels/${encodeURIComponent(channelId)}`, {
      method: 'PUT',
      body: JSON.stringify(payload || {}),
    });
  }

  async testChannel(channelId) {
    return this.request(`/api/channels/${encodeURIComponent(channelId)}/test`, {
      method: 'POST',
      body: JSON.stringify({}),
    });
  }

  async testAllChannels() {
    return this.request('/api/channels/test-all', {
      method: 'POST',
      body: JSON.stringify({}),
    });
  }

  async createBilibiliQr() {
    return this.request('/api/channels/bilibili/qr/create', {
      method: 'POST',
      body: JSON.stringify({}),
    });
  }

  async pollBilibiliQr(qrcodeKey) {
    const qs = new URLSearchParams({ qrcode_key: String(qrcodeKey || '') });
    return this.request(`/api/channels/bilibili/qr/poll?${qs.toString()}`);
  }

  async openChannelAuthWindow(channel) {
    return this.request('/api/desktop/channel-auth-window', {
      method: 'POST',
      body: JSON.stringify({ channel: String(channel || '') }),
    });
  }

  async hydrateSong(songId) {
    return this.request(`/api/songs/${encodeURIComponent(songId)}/hydrate`, {
      method: 'POST',
      body: JSON.stringify({}),
    });
  }

  async getAudioCacheStats() {
    return this.request('/api/cache/audio/stats');
  }

  async clearAudioCache() {
    return this.request('/api/cache/audio', {
      method: 'DELETE',
      body: JSON.stringify({}),
    });
  }

  // --- Playback History, Smart Shuffle, Smart Radio, Dusty Gems & Insights ---

  getPlaybackSession() {
    return this.request('/api/playback/session');
  }

  savePlaybackSession(payload, { keepalive = false } = {}) {
    return this.request('/api/playback/session', { method: 'PUT', body: JSON.stringify({ ...state.writeScope(), ...payload }), keepalive });
  }

  getPlaybackHistory({ limit = 100, offset = 0, q = '' } = {}) {
    return this.request(`/api/playback/history?${new URLSearchParams({ limit, offset, q })}`);
  }

  deletePlaybackHistory(trackKey, scope = state.writeScope()) {
    const query = trackKey ? `?track_key=${encodeURIComponent(trackKey)}` : '';
    return this.request('/api/playback/history' + query, { method: 'DELETE', body: JSON.stringify(scope) });
  }

  pickLocalFolder() {
    return this.request('/api/desktop/pick-folder', { method: 'POST', body: '{}' });
  }

  checkAppUpdate() {
    return this.request('/api/app/update-check');
  }

  recordPlayback(song, listenedMs = 0, completed = false) {
    return this.request('/api/playback/record', {
      method: 'POST',
      body: JSON.stringify({ ...state.writeScope(), ...song, listen_ms: Math.round(listenedMs), completed }),
    });
  }

  async smartShuffle(tracks, seed = null) {
    const body = { tracks: Array.isArray(tracks) ? tracks : [] };
    if (seed !== null && seed !== undefined) body.seed = seed;
    return this.request('/api/queue/smart-shuffle', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  async getSmartRadio(exploreRatio = 0.4, excludeIds = [], limit = 30) {
    const qs = new URLSearchParams({
      explore_ratio: String(exploreRatio),
      limit: String(limit),
    });
    if (Array.isArray(excludeIds) && excludeIds.length > 0) {
      qs.set('exclude_ids', excludeIds.slice(-80).join(','));
    }
    return this.request(`/api/recommend/smart-radio?${qs.toString()}`);
  }

  postSmartRadio(payload) {
    return this.request('/api/recommend/smart-radio', { method: 'POST', body: JSON.stringify(payload) });
  }

  postExploreBatch(payload) {
    return this.request('/api/recommend/explore-batch', { method: 'POST', body: JSON.stringify(payload) });
  }

  trashPersonalFmTrack(payload) {
    return this.request('/api/recommend/fm/trash', { method: 'POST', body: JSON.stringify(payload) });
  }

  toggleDesktopFullscreen() {
    return this.request('/api/desktop/window/fullscreen', { method: 'POST', body: '{}' });
  }

  getDesktopLyricsWindow() {
    return this.request('/api/desktop/lyrics-window');
  }

  async getDustyGems(options = {}) {
    const opts = typeof options === 'number' ? { limit: options } : (options && typeof options === 'object' ? options : {});
    const {
      limit = 30,
      offset = 0,
      kind = '',
      playlist = '',
      q = '',
      sort = '',
      seed = null,
      excludeIds = [],
      excludeKeys = [],
    } = opts;
    const qs = new URLSearchParams({ limit: String(limit) });
    const parsedOffset = Number(offset);
    if (Number.isFinite(parsedOffset) && parsedOffset > 0) {
      qs.set('offset', String(Math.floor(parsedOffset)));
    }
    const cleanKind = String(kind || '').trim();
    if (cleanKind && cleanKind !== 'all') qs.set('kind', cleanKind);
    const cleanPlaylist = String(playlist || '').trim();
    if (cleanPlaylist) qs.set('playlist', cleanPlaylist);
    const cleanQuery = String(q || '').trim();
    if (cleanQuery) qs.set('q', cleanQuery);
    const cleanSort = String(sort || '').trim();
    if (cleanSort) qs.set('sort', cleanSort);
    if (seed !== null && seed !== undefined && String(seed).trim() !== '') {
      qs.set('seed', String(seed));
    }
    if (Array.isArray(excludeIds) && excludeIds.length) {
      qs.set('exclude_ids', excludeIds.join(','));
    }
    if (Array.isArray(excludeKeys) && excludeKeys.length) {
      for (const key of excludeKeys) qs.append('exclude_key', key);
    }
    return this.request(`/api/recommend/dusty-gems?${qs}`);
  }

  async getInsights() {
    return this.request('/api/insights');
  }

  // --- Desktop Floating Lyrics ---

  async setDesktopLyricsWindow(open, locked = undefined) {
    const body = { open: Boolean(open) };
    if (typeof locked === 'boolean') body.locked = locked;
    return this.request('/api/desktop/lyrics-window', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  async pushDesktopPlaybackState(payload) {
    return this.request('/api/desktop/lyrics', {
      method: 'POST',
      body: JSON.stringify(payload || {}),
    });
  }

  async centerDesktopLyricsWindow() {
    return this.request('/api/desktop/lyrics-window/center', {
      method: 'POST',
      body: JSON.stringify({}),
    });
  }

  // --- Recommendations & Explicit Feedback ---

  getDailyHistory(date) {
    return this.request('/api/recommend/daily/history' + (date ? '?date=' + encodeURIComponent(date) : ''));
  }

  async getDailyRecommend({ forceCheck = false, knownRawIds = [] } = {}) {
    const qs = new URLSearchParams({ force_check: '1', known_raw_ids: knownRawIds.join(',') });
    return this.request('/api/recommend/daily' + (forceCheck ? '?' + qs : ''));
  }

  async getPersonalFm() {
    return this.request('/api/recommend/fm');
  }

  async getIntelligenceList(songId, playlistId) {
    const qs = new URLSearchParams({
      song_id: String(songId),
      playlist_id: String(playlistId),
    });
    return this.request(`/api/recommend/intelligence?${qs.toString()}`);
  }

  async getSimilarSongs(songId) {
    return this.request(`/api/recommend/similar?id=${encodeURIComponent(songId)}`);
  }

  async submitFeedback(songId, action, context = '') {
    return this.request('/api/feedback', {
      method: 'POST',
      body: JSON.stringify(typeof songId === 'object' ? songId : {
        ...state.writeScope(), song_id: Number(songId) || songId,
        action,
        context,
      }),
    });
  }

  async clearFeedback(payload = state.writeScope()) {
    return this.request('/api/feedback', {
      method: 'DELETE',
      body: JSON.stringify(payload),
    });
  }

  // --- Settings & Local Music Library ---

  async getSettings() {
    return this.request('/api/settings');
  }

  async saveSettings(settings) {
    return this.request('/api/settings', {
      method: 'PUT',
      body: JSON.stringify(settings),
    });
  }

  async scanLocalLibrary(dirs = null) {
    const payload = Array.isArray(dirs) ? { dirs } : {};
    return this.request('/api/local-library/scan', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  async getLocalLibrary(options = {}, legacyLimit = 500) {
    const { q = '', limit = 500, offset = 0 } = typeof options === 'string' ? {q: options, limit: legacyLimit} : options;
    const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (q) qs.set('q', String(q));
    return this.request(`/api/local-library?${qs.toString()}`);
  }

  async getLocalStream(filePath) {
    return this.request('/api/local-library/stream', {
      method: 'POST',
      body: JSON.stringify({ path: String(filePath || '') }),
    });
  }

  // --- Rules ---

  async getRules() {
    return this.request('/api/rules');
  }

  async saveRules(rules) {
    return this.request('/api/rules', {
      method: 'PUT',
      body: JSON.stringify({ rules }),
    });
  }

  // --- Operations ---

  async backup(params = {}) {
    return this.request('/api/operations/backup', {
      method: 'POST',
      body: JSON.stringify(params),
    });
  }

  async downloadBackup(id) {
    if (!/^[0-9a-f]{32}$/.test(id)) throw new Error('备份编号无效');
    const response = await fetch(`/api/operations/backup/${id}/download`, { cache: 'no-store' });
    if (!response.ok) throw new Error('服务端快照校验或下载失败');
    return response.blob();
  }

  async createPlaylist(name, songIds, privacy = 0, scope = state.writeScope()) {
    return this.request('/api/operations/create-playlist', {
      method: 'POST',
      body: JSON.stringify({
        name,
        song_ids: songIds,
        privacy,
        ...scope,
      }),
    });
  }

  async likeTrack(songId, like = true) {
    return this.request('/api/operations/like', {
      method: 'POST',
      body: JSON.stringify({
        song_id: Number(songId) || songId,
        like: Boolean(like),
        ...state.writeScope(),
      }),
    });
  }

  async addTracksToPlaylist(playlistId, songIds, scope = state.writeScope()) {
    return this.request(`/api/playlists/${encodeURIComponent(playlistId)}/add-tracks`, {
      method: 'POST',
      body: JSON.stringify({
        song_ids: songIds,
        ...scope,
      }),
    });
  }

  async getPendingOperations() {
    return this.request('/api/operations/pending');
  }

  async reconcilePendingOperation(operationId) {
    return this.request(`/api/operations/${encodeURIComponent(operationId)}/reconcile`, { method: 'POST' });
  }

  async ignorePendingOperation(operationId) {
    return this.request(`/api/operations/${encodeURIComponent(operationId)}/ignore`, { method: 'POST' });
  }

  /**
   * Unlike songs from liked music playlist.
   * @param {Array<number|string>} songIds
   * @param {number|string} [sourcePlaylistId]
   * @param {{ session_generation?: number, manifest_hash?: string } | null} [extra]
   */
  async unlike(songIds, sourcePlaylistId, extra = null) {
    const body = {
      song_ids: songIds,
      source_playlist_id: sourcePlaylistId,
      ...state.writeScope(),
      ...(extra?.expected_account_id != null ? { expected_account_id: extra.expected_account_id } : {}),
    };
    if (extra && typeof extra === 'object') {
      if (extra.session_generation !== undefined && extra.session_generation !== null) {
        body.session_generation = extra.session_generation;
      }
      if (extra.manifest_hash) {
        body.manifest_hash = extra.manifest_hash;
      }
    }
    return this.request('/api/operations/unlike', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  /**
   * Remove tracks from an owned playlist.
   * @param {number|string} playlistId
   * @param {Array<number|string>} songIds
   * @param {number|string} [sourcePlaylistId]
   * @param {{ session_generation?: number, manifest_hash?: string } | null} [extra]
   */
  async removeTracks(playlistId, songIds, sourcePlaylistId = playlistId, extra = null) {
    const body = {
      song_ids: songIds,
      source_playlist_id: sourcePlaylistId,
      ...state.writeScope(),
      ...(extra?.expected_account_id != null ? { expected_account_id: extra.expected_account_id } : {}),
    };
    if (extra && typeof extra === 'object') {
      if (extra.session_generation !== undefined && extra.session_generation !== null) {
        body.session_generation = extra.session_generation;
      }
      if (extra.manifest_hash) {
        body.manifest_hash = extra.manifest_hash;
      }
    }
    return this.request(`/api/playlists/${encodeURIComponent(playlistId)}/remove-tracks`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  /**
   * Reconcile operations when remote result is unknown or in doubt.
   * @param {'unlike'|'remove_tracks'} action
   * @param {number|string|null} playlistId
   * @param {Array<number|string>} songIds
   * @param {number|string|null} [sourcePlaylistId]
   */
  async reconcileOperations(action, playlistId, songIds, sourcePlaylistId = playlistId) {
    return this.request('/api/operations/reconcile', {
      method: 'POST',
      body: JSON.stringify({
        action,
        playlist_id: playlistId !== null && playlistId !== undefined ? Number(playlistId) || playlistId : null,
        source_playlist_id: sourcePlaylistId,
        song_ids: songIds,
      }),
    });
  }

  // --- Canonical Favorites, Hybrid Playlists, Route Cache & Blocklist ---

  async getFavorites() {
    return this.request('/api/favorites');
  }

  async setFavorite(trackKey, liked = true, track = null) {
    return this.request('/api/favorites', {
      method: 'POST',
      body: JSON.stringify({ track_key: trackKey, liked: Boolean(liked), ...(track ? { track } : {}) }),
    });
  }

  async getHybridPlaylists(includeTracks = false) {
    const q = includeTracks ? '?include_tracks=1' : '';
    return this.request(`/api/hybrid-playlists${q}`);
  }

  async createHybridPlaylist(name, description = '', tracks = []) {
    return this.request('/api/hybrid-playlists', {
      method: 'POST',
      body: JSON.stringify({ name, description, tracks }),
    });
  }

  async getHybridPlaylist(playlistId) {
    return this.request(`/api/hybrid-playlists/${encodeURIComponent(playlistId)}`);
  }

  async deleteHybridPlaylist(playlistId) {
    return this.request(`/api/hybrid-playlists/${encodeURIComponent(playlistId)}`, { method: 'DELETE' });
  }

  async addTracksToHybridPlaylist(playlistId, tracks) {
    return this.request(`/api/hybrid-playlists/${encodeURIComponent(playlistId)}/tracks`, {
      method: 'POST',
      body: JSON.stringify({ tracks }),
    });
  }

  async removeTracksFromHybridPlaylist(playlistId, trackKeys) {
    return this.request(`/api/hybrid-playlists/${encodeURIComponent(playlistId)}/tracks`, {
      method: 'DELETE',
      body: JSON.stringify({ track_keys: trackKeys }),
    });
  }

  async getTrackRoute(trackKey) {
    return this.request(`/api/tracks/route?track_key=${encodeURIComponent(trackKey)}`);
  }

  async saveTrackRoute(payload) {
    return this.request('/api/tracks/route', { method: 'PUT', body: JSON.stringify(payload) });
  }

  async deleteTrackRoute(trackKey) {
    return this.request(`/api/tracks/route?track_key=${encodeURIComponent(trackKey)}`, { method: 'DELETE' });
  }

  async getTrackBlocklist(trackKey) {
    return this.request(`/api/tracks/blocklist?track_key=${encodeURIComponent(trackKey)}`);
  }

  async blockTrackCandidate(trackKey, candidateKey, reason = '') {
    return this.request('/api/tracks/blocklist', {
      method: 'POST',
      body: JSON.stringify({ track_key: trackKey, candidate_key: candidateKey, reason }),
    });
  }

  async unblockTrackCandidate(trackKey, candidateKey) {
    return this.request('/api/tracks/blocklist', {
      method: 'DELETE',
      body: JSON.stringify({ track_key: trackKey, candidate_key: candidateKey }),
    });
  }

  // --- OpenSubsonic & WebDAV Protocols + WebDAV Encrypted Sync ---

  async getOpenSubsonicPlaylists() {
    return this.request('/api/opensubsonic/playlists');
  }

  async getOpenSubsonicPlaylist(playlistId) {
    return this.request(`/api/opensubsonic/playlists/${encodeURIComponent(playlistId)}`);
  }

  async searchOpenSubsonic(q = '', limit = 30, offset = 0) {
    return this.request(`/api/opensubsonic/search?q=${encodeURIComponent(q)}&limit=${limit}&offset=${offset}`);
  }

  async scanWebDav() {
    return this.request('/api/webdav/scan', { method: 'POST', body: JSON.stringify({}) });
  }

  async getWebDavTracks(q = '', limit = 100, offset = 0) {
    return this.request(`/api/webdav/tracks?q=${encodeURIComponent(q)}&limit=${limit}&offset=${offset}`);
  }

  async pushWebDavSync(passphrase) {
    return this.request('/api/webdav/sync/push', {
      method: 'POST',
      body: JSON.stringify({ passphrase }),
    });
  }

  async pullWebDavSync(passphrase, sinceSeq = 0) {
    return this.request('/api/webdav/sync/pull', {
      method: 'POST',
      body: JSON.stringify({ passphrase, since_seq: sinceSeq }),
    });
  }

  // --- HTTP Resolver v2 Registry, Subscriptions & Sidecar Bridge ---

  async getResolvers() {
    return this.request('/api/resolvers');
  }

  async saveResolver(payload) {
    return this.request('/api/resolvers', { method: 'POST', body: JSON.stringify(payload) });
  }

  async deleteResolver(resolverId) {
    return this.request(`/api/resolvers/${encodeURIComponent(resolverId)}`, { method: 'DELETE' });
  }

  async subscribeResolvers(url) {
    return this.request('/api/resolvers/subscribe', { method: 'POST', body: JSON.stringify({ url }) });
  }

  async probeResolver(url, apiKey = '') {
    return this.request('/api/resolvers/probe', { method: 'POST', body: JSON.stringify({ url, api_key: apiKey }) });
  }

  async getSidecarStatus() {
    return this.request('/api/sidecar/status');
  }

  async startSidecar(adapterPath = '', port = 0) {
    return this.request('/api/sidecar/start', {
      method: 'POST',
      body: JSON.stringify({ adapter_path: adapterPath, port }),
    });
  }

  async stopSidecar() {
    return this.request('/api/sidecar/stop', { method: 'POST', body: JSON.stringify({}) });
  }

  // --- Cross-Platform Playlist Export & Migration Assistant ---

  async exportPlaylist(playlistId, format = 'json') {
    return this.request(`/api/playlists/${encodeURIComponent(playlistId)}/export?format=${encodeURIComponent(format)}`);
  }

  async previewPlaylistMigration(content, format = 'auto', allowOnline = false) {
    return this.request('/api/playlists/migrate/preview', {
      method: 'POST',
      body: JSON.stringify({ content, format, allow_online: Boolean(allowOnline) }),
    });
  }

  async commitPlaylistMigration(payload) {
    return this.request('/api/playlists/migrate/commit', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }
}

export const api = new ApiClient();
