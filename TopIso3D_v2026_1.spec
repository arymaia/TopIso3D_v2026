# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['TopIso3D_v2026_1.py'],
    pathex=[],
    binaries=[],
    datas=[('topiso3d.iconset', 'topiso3d.iconset')],
    hiddenimports=['xlsxwriter'],
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
    name='TopIso3D_v2026_1',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
