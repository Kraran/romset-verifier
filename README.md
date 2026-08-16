# RomSet Verifier

**Version 1.0.0-beta**

Local ROM set auditor for **RetroBat**, **Batocera** and **Recalbox** collections.

Verify, repair and maintain No-Intro / Redump / MAME / FBNeo sets with a dark UI inspired by desktop ROM managers.

> **Beta software.** Core scan & repair are usable daily; some tools (ZIP recompression, edge-case arcade sets) still need real-world feedback. Report issues on GitHub.

---

## Features

- **No-Intro / Redump** — CRC-32 from ZIP central directory (fast, no full decompress)
- **MAME / FBNeo (non-merged)** — full ZIP audit, clone/parent, BIOS, driver status
- **CHD / MAMERedump** — SHA-1 from CHD headers when needed
- **Repair** — rename internal members, rebuild ZIPs, borrow missing ROMs from other archives
- **Profiles** — multi-system RetroBat-style collections (`es_systems.cfg`)
- **DAT hub** — download No-Intro / Redump / MAME / FBNeo packs; generate filtered MAME `listxml`
- **Tools** — max DEFLATE ZIP recompression (no 7-Zip required)
- **i18n** — Français, English, Español, Deutsch, Italiano, Português, Nederlands, Polski, Türkçe, Svenska, Norsk, Dansk, Русский

---

## Requirements

- **Windows 10/11** (primary target) — Linux/macOS should work for the Python server
- **Python 3.10+**
- Dependencies: `flask`, `lxml` (see `requirements.txt`)
- Optional: **Node.js LTS** + npm for Electron shell

---

## Quick start (Windows)

1. Install [Python 3](https://www.python.org/downloads/) — enable **Add python.exe to PATH**
2. Copy this folder wherever you like
3. Double-click **`Lancer.bat`**
   - Installs `flask` / `lxml` on first run if needed
   - Starts **Electron** if Node.js is available, otherwise opens **http://127.0.0.1:8080** in your browser
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
| Merged / split MAME | Not supported |
| Network drives (NAS) | Supported but slower; close Explorer/emulators before repair/rezip |
| ZIP recompress | Rewrites every `.zip` in the folder (backup recommended on first try) |
| Security | Server binds to **127.0.0.1** only (local machine) |

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
