from __future__ import annotations

import sys
from pathlib import Path

from .version import __version__, windows_version_tuple


def write_version_file(target: Path) -> None:
    version_tuple = windows_version_tuple()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple},
    prodvers={version_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', 'Pruner Studio contributors'),
        StringStruct('FileDescription', 'Pruner Studio local music organizer'),
        StringStruct('FileVersion', '{__version__}'),
        StringStruct('InternalName', 'Pruner-Studio'),
        StringStruct('LegalCopyright', 'MIT License'),
        StringStruct('OriginalFilename', 'Pruner-Studio.exe'),
        StringStruct('ProductName', 'Pruner Studio'),
        StringStruct('ProductVersion', '{__version__}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m pruner_studio.windows_version_info OUTPUT")
    write_version_file(Path(sys.argv[1]).resolve())
