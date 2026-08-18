/**
 * Pruner Studio - API Client
 * Strict implementation of API envelope and error contract.
 */

class ApiClient {
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

    if (options.body && typeof options.body === 'string') {
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
      let payload;

      const contentType = response.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {
        payload = await response.json();
      } else {
        const text = await response.text();
        try {
          payload = JSON.parse(text);
        } catch {
          payload = {
            ok: response.ok,
            data: text,
            error: response.ok ? null : { code: 'HTTP_ERROR', message: `服务器返回状态码 ${response.status}` },
          };
        }
      }

      if (payload && typeof payload === 'object' && 'ok' in payload) {
        if (!response.ok) {
          return {
            ok: false,
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
        ok: response.ok,
        data: payload,
        error: response.ok ? null : { code: 'HTTP_ERROR', message: `HTTP ${response.status}` },
      };
    } catch (err) {
      return {
        ok: false,
        data: null,
        error: {
          code: 'NETWORK_ERROR',
          message: err instanceof Error ? err.message : '网络请求失败',
        },
      };
    }
  }

  // Auth & System
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

  async logout() {
    return this.request('/api/auth/logout', { method: 'POST' });
  }

  // Playlists & Catalog
  async getPlaylists() {
    return this.request('/api/playlists');
  }

  async syncPlaylist(playlistId) {
    return this.request(`/api/playlists/${encodeURIComponent(playlistId)}/sync`, {
      method: 'POST',
    });
  }

  async getCatalog() {
    return this.request('/api/catalog');
  }

  async importCatalog(format, content) {
    return this.request('/api/catalog/import', {
      method: 'POST',
      body: JSON.stringify({ format, content }),
    });
  }

  // Media
  async getLyrics(songId) {
    return this.request(`/api/lyrics?id=${encodeURIComponent(songId)}`);
  }

  async getAudio(songId) {
    return this.request(`/api/audio?id=${encodeURIComponent(songId)}`);
  }

  // Rules
  async getRules() {
    return this.request('/api/rules');
  }

  async saveRules(rules) {
    return this.request('/api/rules', {
      method: 'PUT',
      body: JSON.stringify({ rules }),
    });
  }

  // Operations
  async backup(params = {}) {
    return this.request('/api/operations/backup', {
      method: 'POST',
      body: JSON.stringify(params),
    });
  }

  async createPlaylist(name, songIds, privacy = 0) {
    return this.request('/api/operations/create-playlist', {
      method: 'POST',
      body: JSON.stringify({
        name,
        song_ids: songIds,
        privacy,
      }),
    });
  }

  /**
   * Unlike songs from liked music playlist.
   * @param {Array<number|string>} songIds
   * @returns {Promise<{ ok: boolean, data: { operation_id: string, requested_ids: number[], successful_ids: number[], failed_items: Array<{id: number, reason: string}>, message: string } | null, error: any }>}
   */
  async unlike(songIds, sourcePlaylistId) {
    return this.request('/api/operations/unlike', {
      method: 'POST',
      body: JSON.stringify({
        song_ids: songIds,
        source_playlist_id: sourcePlaylistId,
      }),
    });
  }

  /**
   * Remove tracks from an owned playlist.
   * @param {number|string} playlistId
   * @param {Array<number|string>} songIds
   * @returns {Promise<{ ok: boolean, data: { operation_id?: string, requested_ids?: number[], successful_ids?: number[], failed_items?: Array<{id: number, reason: string}>, message?: string } | null, error: any }>}
   */
  async removeTracks(playlistId, songIds, sourcePlaylistId = playlistId) {
    return this.request(`/api/playlists/${encodeURIComponent(playlistId)}/remove-tracks`, {
      method: 'POST',
      body: JSON.stringify({
        song_ids: songIds,
        source_playlist_id: sourcePlaylistId,
      }),
    });
  }

  /**
   * Reconcile operations when remote result is unknown or in doubt.
   * @param {'unlike'|'remove_tracks'} action
   * @param {number|string|null} playlistId
   * @param {Array<number|string>} songIds
   * @returns {Promise<{ ok: boolean, data: any, error: any }>}
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
}

export const api = new ApiClient();
