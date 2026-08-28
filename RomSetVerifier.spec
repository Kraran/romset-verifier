# -*- mode: python ; coding: utf-8 -*-
# Build Windows:  python -m PyInstaller --noconfirm RomSetVerifier.spec
from pathlib import Path

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "_ui.html"), "."),
    (str(ROOT / "i18n"), "i18n"),
]
for name in (
    "icon.png", "icon-32.png", "icon-256.png", "icon-512.png",
    "favicon.ico", "about-author.jpg",
):
    p = ROOT / name
    if p.is_file():
        datas.append((str(p), "."))

icon = None
for cand in ("favicon.ico", "icon.ico", "icon.png"):
    if (ROOT / cand).is_file():
        icon = str(ROOT / cand)
        break

a = Analysis(
    ["rom_verifier.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "lxml",
        "lxml.etree",
        "lxml._elementpath",
        "flask",
        "jinja2",
        "werkzeug",
        "click",
        "itsdangerous",
        "markupsafe",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas"],
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
    name="RomSetVerifier",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
