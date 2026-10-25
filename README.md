# RomSet Verifier

**Version 1.1.0-beta**

Local ROM set auditor for **RetroBat**, **Batocera** and **Recalbox** collections.

Verify, repair and maintain No-Intro / Redump / MAME / FBNeo sets with a dark UI inspired by desktop ROM managers.

> **Beta software.** Core scan & repair are usable daily; some tools (ZIP recompression, edge-case arcade sets) still need real-world feedback. Report issues on GitHub.

---

## Features

- **No-Intro / Redump** — CRC-32 from ZIP central directory or `.7z` header (fast, no full decompress)
- **Track sheets** — renaming a file updates the `.cue` / `.gdi` / `.m3u` next to it that reference the old name
- **MAME / FBNeo (non-merged)** — full ZIP audit, clone/parent, BIOS, driver status
- **CHD / MAMERedump** — SHA-1 from CHD headers when needed
- **Repair** — rename internal members, rebuild ZIPs, borrow missing ROMs from other archives
- **Profiles** — multi-system RetroBat-style collections (`es_systems.cfg`)
- **DAT hub** — Redump straight from **redump.org** (official, one DAT per system); No-Intro from a GitHub mirror (its last publication date is shown) or **import the official DAT-o-MATIC daily pack** you downloaded; MAME / FBNeo packs; filtered MAME `listxml`. Updating a DAT sets the older version aside (`cache/superseded_dats/`) and points profiles to the new one
- **Tools** — max DEFLATE ZIP recompression (no 7-Zip required)
- **Interface** — same look as [Gamelist Media Editor](https://github.com/Kraran/gamelist-media-editor): one large results table (status, game, region, file found ZIP → ROM, size, DAT vs file CRC, plain-language detail, per-row actions), completion meter, keyboard shortcuts; fonts and icons embedded (works offline)
- **i18n** — Français, English, Español, Deutsch, Italiano, Português, Nederlands, Polski, Türkçe, Svenska, Norsk, Dansk, Русский

---

## Requirements

- **Windows 10/11** (primary target) — Linux/macOS should work for the Python server
- **Python 3.10+**
- Dependencies: `flask`, `lxml`, optional `py7zr` for `.7z` (see `requirements.txt`)
- Optional: **Node.js LTS** + npm for Electron shell

---

## Quick start (Windows)

1. Install [Python 3](https://www.python.org/downloads/) — enable **Add python.exe to PATH**
2. Copy this folder wherever you like
3. Double-click **`Lancer.bat`** (or **`RomSet Verifier.bat`**)
   - Installs `flask` / `lxml` on first run if needed
   - Opens a dedicated **application window** (`--app=` with Edge / Chrome / Brave: no tabs, no address bar)
   - Falls back to the default browser if only Firefox is installed
   - Closing the window or **Quit** stops the local server
4. Load a DAT → choose a ROMs folder → **Scan**

### Manual start

```bash
pip install -r requirements.txt
python rom_verifier.py --open
```

---

## Typical workflow

1. **Bases DAT** (Tools menu) — download the DAT pack you need, or point to a local `.dat` / MAME `.xml`
2. **Scan** a ROMs folder (optional: subfolders for multi-disc / CHD layouts)
3. Filter by status: Good / Rename / Bad / Missing / Incomplete
4. **Repair** selection (or repair all Rename) — then **rescan** to confirm
5. Optional: **Collection** tab — create a RetroBat profile and batch-scan systems

---

## Important limitations (read before filing issues)

| Topic | Behaviour |
|--------|-----------|
| MAME sets | **Non-merged only** (one ZIP per game with all ROMs) |
| Hashes | CRC-32 for standard/arcade ROMs; SHA-1 for CHD / some Redump-style DATs |
| ZIP / 7z content | Fast scan trusts the CRC stored in the archive header; tick **Deep check** to decompress every file and detect corrupted data (much slower) |
| Large loose files (≥ 64 MB: ISO, BIN…) | **ISO name + size** (checked by default): never read in a fast scan — identified from the DAT by exact name + size, then relaxed name (case, accents, apostrophes, dashes), then a unique name prefix among dumps of that size (truncated Windows names), then a size unique in the DAT. Anything else stays *not identified — CRC not read* (never deleted by *Delete all Bad*). The CRC shown is the DAT's, not a computed one. Uncheck it (or use **Deep check**) for a bit-exact CRC: large files are then read one at a time, sequentially, in 32 MB blocks |
| CRC cache (loose files ≥ 1 MB) | Every CRC / SHA-1 actually computed is stored in `cache/hash_cache.json` and reused while the file keeps the same size and modification date — it takes precedence over the name + size match. **Deep check** ignores and refreshes it; *Tools → CRC cache* clears it |
| Scan progress | Progress bar with the current phase, the files being read (with their own %), throughput and estimated time left, plus a **Cancel** button. Large files (≥ 64 MB) are read **one at a time** — on a HDD or NAS, parallel reads turn into random I/O (seek storm) |
| Merged / split MAME | Not supported |
| `.7z` archives | Read-only, No-Intro / Redump mode (needs `py7zr`, installed by the launchers): archive can be renamed, not files inside. Arcade sets: `.zip` only |
| `.rar` archives | Not supported: reported as errors in the scan |
| Network drives (NAS) | Supported but slower; close Explorer/emulators before repair/rezip |
| ZIP recompress | Rewrites every `.zip` in the folder (backup recommended on first try) |
| Security | Server binds to **127.0.0.1** only; requests whose `Host` or `Origin` is not local are refused (protects against DNS rebinding and cross-site requests) |
| Concurrency | One operation at a time (scan, repair, delete, rezip); actions on results from an older scan are refused — rescan |
| Deleted files | Never erased: moved to `.rsv_trash/<date>/` inside the ROMs folder (empty it yourself once checked) |
| Repair | Files not in the DAT are kept in the ZIP; if a wrong file must be removed, the original ZIP is copied to `.rsv_trash/` first |

---

## Project layout

```
romset-verifier/
├── Lancer.bat          # Windows launcher
├── Lancer.sh           # Unix helper (if present)
├── rom_verifier.py     # Flask backend + scanner/repair
├── _ui.html            # Front-end (embedded by the server)
├── requirements.txt
├── package.json        # Optional Electron shell
├── main.js / preload.js
├── i18n/               # UI translations
├── fonts/              # Embedded UI fonts (IBM Plex Sans, Saira Condensed — OFL)
├── dat/                # Downloaded / generated DAT packs
├── roms/               # Default ROMs folder (optional)
└── profiles/           # Saved collection profiles
```

---

## Development

```bash
pip install -r requirements.txt
python rom_verifier.py --port 8080 --open
```

No build step for the web UI: edit `_ui.html` and reload.

Tests:

```bash
pip install pytest
pytest tests/
```

Electron (optional):

```bash
npm install
npm start
```

---

## Contributing

This is a **beta**. Useful contributions:

- Bug reports with **DAT type**, **log snippet** (`repair.log` if relevant), and steps to reproduce
- i18n fixes / missing keys
- Tests around DAT parsing and repair (especially duplicate CRC cases)

Please do **not** open issues asking for illegal ROM sources or copyrighted DAT redistributions beyond what the official projects already allow you to download yourself.

---

## Licence

MIT — see [LICENSE](LICENSE).

RomSet Verifier does **not** ship ROMs or complete commercial DAT databases. DAT downloads use public project endpoints (No-Intro, Redump mirrors, MAME/FBNeo sources) under their respective terms.

---

## Credits

- Built for RetroBat / Batocera / Recalbox users
- DAT ecosystems: [No-Intro](https://no-intro.org), [Redump](http://redump.org), [MAME](https://www.mamedev.org), [FBNeo](https://github.com/finalburnneo/FBNeo)
- Arcade titles enrichment: [Arcade Database (ADB)](http://adb.arcadeitalia.net)
- Interface assets: Tabler Icons (MIT), IBM Plex Sans and Saira Condensed (OFL) — see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
