# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

project_root = Path(SPECPATH)

datas = [(str(project_root / 'web'), 'web')]
binaries = []
hiddenimports = [
    'qrcode.image.pil',
    'uvicorn',
    'uvicorn.logging',
    'uvicorn.loops.auto',
    'uvicorn.loops.asyncio',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan.on',
    'uvicorn.lifespan.off',
    'starlette',
    'anyio._backends._asyncio',
    'httpx',
    'httpx._transports.default',
    'Crypto.Cipher.AES',
    'Crypto.Hash.SHA256',
    'Crypto.PublicKey.ECC',
    'Crypto.PublicKey._nist_ecc',
    'Crypto.PublicKey._ec_lib',
    'Crypto.PublicKey._edwards',
    'Crypto.PublicKey._montgomery',
    'Crypto.Protocol.KDF',
    'Crypto.Signature.DSS',
    'Crypto.Util.Padding',
    'Crypto.Util.asn1',
    'pruner_studio.lan_sync',
    'pruner_studio.lan_tls',
    'pruner_studio.track_identity',
    'pruner_studio.local_metadata',
    'pruner_studio.open_protocols',
    'pruner_studio.resolver_v2',
    'pruner_studio.sidecar_bridge',
    'pruner_studio.sidecar_runner',
    'pruner_studio.playlist_migration',
    'webview',
    'webview.platforms.edgechromium',
    'webview.platforms.winforms',
    'clr',
    'pythonnet',
    'clr_loader',
    'bottle',
    'proxy_tools',
    'cffi',
    '_cffi_backend',
    'pystray',
    'pystray._win32',
]

for pkg_name in ('webview', 'pythonnet', 'clr_loader'):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg_name)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += [
        mod for mod in pkg_hiddenimports
        if '._pyinstaller' not in mod and '.__pyinstaller' not in mod
    ]

datas = list(dict.fromkeys(datas))
binaries = list(dict.fromkeys(binaries))
hiddenimports = list(dict.fromkeys(hiddenimports))


a = Analysis(
    [str(project_root / 'scripts' / 'pruner_studio_entry.py')],
    pathex=[str(project_root / 'src')],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Pruner-Studio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(project_root / 'web' / 'assets' / 'icon.ico'),
    manifest=str(project_root / 'build' / 'Pruner-Studio.exe.manifest'),
    version=str(project_root / 'build' / 'windows-version-info.txt'),
)
