from __future__ import annotations

import unicodedata
from typing import Any

from .catalog_service import CatalogService
from .errors import AppError
from .models import Song
from .netease_client import NeteaseClient


def normalize_credit(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def is_strict_match(source: Song, candidate: Song, duration_tolerance_ms: int) -> bool:
    if normalize_credit(source.title) != normalize_credit(candidate.title):
        return False
    source_artist = normalize_credit(source.artists[0]) if source.artists else ""
    candidate_artist = normalize_credit(candidate.artists[0]) if candidate.artists else ""
    if not source_artist or source_artist != candidate_artist:
        return False
    if source.duration <= 0 or candidate.duration <= 0:
        return False
    return abs(source.duration - candidate.duration) <= duration_tolerance_ms


class AudioService:
    def __init__(self, client: NeteaseClient, catalog: CatalogService, duration_tolerance_ms: int = 5_000):
        self.client = client
        self.catalog = catalog
        self.duration_tolerance_ms = duration_tolerance_ms

    def resolve(self, song_id: int) -> dict[str, Any]:
        original = self.client.audio_url(song_id)
        if original:
            return {**original, "is_alternative": False, "source": "original", "alternative_info": None}
        source = self.catalog.song(song_id)
        if source is None:
            raise AppError("SONG_NOT_IN_CATALOG", "本地曲库中找不到这首歌", 404)
        query = " ".join([source.title, source.artists[0] if source.artists else ""]).strip()
        for candidate in self.client.search_songs(query):
            if candidate.id == source.id or not is_strict_match(source, candidate, self.duration_tolerance_ms):
                continue
            audio = self.client.audio_url(candidate.id)
            if audio:
                return {
                    **audio,
                    "is_alternative": True,
                    "source": "alternative",
                    "alternative_info": candidate.to_dict(),
                }
        raise AppError("AUDIO_UNAVAILABLE", "没有找到严格匹配的可播放音源", 404)
