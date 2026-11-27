"""Regression tests for startup parse errors, Range proxy sniffing, MIME healing, and cold-start audio lookup."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

from pruner_studio.app import build_application, create_asgi_app
from pruner_studio.config import Settings
from pruner_studio.external_sources import ExternalCandidate
from pruner_studio.models import PlaylistSummary, Song
from test_backend import FakeTransport


class _FragmentedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class PlaybackResumeAndStreamFixesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.transport = FakeTransport()
        self.app = build_application(Settings(self.root, self.root), self.transport)
        self.app.client.pacer.sleep_fn = lambda _: None
        self.http = self.enterContext(
            TestClient(create_asgi_app(self.app, 8765), base_url="http://127.0.0.1:8765")
        )

    def _pin_dns(self):
        return self.enterContext(
            patch(
                "pruner_studio.external_sources._resolve_addresses",
                return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
            )
        )

    def test_mid_file_range_with_octet_stream_allows_angle_brace_bracket_bytes(self):
        self._pin_dns()
        factory = httpx.AsyncClient
        for body in (b"<AUDIO_FRAME_1234", b"  {AUDIO_FRAME_5678", b"\n[AUDIO_FRAME_9012"):
            with self.subTest(prefix=body[:4]):
                target = self.app.audio.register_stream_token(
                    session_generation=self.app.session_generation,
                    kind="remote",
                    url="https://cdn.test/song.flac",
                    content_type="audio/flac",
                    format_name="flac",
                    provider="netease",
                    cache_key="mid-range-test",
                )

                def upstream(_req: httpx.Request, payload: bytes = body) -> httpx.Response:
                    return httpx.Response(
                        206,
                        headers={
                            "content-type": "application/octet-stream",
                            "content-range": f"bytes 1769472-{1769472 + len(payload) - 1}/2000000",
                            "content-length": str(len(payload)),
                        },
                        content=payload,
                    )

                with patch(
                    "pruner_studio.app.httpx.AsyncClient",
                    side_effect=lambda **kw: factory(transport=httpx.MockTransport(upstream), **kw),
                ):
                    res = self.http.get(f"/api/stream/{target.token}", headers={"Range": "bytes=1769472-"})

                self.assertEqual(206, res.status_code, res.text)
                self.assertEqual(body, res.content)
                self.assertEqual("audio/flac", res.headers["content-type"].split(";")[0])

    def test_flac_mislabelled_as_audio_mpeg_is_healed_on_stream_and_cache(self):
        self._pin_dns()
        factory = httpx.AsyncClient
        flac_payload = b"fLaC\x80\x00\x00\x22" + b"\x00" * 64
        target = self.app.audio.register_stream_token(
            session_generation=self.app.session_generation,
            kind="remote",
            url="https://cdn.test/jyeffect.flac",
            content_type="audio/flac",
            format_name="flac",
            provider="netease",
            cache_key="netease:1897964061:jyeffect",
        )

        def upstream(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "audio/mpeg", "content-length": str(len(flac_payload))},
                content=flac_payload,
            )

        with patch(
            "pruner_studio.app.httpx.AsyncClient",
            side_effect=lambda **kw: factory(transport=httpx.MockTransport(upstream), **kw),
        ):
            res = self.http.get(f"/api/stream/{target.token}")

        self.assertEqual(200, res.status_code)
        self.assertEqual(flac_payload, res.content)
        self.assertEqual("audio/flac", res.headers["content-type"].split(";")[0])
        self.assertEqual("nosniff", res.headers["x-content-type-options"])

        cached = self.app.audio_cache.get("netease:1897964061:jyeffect")
        self.assertIsNotNone(cached)
        self.assertEqual("audio/flac", cached[1])

    def test_audio_disk_cache_self_heals_legacy_poisoned_mpeg_mime_for_flac(self):
        flac_payload = b"fLaC\x80\x00\x00\x22" + b"\x11" * 48
        cache_key = "netease:1897964061:jyeffect"
        key_hash = self.app.audio_cache._key_hash(cache_key)
        fname = f"{key_hash}.flac"
        (self.app.audio_cache.cache_dir / fname).write_bytes(flac_payload)
        index_path = self.app.audio_cache.cache_dir / "index.json"
        index_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": {
                        key_hash: {
                            "key": cache_key,
                            "filename": fname,
                            "content_type": "audio/mpeg",
                            "format": "flac",
                            "size_bytes": len(flac_payload),
                            "created_at": 1.0,
                            "last_accessed_at": 1.0,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        hit = self.app.audio_cache.get(cache_key)
        self.assertIsNotNone(hit)
        self.assertEqual("audio/flac", hit[1])
        repaired_index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual("audio/flac", repaired_index["entries"][key_hash]["content_type"])

    def test_fragmented_leading_chunks_sniff_mp4_ftyp_and_reject_disguised_html(self):
        self._pin_dns()
        factory = httpx.AsyncClient
        mp4_chunks = [b"\x00\x00", b"\x00\x18", b"ftyp", b"M4A ", b"\x00" * 32]
        target_ok = self.app.audio.register_stream_token(
            session_generation=self.app.session_generation,
            kind="remote",
            url="https://cdn.test/track.m4a",
            content_type="audio/mp4",
            format_name="m4a",
            provider="qq",
            cache_key="fragmented-mp4",
        )

        def upstream_ok(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                stream=_FragmentedStream(mp4_chunks),
            )

        with patch(
            "pruner_studio.app.httpx.AsyncClient",
            side_effect=lambda **kw: factory(transport=httpx.MockTransport(upstream_ok), **kw),
        ):
            res = self.http.get(f"/api/stream/{target_ok.token}")

        self.assertEqual(200, res.status_code)
        self.assertEqual(b"".join(mp4_chunks), res.content)
        self.assertEqual("audio/mp4", res.headers["content-type"].split(";")[0])

        # HTML error page disguised as audio/mpeg at byte 0 MUST still be rejected with 502!
        target_bad = self.app.audio.register_stream_token(
            session_generation=self.app.session_generation,
            kind="remote",
            url="https://cdn.test/error.mp3",
            content_type="audio/mpeg",
            format_name="mp3",
            provider="netease",
            cache_key="disguised-html",
        )
        with patch(
            "pruner_studio.app.httpx.AsyncClient",
            side_effect=lambda **kw: factory(
                transport=httpx.MockTransport(
                    lambda _r: httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"<html>403 Forbidden</html>")
                ),
                **kw,
            ),
        ):
            res_bad = self.http.get(f"/api/stream/{target_bad.token}")

        self.assertEqual(502, res_bad.status_code)
        self.assertEqual("STREAM_UPSTREAM_INVALID", res_bad.json()["error"]["code"])
        self.assertEqual([], list(self.app.audio_cache.cache_dir.glob("*.part")))

    def test_categorized_upstream_http_errors_on_stream_proxy(self):
        self._pin_dns()
        factory = httpx.AsyncClient
        for status_code, expected_code in (
            (403, "STREAM_UPSTREAM_FORBIDDEN"),
            (404, "STREAM_UPSTREAM_NOT_FOUND"),
            (503, "STREAM_UPSTREAM_FAILED"),
        ):
            with self.subTest(upstream_status=status_code):
                target = self.app.audio.register_stream_token(
                    session_generation=self.app.session_generation,
                    kind="remote",
                    url=f"https://cdn.test/err-{status_code}.flac",
                    content_type="audio/flac",
                    format_name="flac",
                    provider="netease",
                )
                with patch(
                    "pruner_studio.app.httpx.AsyncClient",
                    side_effect=lambda **kw: factory(
                        transport=httpx.MockTransport(lambda _r, sc=status_code: httpx.Response(sc, content=b"err")),
                        **kw,
                    ),
                ):
                    res = self.http.get(f"/api/stream/{target.token}")
                self.assertEqual(502, res.status_code)
                self.assertEqual(expected_code, res.json()["error"]["code"])

    def test_cold_start_audio_resolution_uses_song_hint_and_account_fallback_without_404(self):
        # Simulate cold start: catalog._active_account_id is NOT set yet, NetEase original fails,
        # and the track is a restored Discover track NOT in local catalog, passed via title/artist/duration hints.
        self.app.catalog.set_active_account(None)
        self.transport.handlers["/song/url/v1"] = {"code": 200, "data": [{"id": 1897964061, "url": None}]}
        self.transport.handlers["/cloudsearch"] = {"code": 200, "result": {"songs": []}}
        self.transport.handlers["/song/detail"] = {"code": 200, "songs": []}

        resolved_candidate = ExternalCandidate(
            title="雨不停。流",
            artists=["孙盛希"],
            album="OST",
            duration_ms=265935,
            source_provider="migu",
            provider_track_id="mg_1",
            url="https://cdn.migu.test/rain.flac",
            format="flac",
            bitrate=950000,
            match_score=0.98,
            strict_match=True,
        )
        with patch.object(self.app.audio.external_resolver, "resolve", return_value=resolved_candidate):
            res = self.http.get(
                "/api/audio",
                params={
                    "id": "1897964061",
                    "title": "雨不停。流",
                    "artist": "孙盛希",
                    "duration": "265935",
                    "allow_external": "1",
                },
            )

        self.assertEqual(200, res.status_code, res.text)
        data = res.json()["data"]
        self.assertEqual("external", data["source"])
        self.assertEqual("migu", data["source_provider"])

        # Also verify account-stored song is found even if catalog._active_account_id is still None on cold start
        # when session.active_account_id is set.
        song_in_db = Song(id=555001, title="Cached Cold Track", artists=["Artist"], duration=210000)
        self.app.db.replace_playlist_tracks(
            "777",
            PlaylistSummary(10, "Liked", 1, "liked", "public"),
            [song_in_db],
        )
        found = self.app.audio._lookup_song(555001, account_id="777")
        self.assertIsNotNone(found)
        self.assertEqual("Cached Cold Track", found.title)


if __name__ == "__main__":
    unittest.main()
