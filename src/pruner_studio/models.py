from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class AccountSummary:
    user_id: int
    display_name: str
    avatar_url: str = ""
    vip_type: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlaylistSummary:
    id: int | str
    name: str
    track_count: int
    kind: str
    privacy: str
    cover_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Song:
    id: int
    title: str
    artists: list[str]
    album: str = ""
    duration: int = 0
    cover_url: str = ""
    source_playlist_id: int | str | None = None
    availability: str = "unknown"
    chorus_start: int | None = None
    chorus_end: int | None = None
    fee: int | None = None
    privilege_st: int | None = None
    max_br_level: str = ""
    is_cloud: bool = False
    source_playlist_name: str = ""
    artist_ids: list[int] = field(default_factory=list)
    provider: str = "netease"
    external_id: str = ""
    track_key: str = ""
    isrc: str = ""
    mbid: str = ""
    local_path: str = ""

    def resolved_track_key(self) -> str:
        if self.track_key:
            return self.track_key
        if self.provider and self.provider != "netease" and self.external_id:
            return f"{self.provider}:{self.external_id}"
        if self.local_path:
            return f"local:{self.local_path}"
        if self.id and int(self.id) > 0:
            return f"netease:{int(self.id)}"
        return ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        tkey = self.resolved_track_key()
        data["track_key"] = tkey
        if not data["external_id"]:
            if data["provider"] == "netease" and self.id > 0:
                data["external_id"] = str(self.id)
            elif self.local_path:
                data["external_id"] = self.local_path
        data["duration_ms"] = self.duration
        return data


@dataclass(frozen=True)
class FailedItem:
    id: int | None
    code: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    requested_ids: list[int] = field(default_factory=list)
    successful_ids: list[int] = field(default_factory=list)
    failed_items: list[FailedItem] = field(default_factory=list)
    message: str = ""
    resource_id: int | None = None
    unknown_items: list[FailedItem] = field(default_factory=list)
    pending_operation_ids: list[str] = field(default_factory=list)
    cooldown_sec: float = 0.0
    backup_id: str | None = None
    checksum: str | None = None
    snapshot_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "requested_ids": self.requested_ids,
            "successful_ids": self.successful_ids,
            "confirmed_ids": self.successful_ids,
            **({"cooldown_sec": self.cooldown_sec} if self.cooldown_sec else {}),
            **({"backup_id": self.backup_id, "checksum": self.checksum, "snapshot_path": self.snapshot_path}
               if self.backup_id else {}),
            "failed_items": [item.to_dict() for item in self.failed_items],
            "unknown_items": [item.to_dict() for item in self.unknown_items],
            "message": self.message,
            "resource_id": self.resource_id,
            "pending_operation_ids": self.pending_operation_ids,
        }
