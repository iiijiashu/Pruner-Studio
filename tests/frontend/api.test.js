/**
 * Pruner Studio Frontend Tests - API Client
 * Tests API envelope verification, HTTP status authority, and required routes:
 * POST /api/playlists/{playlist_id}/remove-tracks with source context
 * POST /api/operations/reconcile with action, target, source, and song IDs
 */
import assert from 'node:assert/strict';
import { state } from '../../web/js/state.js';
import { api } from '../../web/js/api.js';

export async function runApiTests() {
  console.log('Testing api.js...');

  const originalFetch = globalThis.fetch;

  try {
    // 1. Successful response envelope
    globalThis.fetch = async (url, options) => {
      return {
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({
          ok: true,
          data: { authenticated: true, user: { id: 100, display_name: 'Test' } },
          error: null,
        }),
      };
    };

    const statusRes = await api.getStatus();
    assert.equal(statusRes.ok, true);
    assert.equal(statusRes.data.authenticated, true);
    assert.equal(statusRes.data.user.id, 100);
    assert.equal(statusRes.error, null);

    // 2. Structured Failure Response
    globalThis.fetch = async (url, options) => {
      return {
        ok: false,
        status: 400,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({
          ok: false,
          data: null,
          error: { code: 'INVALID_SONG_ID', message: '歌曲 ID 不合法。' },
        }),
      };
    };

    const failRes = await api.unlike([999999], 777);
    assert.equal(failRes.ok, false);
    assert.equal(failRes.data, null);
    assert.equal(failRes.error.code, 'INVALID_SONG_ID');

    // 3. HTTP status is authoritative even if malformed payload says ok:true
    globalThis.fetch = async () => ({
      ok: false,
      status: 500,
      headers: new Map([['content-type', 'application/json']]),
      json: async () => ({ ok: true, data: { unsafe: true }, error: null }),
    });
    const inconsistentRes = await api.health();
    assert.equal(inconsistentRes.ok, false);
    assert.equal(inconsistentRes.data, null);
    assert.equal(inconsistentRes.error.code, 'HTTP_ERROR');

    // A 2xx JSON object without the required envelope is a protocol failure,
    // never an implicit success.
    globalThis.fetch = async () => ({
      ok: true,
      status: 200,
      headers: new Map([['content-type', 'application/json']]),
      json: async () => ({ unexpected: true }),
    });
    const malformedEnvelope = await api.health();
    assert.equal(malformedEnvelope.ok, false);
    assert.equal(malformedEnvelope.data, null);
    assert.equal(malformedEnvelope.error.code, 'API_CONTRACT_INVALID');

    // 4. Network Transport Exception
    globalThis.fetch = async () => {
      throw new Error('Connection refused at 127.0.0.1:8000');
    };

    const netFailRes = await api.health();
    assert.equal(netFailRes.ok, false);
    assert.equal(netFailRes.data, null);
    assert.equal(netFailRes.error.code, 'NETWORK_ERROR');
    assert.ok(netFailRes.error.message.includes('Connection refused'));

    // 5. Test POST /api/playlists/{playlist_id}/remove-tracks
    let lastUrl = '';
    let lastMethod = '';
    let lastBody = null;

    globalThis.fetch = async (url, options) => {
      lastUrl = url;
      lastMethod = options.method || 'GET';
      lastBody = options.body ? JSON.parse(options.body) : null;
      return {
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ ok: true, data: { successful_ids: [101, 102] }, error: null }),
      };
    };

    const removeRes = await api.removeTracks(555, [101, 102]);
    assert.equal(removeRes.ok, true);
    assert.equal(lastUrl, '/api/playlists/555/remove-tracks');
    assert.equal(lastMethod, 'POST');
    assert.deepEqual(lastBody, { ...state.writeScope(), song_ids: [101, 102], source_playlist_id: 555 });

    // 6. Test POST /api/operations/reconcile
    const reconcileRes1 = await api.reconcileOperations('unlike', 777, [201, 202], 777);
    assert.equal(reconcileRes1.ok, true);
    assert.equal(lastUrl, '/api/operations/reconcile');
    assert.equal(lastMethod, 'POST');
    assert.deepEqual(lastBody, {
      action: 'unlike',
      playlist_id: 777,
      source_playlist_id: 777,
      song_ids: [201, 202],
    });

    const reconcileRes2 = await api.reconcileOperations('remove_tracks', 888, [301]);
    assert.equal(reconcileRes2.ok, true);
    assert.equal(lastUrl, '/api/operations/reconcile');
    assert.deepEqual(lastBody, {
      action: 'remove_tracks',
      playlist_id: 888,
      source_playlist_id: 888,
      song_ids: [301],
    });

    const pendingRes = await api.getPendingOperations();
    assert.equal(pendingRes.ok, true);
    assert.equal(lastUrl, '/api/operations/pending');
    const pendingReconcile = await api.reconcilePendingOperation('abc123');
    assert.equal(pendingReconcile.ok, true);
    assert.equal(lastUrl, '/api/operations/abc123/reconcile');
    assert.equal(lastMethod, 'POST');
    const pendingIgnore = await api.ignorePendingOperation('abc123');
    assert.equal(pendingIgnore.ok, true);
    assert.equal(lastUrl, '/api/operations/abc123/ignore');

    // Empty mutating requests still use JSON so cross-site simple requests
    // cannot reach loopback write routes without a CORS preflight.
    let emptyMutationHeaders = null;
    globalThis.fetch = async (_url, options) => {
      emptyMutationHeaders = options.headers;
      return {
        ok: true,
        status: 200,
        headers: new Map([['content-type', 'application/json']]),
        json: async () => ({ ok: true, data: {}, error: null }),
      };
    };
    await api.logout();
    assert.equal(emptyMutationHeaders['Content-Type'], 'application/json');

    // 8. Route does not fabricate success on 404 / 500
    globalThis.fetch = async () => ({
      ok: false,
      status: 404,
      headers: new Map([['content-type', 'application/json']]),
      json: async () => ({
        ok: false,
        data: null,
        error: { code: 'NOT_FOUND', message: 'API route unavailable' },
      }),
    });

    const notFoundRes = await api.removeTracks(999, [1]);
    assert.equal(notFoundRes.ok, false);
    assert.equal(notFoundRes.error.code, 'NOT_FOUND');

    console.log('✓ api.js tests passed!');
  } finally {
    globalThis.fetch = originalFetch;
  }
}
