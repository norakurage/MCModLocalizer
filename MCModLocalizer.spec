# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_all

datas = [('app/assets', 'assets')]
binaries = []
hiddenimports = []

# Flet 0.28+ ではデスクトップランタイムが別パッケージ flet_desktop に分離され、
# Flutter クライアント本体（flet_desktop/app）を同梱しないと、実行時に
# ModuleNotFoundError: No module named 'flet_desktop' で起動に失敗する。
for _pkg in ("flet", "flet_desktop"):
    _datas, _binaries, _hiddenimports = collect_all(_pkg)
    datas += _datas
    binaries += _binaries
    hiddenimports += _hiddenimports

a = Analysis(
    ['app/main.py'],
    pathex=[],
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
    name='MCModLocalizer',
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

if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='MCModLocalizer.app',
        icon=None,
        bundle_identifier='com.norakurage.MCModLocalizer',
    )
