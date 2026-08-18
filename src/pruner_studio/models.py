from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class AccountSummary:
    user_id: int
    display_name: str
    avatar_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlaylistSummary:
    id: int
    name: str
    track_count: int
    kind: str
    privacy: str

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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "requested_ids": self.requested_ids,
            "successful_ids": self.successful_ids,
            "failed_items": [item.to_dict() for item in self.failed_items],
            "unknown_items": [item.to_dict() for item in self.unknown_items],
            "message": self.message,
            "resource_id": self.resource_id,
        }
