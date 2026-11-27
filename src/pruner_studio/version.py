"""Canonical Pruner Studio release version."""

__version__ = "0.4.0"


def windows_version_tuple() -> tuple[int, int, int, int]:
    parts = [int(value) for value in __version__.split(".")]
    if len(parts) > 4:
        raise ValueError("Windows file versions support at most four numeric parts")
    padded = (parts + [0] * 4)[:4]
    return padded[0], padded[1], padded[2], padded[3]
