#!/usr/bin/env python3
"""
RomSet Verifier — vérification de romset à partir d'un DAT/XML
Interface locale moderne (liste + détail), support loose + ZIP.
"""

from __future__ import annotations

import functools
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_local_site = Path.home() / ".local" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
if _local_site.exists():
    sys.path.insert(0, str(_local_site))

from flask import Flask, jsonify, render_template_string, request, send_file
from lxml import etree

try:  # optionnel : lecture des archives .7z (CRC lus dans l'en-tête, sans décompresser)
    import py7zr
except ImportError:  # pragma: no cover - dépend de l'installation
    py7zr = None

if getattr(sys, "frozen", False):
    # PyInstaller : ressources packagées dans _MEIPASS, données user à côté de l'exe
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    SCRIPT_DIR = BUNDLE_DIR


def _asset(*parts: str) -> Path:
    """Fichier livré avec l'app (_ui.html, i18n, icônes)."""
    return BUNDLE_DIR.joinpath(*parts)

APP_VERSION = "1.1.0-beta"
APP_NAME = "RomSet Verifier"
# Dernière version MAME connue hors ligne (la liste en ligne via l'API GitHub la complète)
MAME_LATEST_KNOWN = 289
DEFAULT_DAT_DIR = SCRIPT_DIR / "dat"
DEFAULT_ROMS_DIR = SCRIPT_DIR / "roms"
CACHE_DIR = SCRIPT_DIR / "cache"

# Packs officiels (miroir GitHub, rebuild quotidien) — source DAT-o-MATIC No-Intro / Redump
DAT_PACKS = {
    "nointro": {
        "label": "No-Intro",
        "xml": "https://github.com/hugo19941994/auto-datfile-generator/releases/latest/download/no-intro.xml",
        "zip": "https://github.com/hugo19941994/auto-datfile-generator/releases/latest/download/no-intro.zip",
        "subdir": "nointro",
        "kind": "flat",
    },
    "redump": {
        "label": "Redump",
        "xml": "https://github.com/hugo19941994/auto-datfile-generator/releases/latest/download/redump.xml",
        "zip": "https://github.com/hugo19941994/auto-datfile-generator/releases/latest/download/redump.zip",
        "subdir": "redump",
        "kind": "flat",
    },
    "mameredump": {
        "label": "MAMERedump",
        "zip": "https://github.com/MetalSlug/MAMERedump/archive/refs/heads/main.zip",
        "subdir": "mameredump",
        "kind": "github_archive",
        "include_prefixes": ("MAME Redump/",),
        "keep_structure": True,
    },
    "mame": {
        "label": "MAME",
        "zip": "https://github.com/AntoPISA/MAME_Dats/archive/refs/heads/main.zip",
        "subdir": "mame",
        "kind": "github_archive",
        "include_prefixes": ("MAME_dat/", "hash/", "ARCADE_dat/"),
        "keep_structure": True,
    },
    "fbneo": {
        "label": "FBNeo",
        # DAT ClrMamePro dans le fork libretro (dossier dats/)
        "zip": "https://github.com/libretro/FBNeo/archive/refs/heads/master.zip",
        "subdir": "fbneo",
        "kind": "github_archive",
        "include_prefixes": ("dats/", "dats"),
        "keep_structure": False,
    },
}

UA = f"RomSetVerifier/{APP_VERSION} (+https://github.com/Kraran/romset-verifier)"

PROFILES_DIR = SCRIPT_DIR / "profiles"


def ensure_folders() -> None:
    DEFAULT_DAT_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_ROMS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    for p in DAT_PACKS.values():
        (DEFAULT_DAT_DIR / p["subdir"]).mkdir(parents=True, exist_ok=True)

# Taille de buffer lecture (8 Mio) — bon compromis gros fichiers / cache CPU
_READ_BUF = 8 * 1024 * 1024
# Parallelisme : I/O disque + zlib (libère le GIL sur gros blocs)

_MAX_WORKERS = min(32, (os.cpu_count() or 4) * 2)


def _chd_filename(name: str) -> str:
    """Nom de fichier CHD avec extension .chd unique (évite .chd.chd)."""
    n = (name or "").strip()
    if not n:
        return ""
    return n if n.lower().endswith(".chd") else (n + ".chd")


def read_chd_sha1(filepath: Path) -> Optional[str]:
    """
    Lit le SHA1 combiné (raw+meta) depuis l'en-tête CHD — instantané.
    MAME / MAMERedump utilisent ce SHA1 (pas le hash du fichier entier).
    V5: offset 84 ; V4: offset 48 ; V3: offset 80.
    """
    try:
        with open(filepath, "rb") as f:
            hdr = f.read(124)
        if len(hdr) < 16 or hdr[:8] != b"MComprHD":
            return None
        version = int.from_bytes(hdr[12:16], "big")
        if version == 5 and len(hdr) >= 104:
            return hdr[84:104].hex()
        if version == 4 and len(hdr) >= 68:
            return hdr[48:68].hex()
        if version == 3 and len(hdr) >= 100:
            return hdr[80:100].hex()
        return None
    except OSError:
        return None


def _sha1_fileobj(fileobj) -> tuple:
    """Retourne (sha1_hex, size) — hash contenu complet."""
    h = hashlib.sha1()
    size = 0
    read = fileobj.read
    while True:
        chunk = read(_READ_BUF)
        if not chunk:
            break
        size += len(chunk)
        h.update(chunk)
    return h.hexdigest(), size


class ScanCancelled(Exception):
    """Scan interrompu par l'utilisateur (bouton Annuler)."""


class _ScanProgress:
    """
    Avancement du scan en cours, lu par l'UI (GET /api/scan/progress) pendant que
    POST /api/scan travaille. Phases : list (inventaire), quick (cache / nom + taille),
    hash (lecture des fichiers), archives (ZIP / 7z), match (comparaison au DAT).
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.cancel_ev = threading.Event()
        self._reset()

    def _reset(self) -> None:
        self.running = False
        self.started = 0.0
        self.ph = ""
        self.ph_started = 0.0
        self.done = self.total = 0
        self.bytes_done = self.bytes_total = 0
        self.active: Dict[str, List[Any]] = {}  # chemin → [nom, octets lus, taille]
        self.counts = {"quick": 0, "cached": 0, "hashed": 0, "skipped": 0}

    def begin(self) -> None:
        with self.lock:
            self._reset()
            self.cancel_ev.clear()
            self.running = True
            self.started = time.time()

    def end(self) -> None:
        with self.lock:
            self.running = False
            self.active.clear()
            self.cancel_ev.clear()  # sinon une lecture hors scan (réparation…) serait « annulée »

    def phase(self, name: str, total: int = 0, bytes_total: int = 0) -> None:
        with self.lock:
            self.ph, self.ph_started = name, time.time()
            self.done, self.total = 0, int(total)
            self.bytes_done, self.bytes_total = 0, int(bytes_total)
            self.active.clear()

    def step(self, n: int = 1, kind: str = "") -> None:
        with self.lock:
            self.done += n
            if kind in self.counts:
                self.counts[kind] += n

    def file_start(self, path: str, name: str, size: int) -> None:
        with self.lock:
            self.active[path] = [name, 0, int(size)]

    def file_bytes(self, path: str, n: int) -> None:
        with self.lock:
            a = self.active.get(path)
            if a is not None:
                a[1] += n
                self.bytes_done += n

    def file_end(self, path: str) -> None:
        with self.lock:
            self.active.pop(path, None)

    def request_cancel(self) -> bool:
        with self.lock:
            if self.running:
                self.cancel_ev.set()
            return self.running

    def check(self) -> None:
        if self.cancel_ev.is_set():
            raise ScanCancelled("Scan annulé")

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            now = time.time()
            ph_elapsed = max(now - self.ph_started, 0.001) if self.ph_started else 0.0
            rate = self.bytes_done / ph_elapsed if self.ph == "hash" and ph_elapsed > 1 else 0.0
            eta = None
            if rate > 0 and self.bytes_total:
                eta = max(0, int((self.bytes_total - self.bytes_done) / rate))
            elif self.ph in ("quick", "archives") and self.done and self.total and ph_elapsed > 2:
                eta = int(ph_elapsed / self.done * (self.total - self.done))
            active = sorted(self.active.values(), key=lambda a: -a[2])[:4]
            return {
                "running": self.running,
                "cancelling": self.cancel_ev.is_set(),
                "phase": self.ph,
                "done": self.done, "total": self.total,
                "bytes_done": self.bytes_done, "bytes_total": self.bytes_total,
                "rate": int(rate), "eta": eta,
                "elapsed": int(now - self.started) if self.started else 0,
                "active": [{"name": a[0], "done": a[1], "size": a[2]} for a in active],
                "counts": dict(self.counts),
            }


SCAN_PROGRESS = _ScanProgress()


# Gros fichiers (contrôle bit-exact) : lus UN PAR UN, en gros blocs, lecture déclarée
# séquentielle à l'OS. Sur un disque dur ou un NAS, plusieurs lectures de 8 Go en
# parallèle font chercher la tête en permanence (seek storm) au lieu de streamer.
# Les petits fichiers restent parallélisés.
BIG_READ_MIN_SIZE = 64 * 1024 * 1024
BIG_READ_PARALLEL = 1
BIG_READ_BUF = 32 * 1024 * 1024


def _open_sequential(filepath):
    """Ouvre en lecture binaire avec l'indication « accès séquentiel » (Windows O_SEQUENTIAL, POSIX fadvise)."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_SEQUENTIAL", 0)
    fd = os.open(str(filepath), flags)
    if hasattr(os, "posix_fadvise"):
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
        except OSError:
            pass
    return os.fdopen(fd, "rb", buffering=0)


def _read_with_progress(filepath, update) -> int:
    """Lit un fichier par blocs ; update(chunk) ; avancement + annulation du scan. Retourne la taille."""
    key = str(filepath)
    size = 0
    with _open_sequential(filepath) as f:
        try:
            big = os.fstat(f.fileno()).st_size >= BIG_READ_MIN_SIZE
        except OSError:
            big = False
        buf = bytearray(BIG_READ_BUF if big else _READ_BUF)
        view = memoryview(buf)
        while True:
            n = f.readinto(buf)
            if not n:
                break
            update(view[:n])
            size += n
            SCAN_PROGRESS.file_bytes(key, n)
            SCAN_PROGRESS.check()
    return size


def _sha1_path(filepath: Path) -> tuple:
    h = hashlib.sha1()
    size = _read_with_progress(filepath, h.update)
    return h.hexdigest(), size



def _crc32_fileobj(fileobj) -> tuple:
    """Retourne (crc_int, size)."""
    crc = 0
    size = 0
    read = fileobj.read
    while True:
        chunk = read(_READ_BUF)
        if not chunk:
            break
        size += len(chunk)
        crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF, size


def _crc32_path(filepath: Path) -> tuple:
    """CRC32 fichier loose. Retourne (crc_int, size)."""
    state = [0]

    def upd(chunk):
        state[0] = zlib.crc32(chunk, state[0])
    size = _read_with_progress(filepath, upd)
    return state[0] & 0xFFFFFFFF, size


def _probe_dat_kind(dat_path: Path) -> str:
    """
    Identifie le type de DAT sans tout charger.
    Retourne: softwarelist | arcade | standard
    - softwarelist : racine <softwarelist>
    - arcade : listxml MAME (<mame>) ou machines avec ROMs CRC (set arcade)
    - standard : No-Intro / Redump / MAMERedump (disks SHA1, roms CRC, etc.)
    """
    saw_sourcefile = False
    n_machine = 0
    n_game = 0
    n_rom = 0
    n_disk = 0
    try:
        for _ev, el in etree.iterparse(
            str(dat_path),
            events=("start",),
            tag=("mame", "datafile", "softwarelist", "machine", "game", "rom", "disk", "software"),
            huge_tree=True,
            recover=True,
        ):
            tag = el.tag.split("}")[-1] if isinstance(el.tag, str) else el.tag
            if tag == "softwarelist":
                return "softwarelist"
            if tag == "mame":
                return "arcade"
            if tag == "machine":
                n_machine += 1
                if el.get("sourcefile"):
                    saw_sourcefile = True
            elif tag == "game":
                n_game += 1
            elif tag == "rom":
                n_rom += 1
            elif tag == "disk":
                n_disk += 1
            # Échantillon suffisant
            if (n_rom + n_disk) >= 40 or (n_machine + n_game) >= 80:
                break
    except Exception:
        pass

    # listxml exporté parfois en datafile mais avec sourcefile + roms CRC
    if saw_sourcefile and n_rom > 0:
        return "arcade"
    # MAMERedump / CHD sets : beaucoup de disks, peu ou pas de roms
    if n_disk > 0 and n_disk >= n_rom:
        return "standard"
    # Machines arcade classiques : roms CRC dominantes
    if n_rom > 0 and n_machine > n_game and n_machine >= 3:
        return "arcade"
    # FBNeo / ClrMamePro : <game> avec plusieurs ROMs par jeu (set zip non-merged)
    # Ex. FinalBurn Neo DAT : ~1 jeu = plusieurs rom crc
    n_sets = n_machine + n_game
    if n_rom > 0 and n_sets >= 3 and n_rom >= n_sets * 2:
        return "arcade"
    # Nom de fichier / chemin
    name_l = dat_path.name.lower()
    path_l = str(dat_path).lower().replace("\\", "/")
    markers = (
        "fbneo", "finalburn", "final burn", "fba_", "fba ",
        "arcade", "mame", "neogeo", "cps1", "cps2", "cps3",
    )
    if n_rom > 0 and n_sets >= 1 and any(m in name_l or m in path_l for m in markers):
        # Évite de classer un No-Intro "Arcade" fantôme : exige plusieurs roms/jeu en moyenne
        if n_sets >= 3 and n_rom >= n_sets:
            return "arcade"
        if "fbneo" in name_l or "fbneo" in path_l or "finalburn" in name_l or "finalburn" in path_l:
            return "arcade"
    return "standard"


def parse_dat(dat_path: Path) -> Tuple[Dict, Dict, List[Dict], set, str]:
    """
    Parse DAT No-Intro / Redump / MAMERedump / MAME listxml / MAME softwarelist.
    MAME listxml : racine <mame>, machines + ROMs CRC
    MAMERedump : <datafile><machine><disk sha1> — CHD, mode standard SHA1
    Softwarelist : racine <softwarelist>
    """
    kind = _probe_dat_kind(dat_path)
    if kind == "softwarelist":
        return _parse_softwarelist_dat(dat_path)
    if kind == "arcade":
        return _parse_mame_dat(dat_path)
    return _parse_standard_dat(dat_path)


def _parse_softwarelist_dat(dat_path: Path) -> Tuple[Dict, Dict, List[Dict], set, str]:
    """
    MAME software list :
      - Cartouches / ROM : <part><dataarea><rom crc size sha1>>
      - CHD / CD       : <part><diskarea><disk sha1>>

    Convention fichiers :
      - 1 ROM  → <software>.<ext> (ext du rom name) ou contenu ZIP <software>.zip
      - multi  → noms des roms tels que dans le softlist
      - 1 CHD  → <software>.chd
    """
    parser = etree.XMLParser(recover=True, huge_tree=True)
    tree = etree.parse(str(dat_path), parser=parser)
    root = tree.getroot()
    tag = root.tag.split("}")[-1] if isinstance(root.tag, str) else root.tag
    if tag != "softwarelist":
        sl = root.find(".//softwarelist")
        if sl is not None:
            root = sl
        else:
            raise ValueError("Pas un software list MAME (balise <softwarelist> attendue)")

    sl_name = root.get("name") or dat_path.stem
    sl_desc = root.get("description") or sl_name
    header: Dict[str, Any] = {
        "name": sl_name,
        "description": sl_desc,
        "dat_mode": "standard",
        "format": "softwarelist",
    }

    rom_map: Dict[Any, Dict] = {}
    games_list: List[Dict] = []
    size_set: set = set()
    n_crc = 0
    n_sha1_only = 0

    for soft in root.findall("software"):
        sname = (soft.get("name") or "").strip()
        if not sname:
            continue
        cloneof = (soft.get("cloneof") or "").strip()
        supported = (soft.get("supported") or "yes").lower()
        desc_el = soft.find("description")
        desc = (desc_el.text or "").strip() if desc_el is not None else sname
        year_el = soft.find("year")
        year = (year_el.text or "").strip() if year_el is not None else ""
        pub_el = soft.find("publisher")
        publisher = (pub_el.text or "").strip() if pub_el is not None else ""

        roms = []
        disks = []

        for part in soft.findall("part"):
            for darea in part.findall("dataarea"):
                for rom in darea.findall("rom"):
                    rname = (rom.get("name") or "").strip()
                    if not rname or rname.startswith("."):
                        continue
                    # ignore load flags without dump
                    if (rom.get("status") or "").lower() == "nodump":
                        continue
                    crc_s = (rom.get("crc") or "").lower().strip()
                    sha1 = (rom.get("sha1") or "").lower().strip()
                    try:
                        size = int(rom.get("size") or 0)
                    except ValueError:
                        size = 0
                    crc_i = -1
                    if crc_s:
                        try:
                            crc_i = int(crc_s, 16)
                        except ValueError:
                            crc_i = -1
                    if crc_i < 0 and not sha1:
                        continue
                    roms.append({
                        "name": rname,
                        "size": size,
                        "crc": crc_s,
                        "crc_int": crc_i,
                        "sha1": sha1,
                    })
            for darea in part.findall("diskarea"):
                for disk in darea.findall("disk"):
                    dname = (disk.get("name") or "").strip()
                    sha1 = (disk.get("sha1") or "").lower().strip()
                    if not sha1:
                        continue
                    disks.append({
                        "name": dname,
                        "sha1": sha1,
                        "status": (disk.get("status") or "good").lower(),
                    })

        # disks directs (rare)
        for disk in soft.findall("disk"):
            sha1 = (disk.get("sha1") or "").lower().strip()
            if sha1:
                disks.append({
                    "name": (disk.get("name") or "").strip(),
                    "sha1": sha1,
                    "status": (disk.get("status") or "good").lower(),
                })

        # --- ROMs (cartouches, etc.) ---
        if roms:
            for i, r in enumerate(roms):
                # 1 seule ROM : nom attendu = software + extension du dump
                # plusieurs : garder le nom MAME du rom (souvent dans un zip software.zip)
                if len(roms) == 1:
                    ext = Path(r["name"]).suffix or ".bin"
                    rom_name = sname + ext
                else:
                    rom_name = r["name"]
                entry = {
                    "game": sname,
                    "rom_name": rom_name,
                    "dump_name": r["name"],
                    "size": r["size"],
                    "crc": r["crc"],
                    "crc_int": r["crc_int"],
                    "md5": "",
                    "sha1": r["sha1"],
                    "sha256": "",
                    "is_disk": False,
                    "description": desc,
                    "cloneof": cloneof,
                    "year": year,
                    "publisher": publisher,
                    "supported": supported,
                }
                games_list.append(entry)
                if r["size"]:
                    size_set.add(r["size"])
                if r["crc_int"] >= 0:
                    rom_map[("crc", r["crc_int"], r["size"])] = entry
                    n_crc += 1
                elif r["sha1"]:
                    rom_map[("sha1", r["sha1"])] = entry
                    n_sha1_only += 1

        # --- CHD ---
        for i, d in enumerate(disks):
            if len(disks) == 1:
                rom_name = _chd_filename(sname)
            else:
                rom_name = _chd_filename(sname if i == 0 else f"{sname}-{i + 1}")
            entry = {
                "game": sname,
                "rom_name": rom_name,
                "disk_name": d.get("name") or "",
                "size": 0,
                "crc": "",
                "crc_int": -1,
                "md5": "",
                "sha1": d["sha1"],
                "sha256": "",
                "is_disk": True,
                "description": desc,
                "cloneof": cloneof,
                "year": year,
                "publisher": publisher,
                "supported": supported,
            }
            games_list.append(entry)
            rom_map[("sha1", d["sha1"])] = entry
            n_sha1_only += 1

    if not games_list:
        raise ValueError(
            f"Software list « {sl_name} » : aucune ROM (crc) ni disk (sha1) trouvé"
        )

    if n_crc > 0:
        hash_mode = "crc"
    elif n_sha1_only > 0:
        hash_mode = "sha1"
    else:
        hash_mode = "crc"

    header["hash_mode"] = hash_mode
    header["dat_mode"] = "standard"
    header["count"] = len(games_list)
    return header, rom_map, games_list, size_set, hash_mode


def _parse_standard_dat(dat_path: Path) -> Tuple[Dict, Dict, List[Dict], set, str]:
    parser = etree.XMLParser(recover=True, huge_tree=True, remove_blank_text=False)
    tree = etree.parse(str(dat_path), parser=parser)
    root = tree.getroot()
    header: Dict[str, str] = {}
    he = root.find("header")
    if he is not None:
        for child in he:
            if child.text:
                header[child.tag] = child.text

    rom_map: Dict[Any, Dict] = {}
    games_list: List[Dict] = []
    size_set: set = set()
    n_crc = 0
    n_sha1_only = 0

    games = list(root.findall("game")) + list(root.findall("machine"))
    for game in games:
        gname = game.get("name") or ""
        for rom in game.findall("rom"):
            rname = rom.get("name") or ""
            try:
                size = int(rom.get("size") or 0)
            except ValueError:
                size = 0
            crc_s = (rom.get("crc") or "").lower().strip()
            md5 = (rom.get("md5") or "").lower()
            sha1 = (rom.get("sha1") or "").lower().strip()
            sha256 = (rom.get("sha256") or "").lower()
            try:
                crc_i = int(crc_s, 16) if crc_s else -1
            except ValueError:
                crc_i = -1
            entry = {
                "game": gname, "rom_name": rname, "size": size,
                "crc": crc_s, "crc_int": crc_i, "md5": md5, "sha1": sha1,
                "sha256": sha256, "is_disk": False,
            }
            games_list.append(entry)
            if size:
                size_set.add(size)
            if crc_i >= 0:
                rom_map[("crc", crc_i, size)] = entry
                n_crc += 1
            elif sha1:
                rom_map[("sha1", sha1)] = entry
                n_sha1_only += 1
        # description machine (utile MAMERedump / affichage)
        desc_el = game.find("description")
        gdesc = (desc_el.text or "").strip() if desc_el is not None else gname
        for disk in game.findall("disk"):
            dname = disk.get("name") or ""
            rname = _chd_filename(dname) if dname else _chd_filename(gname)
            sha1 = (disk.get("sha1") or "").lower().strip()
            if not sha1:
                continue
            entry = {
                "game": gname,
                "rom_name": rname,
                "size": 0, "crc": "", "crc_int": -1, "md5": "",
                "sha1": sha1, "sha256": "", "is_disk": True,
                "description": gdesc,
            }
            games_list.append(entry)
            rom_map[("sha1", sha1)] = entry
            n_sha1_only += 1

    if n_crc > 0:
        hash_mode = "crc"
    elif n_sha1_only > 0:
        hash_mode = "sha1"
    else:
        hash_mode = "crc"

    header["hash_mode"] = hash_mode
    header["dat_mode"] = "standard"
    return header, rom_map, games_list, size_set, hash_mode


def _parse_mame_dat(dat_path: Path) -> Tuple[Dict, Dict, List[Dict], set, str]:
    """
    Parse MAME listxml ou DAT style machine.
    - Racine <mame build="..."> ou <datafile>
    - 1 entrée = 1 machine avec ROMs/CHD (devices vides ignorés)
    - Mode non-merged
    """
    header: Dict[str, str] = {
        "name": dat_path.stem,
        "description": dat_path.name,
    }
    machines: List[Dict] = []
    size_set: set = set()
    rom_map: Dict[Any, Dict] = {}

    # Attributs racine <mame>
    try:
        for _ev, el in etree.iterparse(str(dat_path), events=("start",), tag=("mame", "datafile"), huge_tree=True):
            if el.tag == "mame":
                build = el.get("build") or ""
                header["name"] = "MAME"
                if build:
                    header["description"] = f"MAME {build}"
                    header["version"] = build.split()[0] if build else ""
                header["build"] = build
            elif el.tag == "datafile":
                pass
            break
    except Exception:
        pass

    for _ev, el in etree.iterparse(
        str(dat_path), events=("end",), tag=("header", "machine", "game"), huge_tree=True
    ):
        tag = el.tag.split("}")[-1] if isinstance(el.tag, str) else el.tag
        if tag == "header":
            for child in el:
                if child.text:
                    header[child.tag] = child.text
            el.clear()
            continue

        if tag not in ("machine", "game"):
            el.clear()
            continue

        name = el.get("name") or ""
        if not name:
            el.clear()
            continue

        isbios = (el.get("isbios") or "").lower() in ("yes", "1", "true")
        isdevice = (el.get("isdevice") or "").lower() in ("yes", "1", "true")
        ismechanical = (el.get("ismechanical") or "").lower() in ("yes", "1", "true")
        runnable = (el.get("runnable") or "yes").lower() not in ("no", "0", "false")
        cloneof = el.get("cloneof") or ""
        romof = el.get("romof") or ""
        desc = el.findtext("description") or name
        year = el.findtext("year") or ""
        manufacturer = el.findtext("manufacturer") or ""

        driver_el = el.find("driver")
        driver_status = "good"
        if driver_el is not None:
            # status global MAME (good|imperfect|preliminary)
            driver_status = (driver_el.get("status") or driver_el.get("emulation") or "good").lower()

        roms = []
        for rom in el.findall("rom"):
            rname = rom.get("name") or ""
            try:
                size = int(rom.get("size") or 0)
            except ValueError:
                size = 0
            crc_s = (rom.get("crc") or "").lower().strip()
            sha1 = (rom.get("sha1") or "").lower().strip()
            merge = rom.get("merge") or ""
            rstatus = (rom.get("status") or "good").lower()
            optional = (rom.get("optional") or "").lower() in ("yes", "1", "true")
            try:
                crc_i = int(crc_s, 16) if crc_s else -1
            except ValueError:
                crc_i = -1
            roms.append({
                "name": rname, "size": size, "crc": crc_s, "crc_int": crc_i,
                "sha1": sha1, "merge": merge, "status": rstatus, "optional": optional,
            })
            if size:
                size_set.add(size)

        disks = []
        for disk in el.findall("disk"):
            dname = disk.get("name") or ""
            sha1 = (disk.get("sha1") or "").lower().strip()
            dstatus = (disk.get("status") or "good").lower()
            optional = (disk.get("optional") or "").lower() in ("yes", "1", "true")
            merge = disk.get("merge") or ""
            disks.append({
                "name": dname, "sha1": sha1, "status": dstatus,
                "optional": optional, "merge": merge,
            })

        # Arcade only : ignorer devices/mécaniques sans ROM ni CHD
        if not roms and not disks:
            el.clear()
            continue
        # Devices purement internes sans set jouable : on garde ceux qui ont des ROMs
        # (ex. ym2608) car présents dans un romset non-merged complet.

        if isbios:
            mtype = "bios"
        elif isdevice:
            mtype = "device"
        elif disks and not roms:
            mtype = "chd"
        elif disks:
            mtype = "game+chd"
        else:
            mtype = "game"

        machines.append({
            "game": name,
            "rom_name": name + ".zip",
            "description": desc,
            "year": year,
            "manufacturer": manufacturer,
            "cloneof": cloneof,
            "romof": romof,
            "isbios": isbios,
            "isdevice": isdevice,
            "ismechanical": ismechanical,
            "runnable": runnable,
            "mtype": mtype,
            "driver_status": driver_status,
            "roms": roms,
            "disks": disks,
            "size": 0,
            "crc": "",
            "crc_int": -1,
            "sha1": "",
            "md5": "",
            "is_disk": False,
        })
        el.clear()

    # Résolution BIOS
    # - sets isbios="yes"
    # - romof ≠ cloneof (ex. Neo-Geo : romof="neogeo") — même si le set BIOS
    #   est absent du DAT "clean"
    # - BIOS connus qui apparaissent comme romof
    _KNOWN_BIOS = frozenset({
        "neogeo", "neocdz", "neogeo_noslot", "ngp", "ngpc",
        "qsound", "cps1", "cps2", "cps3", "cp1",
        "pgm", "skns", "midssio", "nmk004", "decocass", "isgsm",
        "bubsys", "aleck64", "alg_bios", "aristmk5", "bioship",
        "cv1k", "hikaru", "hod2bios", "konamigv", "konamigx",
        "macsbios", "maxaflex", "megaplay", "megatech", "naomi",
        "naomi2", "naomigd", "stvbios", "syst1", "syst2", "syst22",
        "taitotz", "triforce", "viper", "chihiro", "lindbergh",
        "ym2608", "ym2413", "cchip",
    })
    bios_names = {m["game"] for m in machines if m.get("isbios")}
    by_name = {m["game"]: m for m in machines}
    romof_of = {m["game"]: (m.get("romof") or "") for m in machines}

    # Marquer comme BIOS les cibles romof « hors parent clone »
    for m in machines:
        ro = m.get("romof") or ""
        co = m.get("cloneof") or ""
        if not ro:
            continue
        if ro in _KNOWN_BIOS:
            bios_names.add(ro)
        # romof distinct du parent clone → quasi toujours un BIOS/device set
        if ro != co and ro != m["game"]:
            target = by_name.get(ro)
            if target is None or target.get("isbios") or target.get("mtype") == "bios":
                bios_names.add(ro)
            elif ro in _KNOWN_BIOS:
                bios_names.add(ro)
            # Neo-Geo / CPS dans un DAT clean : neogeo absent de la liste
            if target is None:
                bios_names.add(ro)

    def _resolve_bios(m: Dict) -> str:
        if m.get("isbios"):
            return m["game"]
        seen = set()
        cur = m.get("romof") or ""
        for _ in range(5):
            if not cur or cur in seen:
                break
            seen.add(cur)
            if cur in bios_names:
                return cur
            cur = romof_of.get(cur) or ""
        # héritage via parent clone
        cur = m.get("cloneof") or ""
        for _ in range(5):
            if not cur or cur in seen:
                break
            seen.add(cur)
            parent = by_name.get(cur)
            if parent:
                # parent.romof peut être le BIOS
                pro = parent.get("romof") or ""
                if pro and pro in bios_names:
                    return pro
                if pro and pro != (parent.get("cloneof") or "") and pro != parent["game"]:
                    if pro in bios_names or pro in _KNOWN_BIOS or pro not in by_name:
                        return pro
            cur = romof_of.get(cur) or (parent.get("cloneof") if parent else "") or ""
        # dernier recours : romof ≠ cloneof
        ro = m.get("romof") or ""
        co = m.get("cloneof") or ""
        if ro and ro != co and ro != m["game"]:
            return ro
        return ""

    for m in machines:
        m["bios"] = _resolve_bios(m)

    header["hash_mode"] = "crc"
    header["dat_mode"] = "arcade"
    header["merge_mode"] = "non-merged"
    # Label pack si détectable
    blob = (
        (header.get("name") or "") + " " +
        (header.get("description") or "") + " " +
        (header.get("author") or "") + " " +
        dat_path.name
    ).lower()
    if any(x in blob for x in ("fbneo", "finalburn", "final burn", "fba")):
        header["arcade_engine"] = "fbneo"
        if not header.get("name") or header.get("name") == dat_path.stem:
            header["name"] = header.get("name") or "FBNeo"
    elif not header.get("name"):
        header["name"] = "MAME"
    return header, rom_map, machines, size_set, "crc"



def _names_match(a: str, b: str) -> bool:
    """Comparaison de noms de fichiers (insensible à la casse, ignore chemins)."""
    if not a or not b:
        return False
    na = Path(a).name
    nb = Path(b).name
    if na == nb:
        return True
    if na.lower() == nb.lower():
        return True
    # stem égal (ex. rom.bin vs rom) uniquement si un des deux n'a pas d'extension
    sa, sb = Path(na).stem, Path(nb).stem
    if sa.lower() == sb.lower() and (not Path(na).suffix or not Path(nb).suffix or Path(na).suffix.lower() == Path(nb).suffix.lower()):
        return True
    return False


def _err_row(found, path, msg, is_zip=False, member=""):
    return {
        "status": "error", "game": "", "expected": "", "found": found, "path": path,
        "size": 0, "crc": "", "md5": "", "sha1": "", "is_zip": is_zip,
        "zip_member": member, "message": msg,
    }

# Extensions reconnues (loose)
_LOOSE_EXT = frozenset({
    ".bin", ".a78", ".rom", ".nes", ".sfc", ".smc", ".gb", ".gbc", ".gba",
    ".n64", ".z64", ".v64", ".nds", ".3ds", ".cia", ".psp", ".pce", ".sgx",
    ".ngp", ".ngc", ".ws", ".wsc", ".col", ".gg", ".sms", ".md", ".gen",
    ".smd", ".32x", ".j64", ".jag", ".lnx", ".a26", ".a52", ".int", ".vec",
    ".iso", ".img", ".chd", ".cue", ".nrg", ".mdf", ".cdi", ".gcm",
    ".wbfs", ".rvz", ".wia", ".cso", ".iso", ".toast",
})


# Gros fichiers hors archive (ISO, BIN…) : identification SANS lecture (case « ISO nom + taille »).
# Un CRC32 d'ISO Xbox 360 lit 7 à 8 Go ; et presque tous ces disques ont l'une des
# 4 tailles standard, donc le filtre taille n'élimine rien. En mode rapide, un fichier
# ≥ QUICK_MATCH_MIN_SIZE n'est jamais lu : soit le DAT l'identifie par le nom et la
# taille, soit il reste « non identifié » sans avoir été touché. Le CRC affiché est
# alors celui du DAT, pas un hash calculé. Case décochée (ou Vérification approfondie) :
# CRC complet, un gros fichier à la fois.
QUICK_MATCH_MIN_SIZE = 64 * 1024 * 1024
QUICK_MATCH_MSGS = {
    "exact": "Identifié nom + taille (CRC non relu)",
    "relaxed": "Identifié par nom approchant + taille (CRC non relu)",
    "prefix": "Identifié par début de nom + taille — nom tronqué ? (CRC non relu)",
    "size": "Identifié par taille unique dans le DAT (CRC non relu)",
}
UNIDENTIFIED_MSG = "Gros fichier non identifié par nom + taille — CRC non relu (décochez « ISO nom + taille » pour le calculer)"
_PREFIX_MIN_LEN = 8
_APOSTROPHES = "'’‘`´ʼ"
_DASHES = "‐‑‒–—―-_"


def _relax_name(name: str) -> str:
    """Nom « assoupli » : sans extension, accents, casse, apostrophes, tirets ni ponctuation."""
    stem = Path(name).stem if Path(name).suffix else name
    s = unicodedata.normalize("NFKD", stem)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = "".join("" if c in _APOSTROPHES else " " if c in _DASHES else c for c in s)
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(s.split())


class _QuickIndex:
    """Index du DAT pour identifier un gros fichier par son nom et sa taille (stat() seul)."""

    def __init__(self, games_list: List[Dict]):
        self.exact: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self.relaxed: Dict[Tuple[str, int], Dict[int, Dict[str, Any]]] = {}
        self.by_size: Dict[int, List[Tuple[List[str], Dict[str, Any]]]] = {}
        for g in games_list:
            size = int(g.get("size") or 0)
            if size < QUICK_MATCH_MIN_SIZE or g.get("is_disk"):
                continue
            crc_i = g.get("crc_int", -1)
            sha1 = (g.get("sha1") or "").lower()
            if (crc_i is None or crc_i < 0) and not sha1:
                continue
            hashes = {
                "size": size,
                "crc": f"{crc_i:08x}" if crc_i is not None and crc_i >= 0 else "",
                "crc_int": crc_i if crc_i is not None else -1,
                "md5": "", "sha1": sha1,
            }
            names = [n for n in (g.get("rom_name"), g.get("dump_name")) if n]
            relaxed = sorted({_relax_name(Path(n).name) for n in names})
            for n in names:
                self.exact.setdefault((Path(n).name.lower(), size), hashes)
            for r in relaxed:
                self.relaxed.setdefault((r, size), {})[id(g)] = hashes
            self.by_size.setdefault(size, []).append((relaxed, hashes))

    def lookup(self, filename: str, size: int) -> Optional[Tuple[Dict[str, Any], str]]:
        """(empreintes DAT, méthode) ou None. Les méthodes approchées exigent l'unicité."""
        hit = self.exact.get((filename.lower(), size))
        if hit:
            return hit, "exact"
        r = _relax_name(filename)
        same = self.relaxed.get((r, size)) or {}
        if len(same) == 1:
            return next(iter(same.values())), "relaxed"
        cands = self.by_size.get(size) or []
        if len(r) >= _PREFIX_MIN_LEN:
            pref = [h for names, h in cands if any(n.startswith(r) for n in names)]
            if len(pref) == 1:
                return pref[0], "prefix"
        if len(cands) == 1:
            return cands[0][1], "size"
        return None


# Cache des empreintes calculées (fichiers hors archive) : un CRC / SHA-1 lu une fois
# n'est plus recalculé tant que le fichier garde la même taille et la même date de
# modification. Il prime sur l'identification par nom + taille (c'est une vraie empreinte).
# La « Vérification approfondie » ignore le cache et le met à jour.
HASH_CACHE_FILE = CACHE_DIR / "hash_cache.json"
HASH_CACHE_MIN_SIZE = 1024 * 1024        # les petits fichiers se relisent vite
HASH_CACHE_MAX_AGE = 180 * 24 * 3600     # entrée oubliée si le fichier n'a pas été revu


class _HashCache:
    def __init__(self, file: Path):
        self.file = Path(file)
        self.lock = threading.Lock()
        self.save_lock = threading.Lock()
        self.data: Dict[str, Dict[str, Any]] = {}
        self.loaded = False
        self.dirty = False
        self.hits = 0
        self.last_save = time.time()

    @staticmethod
    def _key(path) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    def load(self) -> None:
        with self.lock:
            if self.loaded:
                return
            self.loaded = True
            try:
                raw = json.loads(self.file.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
                    self.data = {k: v for k, v in raw["entries"].items() if isinstance(v, dict)}
            except (OSError, ValueError):
                self.data = {}

    def get(self, path, st: os.stat_result, hash_mode: str) -> Optional[Dict[str, Any]]:
        """Empreintes en cache si le fichier n'a pas changé (taille + date), sinon None."""
        with self.lock:
            e = self.data.get(self._key(path))
            if not e or e.get("size") != st.st_size or e.get("mtime") != st.st_mtime_ns:
                return None
            crc, sha1 = str(e.get("crc") or ""), str(e.get("sha1") or "")
            if (crc if hash_mode != "sha1" else sha1) == "":
                return None  # l'empreinte demandée n'a pas encore été calculée
            try:
                crc_int = int(crc, 16) if crc else -1
            except ValueError:  # fichier de cache abîmé : on recalcule
                return None
            e["seen"] = int(time.time())
            self.dirty = True
            self.hits += 1
        return {
            "size": st.st_size, "crc": crc, "crc_int": crc_int,
            "md5": "", "sha1": sha1,
        }

    def put(self, path, st: os.stat_result, crc: str = "", sha1: str = "") -> None:
        key = self._key(path)
        with self.lock:
            e = self.data.get(key)
            if not e or e.get("size") != st.st_size or e.get("mtime") != st.st_mtime_ns:
                e = {"size": st.st_size, "mtime": st.st_mtime_ns, "crc": "", "sha1": ""}
            if crc:
                e["crc"] = crc
            if sha1:
                e["sha1"] = sha1
            e["seen"] = int(time.time())
            self.data[key] = e
            self.dirty = True
            due = time.time() - self.last_save > 30
        if due:  # un long scan de gros ISO interrompu ne perd pas tout
            self.save()

    def move(self, src, dst) -> None:
        """Après un renommage par l'application : l'empreinte suit le fichier."""
        with self.lock:
            e = self.data.pop(self._key(src), None)
            if e is None:
                return
            self.data[self._key(dst)] = e
            self.dirty = True
        self.save()

    def clear(self) -> None:
        with self.lock:
            self.data = {}
            self.loaded = True
            self.dirty = True
        self.save()

    def save(self) -> None:
        # save_lock d'abord : l'instantané et son écriture restent dans l'ordre
        with self.save_lock:
            with self.lock:
                if not self.dirty or not self.loaded:
                    return  # jamais chargé : ne pas écraser le fichier par un cache vide
                limit = time.time() - HASH_CACHE_MAX_AGE
                self.data = {k: v for k, v in self.data.items() if v.get("seen", 0) >= limit}
                payload = json.dumps({"version": 1, "entries": self.data}, separators=(",", ":"))
                self.dirty = False
                self.last_save = time.time()
            try:
                self.file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.file.with_name(self.file.name + ".tmp")
                tmp.write_text(payload, encoding="utf-8")
                _safe_replace(tmp, self.file)
            except OSError as e:
                print(f"[hash-cache] écriture impossible : {e}")


HASH_CACHE = _HashCache(HASH_CACHE_FILE)


_BIG_READ_SEM = threading.BoundedSemaphore(BIG_READ_PARALLEL)


def _hash_loose_task(args: Tuple) -> Dict:
    """
    Worker : hash un fichier loose selon hash_mode.
    args = (chemin, size_set, hash_mode[, quick_index[, use_cache[, cheap_only]]])
    quick_index : voir QUICK_MATCH_MIN_SIZE ; use_cache : voir HASH_CACHE
    (False en vérification approfondie : on recalcule et on met le cache à jour).
    cheap_only : ne lit pas le contenu ; renvoie {"needs_hash": True, "size": …}
    si une lecture complète est nécessaire (1re passe rapide du scan).
    """
    path_str, size_set, hash_mode = args[:3]
    quick_index = args[3] if len(args) > 3 else None
    use_cache = bool(args[4]) if len(args) > 4 else False
    cheap_only = bool(args[5]) if len(args) > 5 else False
    fpath = Path(path_str)

    def res(hashes=None, error=None, **extra):
        return {
            "path": path_str, "found": fpath.name, "is_zip": False,
            "zip_member": "", "hashes": hashes, "error": error, **extra,
        }

    try:
        st = fpath.stat()
        size = st.st_size
    except OSError as e:
        return res(error=str(e))
    cacheable = size >= HASH_CACHE_MIN_SIZE

    # (en mode CRC, une taille absente du DAT reste filtrée plus bas, comme sans cache)
    if use_cache and cacheable and (hash_mode == "sha1" or not size_set or size in size_set):
        cached = HASH_CACHE.get(fpath, st, hash_mode)
        if cached:
            return res(cached, cached=True)

    big_quick = quick_index is not None and size >= QUICK_MATCH_MIN_SIZE
    if big_quick:
        found = quick_index.lookup(fpath.name, size)
        if found:
            return res(dict(found[0]), quick=found[1])

    # CHD : SHA1 header instantané (MAME / MAMERedump) ; header illisible → hash du fichier
    if fpath.suffix.lower() == ".chd":
        sha1 = read_chd_sha1(fpath)
        if sha1:
            return res({"size": size, "crc": "", "crc_int": -1, "md5": "", "sha1": sha1})

    # Mode CRC : filtre taille (aucune entrée du DAT n'a cette taille → inutile de lire)
    if hash_mode != "sha1" and size_set and size not in size_set:
        return res({"size": size, "crc": "", "crc_int": -1, "md5": "", "sha1": ""}, size_mismatch=True)

    # Mode rapide : un gros fichier n'est jamais lu, même non identifié
    if big_quick:
        return res({"size": size, "crc": "", "crc_int": -1, "md5": "", "sha1": ""}, unidentified=True)

    if cheap_only:
        return res(needs_hash=True, size=size)

    big = size >= BIG_READ_MIN_SIZE
    if big:
        _BIG_READ_SEM.acquire()
    try:
        SCAN_PROGRESS.check()  # dans le try : une annulation libère toujours la place
        SCAN_PROGRESS.file_start(path_str, fpath.name, size)
        if hash_mode == "sha1":
            sha1, size2 = _sha1_path(fpath)
            if cacheable:
                HASH_CACHE.put(fpath, st, sha1=sha1)
            return res({"size": size2, "crc": "", "crc_int": -1, "md5": "", "sha1": sha1})
        crc, size2 = _crc32_path(fpath)
        if cacheable:
            HASH_CACHE.put(fpath, st, crc=f"{crc:08x}")
        return res({"size": size2, "crc": f"{crc:08x}", "crc_int": crc, "md5": "", "sha1": ""})
    except ScanCancelled:
        raise
    except Exception as e:
        return res(error=str(e))
    finally:
        SCAN_PROGRESS.file_end(path_str)
        if big:
            _BIG_READ_SEM.release()


def _zip_member_corrupt(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> Optional[str]:
    """
    Vérification approfondie : décompresse le membre (en flux) ; zipfile contrôle le CRC
    réel des données contre celui de l'en-tête. None si OK, sinon le message d'erreur.
    """
    try:
        with zf.open(info, "r") as f:
            while f.read(_READ_BUF):
                pass
        return None
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError) as e:
        return str(e) or e.__class__.__name__


def _hash_zip_task(args: Tuple) -> List[Dict]:
    """Worker : ZIP — CRC ou SHA1 selon mode. deep=True : données décompressées et contrôlées."""
    path_str, size_set, hash_mode = args[:3]
    deep = bool(args[3]) if len(args) > 3 else False
    fpath = Path(path_str)
    out: List[Dict] = []
    try:
        with zipfile.ZipFile(fpath, "r") as zf:
            members = [i for i in zf.infolist() if not i.is_dir() and i.file_size > 0]
            if not members:
                out.append({
                    "path": path_str, "found": fpath.name, "is_zip": True,
                    "zip_member": "", "hashes": None, "error": "ZIP vide",
                })
                return out
            for info in members:
                label = f"{fpath.name} → {info.filename}"
                member_lower = info.filename.lower()
                # CHD dans un zip (rare) : extraire header
                if member_lower.endswith(".chd"):
                    try:
                        with zf.open(info, "r") as mf:
                            hdr = mf.read(124)
                        sha1 = None
                        if len(hdr) >= 16 and hdr[:8] == b"MComprHD":
                            version = int.from_bytes(hdr[12:16], "big")
                            if version == 5 and len(hdr) >= 104:
                                sha1 = hdr[84:104].hex()
                            elif version == 4 and len(hdr) >= 68:
                                sha1 = hdr[48:68].hex()
                            elif version == 3 and len(hdr) >= 100:
                                sha1 = hdr[80:100].hex()
                        if sha1:
                            out.append({
                                "path": path_str, "found": label, "is_zip": True,
                                "zip_member": info.filename, "hashes": {
                                    "size": info.file_size, "crc": "", "crc_int": -1,
                                    "md5": "", "sha1": sha1,
                                }, "error": None,
                            })
                            continue
                    except Exception as e:
                        out.append({
                            "path": path_str, "found": label, "is_zip": True,
                            "zip_member": info.filename, "hashes": None, "error": str(e),
                        })
                        continue

                if hash_mode == "sha1":
                    try:
                        with zf.open(info, "r") as mf:
                            sha1, size = _sha1_fileobj(mf)
                        out.append({
                            "path": path_str, "found": label, "is_zip": True,
                            "zip_member": info.filename, "hashes": {
                                "size": size, "crc": "", "crc_int": -1,
                                "md5": "", "sha1": sha1,
                            }, "error": None,
                        })
                    except Exception as e:
                        out.append({
                            "path": path_str, "found": label, "is_zip": True,
                            "zip_member": info.filename, "hashes": None, "error": str(e),
                        })
                    continue

                if size_set and info.file_size not in size_set:
                    out.append({
                        "path": path_str, "found": label, "is_zip": True,
                        "zip_member": info.filename, "hashes": {
                            "size": info.file_size, "crc": "", "crc_int": -1,
                            "md5": "", "sha1": "",
                        }, "error": None, "size_mismatch": True,
                    })
                    continue
                # CRC catalogue central — pas de décompression (sauf vérification approfondie)
                crc = info.CRC & 0xFFFFFFFF
                row = {
                    "path": path_str, "found": label, "is_zip": True,
                    "zip_member": info.filename, "hashes": {
                        "size": info.file_size, "crc": f"{crc:08x}", "crc_int": crc,
                        "md5": "", "sha1": "",
                    }, "error": None,
                }
                if deep:
                    bad = _zip_member_corrupt(zf, info)
                    if bad:
                        row["corrupt"] = bad
                out.append(row)
    except zipfile.BadZipFile:
        out.append({
            "path": path_str, "found": fpath.name, "is_zip": True,
            "zip_member": "", "hashes": None, "error": "ZIP corrompu",
        })
    except Exception as e:
        out.append({
            "path": path_str, "found": fpath.name, "is_zip": True,
            "zip_member": "", "hashes": None, "error": str(e),
        })
    return out


# Archives lisibles en mode standard (No-Intro / Redump) ; .zip modifiable, .7z en lecture seule
_ARCHIVE_EXTS = (".zip", ".7z")
_7Z_MISSING = "Archive .7z non lue : module py7zr absent (pip install py7zr)"
_RAR_UNSUPPORTED = "Archive RAR non prise en charge : convertissez-la en .zip ou .7z"


def _list_7z(path: Path) -> List[Tuple[str, int, Optional[int]]]:
    """Membres fichiers d'un .7z : (nom, taille, crc32 de l'en-tête ou None)."""
    with py7zr.SevenZipFile(str(path), "r") as z:
        return [
            (fi.filename, int(fi.uncompressed or 0), fi.crc32)
            for fi in z.list() if not fi.is_directory
        ]


def _hash_7z_task(args: Tuple) -> List[Dict]:
    """Worker : .7z — CRC lus dans l'en-tête (pas de décompression), comme pour les ZIP."""
    path_str, size_set, hash_mode = args[:3]
    deep = bool(args[3]) if len(args) > 3 else False
    fpath = Path(path_str)

    def _row(found, member="", hashes=None, error=None, **extra):
        return {
            "path": path_str, "found": found, "is_zip": True, "archive": "7z",
            "zip_member": member, "hashes": hashes, "error": error, **extra,
        }

    if py7zr is None:
        return [_row(fpath.name, error=_7Z_MISSING)]
    try:
        members = [m for m in _list_7z(fpath) if m[1] > 0]
    except Exception as e:
        return [_row(fpath.name, error=f"7z illisible : {e}")]
    if not members:
        return [_row(fpath.name, error="7z vide")]
    corrupt_member: Optional[str] = None
    corrupt_all: Optional[str] = None
    if deep:
        try:
            with py7zr.SevenZipFile(str(fpath), "r") as z:
                corrupt_member = z.testzip()  # 1er fichier dont le CRC réel diffère, ou None
        except Exception as e:
            corrupt_all = str(e) or e.__class__.__name__
    out: List[Dict] = []
    for name, size, crc in members:
        label = f"{fpath.name} → {name}"
        if hash_mode == "sha1":
            out.append(_row(label, name, error="Vérification SHA-1 dans un .7z non prise en charge"))
        elif size_set and size not in size_set:
            out.append(_row(label, name, hashes={
                "size": size, "crc": "", "crc_int": -1, "md5": "", "sha1": "",
            }, size_mismatch=True))
        elif crc is None:
            out.append(_row(label, name, error="CRC absent de l'en-tête 7z"))
        else:
            crc &= 0xFFFFFFFF
            extra = {}
            if corrupt_all or (corrupt_member and corrupt_member == name):
                extra["corrupt"] = corrupt_all or "CRC des données ≠ CRC de l'en-tête"
            out.append(_row(label, name, hashes={
                "size": size, "crc": f"{crc:08x}", "crc_int": crc, "md5": "", "sha1": "",
            }, **extra))
    return out


_SKIP_DIR_NAMES = frozenset({
    ".git", ".svn", "__pycache__", "node_modules",
    "media", "images", "videos", "manuals", "screenshots",
    "titles", "boxes", "boxart", "marquees", "wheels",
    "downloaded_images", "gamelist", "collections",
})


def _iter_rom_paths(rom_dir: Path, max_depth: int = 0):
    """
    Parcourt rom_dir avec profondeur limitée.
    max_depth=0 → fichiers à la racine uniquement
    max_depth=1 → + sous-dossiers immédiats
    max_depth=2 → + un niveau de plus
    Ignore les dossiers système / médias courants (RetroBat).
    Yields Path fichiers.
    """
    max_depth = max(0, min(int(max_depth or 0), 2))
    root = Path(rom_dir)
    if not root.is_dir():
        return

    def _walk(cur: Path, depth: int):
        try:
            with os.scandir(str(cur)) as it:
                for entry in it:
                    try:
                        name = entry.name
                        if name.startswith("."):
                            continue
                        if entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
                        elif entry.is_dir(follow_symlinks=False) and depth < max_depth:
                            if name.lower() in _SKIP_DIR_NAMES:
                                continue
                            yield from _walk(Path(entry.path), depth + 1)
                    except OSError:
                        continue
        except OSError:
            return

    yield from _walk(root, 0)


def _tracked(task):
    """Enveloppe un worker d'archive : nom affiché dans l'avancement + annulation."""
    def run(args):
        path_str = args[0] if isinstance(args, tuple) else args
        SCAN_PROGRESS.check()
        try:
            size = os.path.getsize(path_str)
        except OSError:
            size = 0
        SCAN_PROGRESS.file_start(path_str, Path(path_str).name, size)
        try:
            return task(args)
        finally:
            SCAN_PROGRESS.file_end(path_str)
    return run


def _collect_files(
    rom_dir: Path,
    size_set: set,
    hash_mode: str = "crc",
    max_depth: int = 0,
    skip_zips: bool = False,
    deep: bool = False,
    games_list: Optional[List[Dict]] = None,
    quick_id: bool = True,
) -> List[Dict]:
    loose: List[str] = []
    zips: List[str] = []
    sevens: List[str] = []
    rars: List[str] = []
    seen = set()
    # .chd toujours inclus
    loose_ext = set(_LOOSE_EXT) | {".chd"}

    SCAN_PROGRESS.phase("list")
    for fpath in _iter_rom_paths(rom_dir, max_depth=max_depth):
        SCAN_PROGRESS.step()
        if len(seen) % 64 == 0:
            SCAN_PROGRESS.check()
        try:
            # dédup par chemin (Windows case-insensitive)
            key = os.path.normcase(str(fpath))
            if key in seen:
                continue
            seen.add(key)
            suf = fpath.suffix.lower()
            path_str = str(fpath)
            if suf == ".zip":
                if not skip_zips:
                    zips.append(path_str)
            elif suf == ".7z":
                if not skip_zips:
                    sevens.append(path_str)
            elif suf == ".rar":
                rars.append(path_str)
            elif suf in loose_ext:
                loose.append(path_str)
            elif size_set:
                # Extension rare / absente : candidat si taille connue du DAT
                try:
                    sz = fpath.stat().st_size
                except OSError:
                    continue
                if sz in size_set:
                    loose.append(path_str)
        except OSError:
            continue

    inventory: List[Dict] = []
    workers = max(4, min(_MAX_WORKERS, max(len(loose) + len(zips), 1)))

    if loose:
        quick_index = _QuickIndex(games_list or []) if quick_id and not deep else None
        HASH_CACHE.load()
        # chunksize adaptatif : petits lots si peu de fichiers
        chunk = 8 if len(loose) > 64 else max(1, len(loose) // max(workers, 1) or 1)
        try:
            # 1re passe sans lecture du contenu : cache, nom + taille, en-tête CHD, filtre taille
            SCAN_PROGRESS.phase("quick", total=len(loose))
            slots: List[Optional[Dict]] = [None] * len(loose)  # ordre d'origine conservé
            pending: List[Tuple[int, int]] = []  # (index, taille)
            tasks = [(p, size_set, hash_mode, quick_index, not deep, True) for p in loose]
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for i, result in enumerate(ex.map(_hash_loose_task, tasks, chunksize=chunk)):
                    SCAN_PROGRESS.check()
                    if result.get("needs_hash"):
                        pending.append((i, int(result.get("size") or 0)))
                        SCAN_PROGRESS.step()
                        continue
                    SCAN_PROGRESS.step(kind="cached" if result.get("cached") else
                                       "quick" if result.get("quick") else
                                       "skipped" if result.get("unidentified") else "")
                    slots[i] = result
            # 2e passe : lecture complète (CRC / SHA-1) des fichiers restants
            if pending:
                SCAN_PROGRESS.phase("hash", total=len(pending), bytes_total=sum(sz for _i, sz in pending))
                tasks = [(loose[i], size_set, hash_mode, None, False) for i, _sz in pending]
                chunk = 1 if len(pending) <= 64 else 4  # gros fichiers : répartition fine
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    for (i, _sz), result in zip(pending, ex.map(_hash_loose_task, tasks, chunksize=chunk)):
                        SCAN_PROGRESS.step(kind="hashed")
                        slots[i] = result
            inventory.extend(r for r in slots if r is not None)
        finally:
            HASH_CACHE.save()  # même si le scan est interrompu : les CRC déjà lus sont gardés

    if zips or sevens:
        SCAN_PROGRESS.phase("archives", total=len(zips) + len(sevens))
    if zips:
        tasks = [(p, size_set, hash_mode, deep) for p in zips]
        chunk = 4 if len(zips) > 32 else 1
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for batch in ex.map(_tracked(_hash_zip_task), tasks, chunksize=chunk):
                SCAN_PROGRESS.step()
                inventory.extend(batch)

    if sevens:
        tasks = [(p, size_set, hash_mode, deep) for p in sevens]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for batch in ex.map(_tracked(_hash_7z_task), tasks, chunksize=4 if len(sevens) > 32 else 1):
                SCAN_PROGRESS.step()
                inventory.extend(batch)

    # RAR : signalé (statut « error », jamais supprimé en masse) plutôt qu'ignoré en silence
    for p in rars:
        inventory.append({
            "path": p, "found": Path(p).name, "is_zip": True, "archive": "rar",
            "zip_member": "", "hashes": None, "error": _RAR_UNSUPPORTED,
        })

    return inventory



def _zip_catalog(zpath: Path, deep: bool = False) -> Dict[str, Any]:
    """
    Lecture unique du catalogue central d'un ZIP (sans décompresser).
    Retourne path, name, crc_idx, name_idx, members, corrupt, error.
    deep=True : chaque membre est décompressé ; un membre dont les données ne
    correspondent pas au CRC de l'en-tête va dans corrupt {nom: message} et n'est
    ni indexé par CRC ni proposé comme source (name_idx le garde avec crc=-1).
    """
    path_str = str(zpath)
    out: Dict[str, Any] = {
        "path": path_str,
        "name": Path(path_str).name,
        "crc_idx": {},
        "name_idx": {},
        "members": [],
        "corrupt": {},
        "error": None,
    }
    try:
        with zipfile.ZipFile(path_str, "r") as zf:
            for info in zf.infolist():
                if info.is_dir() or info.file_size <= 0:
                    continue
                crc = info.CRC & 0xFFFFFFFF
                fn = info.filename
                sz = info.file_size
                if deep:
                    bad = _zip_member_corrupt(zf, info)
                    if bad:
                        out["corrupt"][fn] = bad
                        out["name_idx"][fn.lower()] = (fn, sz, -1)
                        base = fn.rsplit("/", 1)[-1].lower()
                        out["name_idx"].setdefault(base, (fn, sz, -1))
                        continue
                out["crc_idx"].setdefault(crc, []).append((fn, sz))
                out["name_idx"][fn.lower()] = (fn, sz, crc)
                base = fn.rsplit("/", 1)[-1].lower()
                if base not in out["name_idx"]:
                    out["name_idx"][base] = (fn, sz, crc)
                out["members"].append((fn, sz, crc))
    except zipfile.BadZipFile:
        out["error"] = "ZIP corrompu"
    except Exception as e:
        out["error"] = str(e)
    return out


def _zip_catalog_task(path_str: str, deep: bool = False) -> Dict[str, Any]:
    return _zip_catalog(Path(path_str), deep=deep)


def _scan_one_mame_machine(args: Tuple) -> Dict:
    """Vérifie un machine.zip en mode non-merged (jamais de ROMs lues depuis le parent).
    Produit aussi `components[]` : statut de chaque ROM/CHD attendu.
    args: (machine, zpath_str|None, catalog|None) — catalog évite une 2e ouverture.
    """
    machine, zpath_str = args[0], args[1]
    catalog = args[2] if len(args) >= 3 else None
    rom_dir = Path(args[3]) if len(args) >= 4 and args[3] else None
    name = machine["game"]
    desc = machine.get("description") or name
    cloneof = machine.get("cloneof") or ""
    mtype = machine.get("mtype") or "game"
    driver_status = machine.get("driver_status") or "good"
    bios = machine.get("bios") or ""
    roms = machine.get("roms") or []
    disks = machine.get("disks") or []

    base = {
        "game": name,
        "expected": name + ".zip",
        "found": "",
        "path": "",
        "size": 0,
        "crc": "",
        "sha1": "",
        "md5": "",
        "is_zip": True,
        "zip_member": "",
        "cloneof": cloneof or "",
        "parent": cloneof if cloneof else name,
        "is_clone": bool(cloneof),
        "mtype": mtype,
        "driver_status": driver_status,
        "bios": bios,
        "description": desc,
        "message": "",
        "components": [],
    }

    required = [
        r for r in roms
        if r.get("crc_int", -1) >= 0
        and r.get("status") != "nodump"
        and not r.get("optional")
    ]

    def _comp(kind, expected_name, status, **kw):
        c = {
            "kind": kind,  # rom | chd | extra
            "expected": expected_name or "",
            "found": kw.get("found") or "",
            "status": status,  # ok | rename | missing | bad_crc | bad_size | nodump | optional | extra
            "crc_expected": kw.get("crc_expected") or "",
            "crc_found": kw.get("crc_found") or "",
            "sha1_expected": kw.get("sha1_expected") or "",
            "sha1_found": kw.get("sha1_found") or "",
            "size_expected": kw.get("size_expected") or 0,
            "size_found": kw.get("size_found") or 0,
            "message": kw.get("message") or "",
        }
        return c

    def _eval_disks(base_dir: Optional[Path]) -> Tuple[List[Dict], List[str], List[str], int]:
        """Statut de chaque CHD attendu. Retourne (components, manquants, mauvais, trouvés OK)."""
        comps: List[Dict] = []
        missing: List[str] = []
        bad: List[str] = []
        ok_n = 0
        for d in disks:
            dname = d.get("name") or ""
            sha_e = (d.get("sha1") or "").lower()
            dstatus = (d.get("status") or "good").lower()
            optional = bool(d.get("optional"))
            chd_fn = _chd_filename(dname)
            if dstatus == "nodump" or not sha_e:
                comps.append(_comp(
                    "chd", chd_fn, "nodump" if dstatus == "nodump" else "optional",
                    sha1_expected=sha_e, message="nodump" if dstatus == "nodump" else "sans SHA1",
                ))
                continue
            found_chd = None
            if base_dir is not None:
                candidates = [
                    base_dir / name / chd_fn,  # convention MAME : <machine>/<disque>.chd
                    base_dir / chd_fn,
                    base_dir / _chd_filename(name),
                ]
                found_chd = next((c for c in candidates if c.is_file()), None)
            if not found_chd:
                if optional:
                    comps.append(_comp(
                        "chd", chd_fn, "optional",
                        sha1_expected=sha_e, message="CHD optionnel absent",
                    ))
                else:
                    comps.append(_comp(
                        "chd", chd_fn, "missing",
                        sha1_expected=sha_e, message="CHD absent",
                    ))
                    missing.append(dname)
                continue
            sha = read_chd_sha1(found_chd)
            if not sha:
                comps.append(_comp(
                    "chd", chd_fn, "bad_crc",
                    found=str(found_chd), sha1_expected=sha_e,
                    message="header CHD illisible",
                ))
                bad.append(f"{chd_fn} (header)")
            elif sha != sha_e:
                comps.append(_comp(
                    "chd", chd_fn, "bad_crc",
                    found=str(found_chd), sha1_expected=sha_e, sha1_found=sha,
                    message="SHA1 incorrect",
                ))
                bad.append(f"{chd_fn} (SHA1)")
            else:
                comps.append(_comp(
                    "chd", chd_fn, "ok",
                    found=str(found_chd), sha1_expected=sha_e, sha1_found=sha,
                    message="OK",
                ))
                ok_n += 1
        return comps, missing, bad, ok_n

    if not zpath_str:
        # Machine CHD seule (aucune ROM requise) : les CHD peuvent exister sans ZIP
        if disks and not required:
            components = []
            for r in roms:
                st = "nodump" if r.get("status") == "nodump" else "optional"
                components.append(_comp(
                    "rom", r.get("name") or "", st,
                    crc_expected=r.get("crc") or "", size_expected=r.get("size") or 0,
                    message="optionnelle" if st == "optional" else "nodump",
                ))
            dcomps, dmissing, dbad, dok = _eval_disks(rom_dir)
            components.extend(dcomps)
            first_found = next((c["found"] for c in dcomps if c.get("found")), "")
            if dbad:
                status, msg = "bad", f"{len(dbad)} mauvais hash : " + ", ".join(dbad[:5])
            elif dmissing and dok:
                status, msg = "incomplete", f"{len(dmissing)} CHD manquant(s) : " + ", ".join(dmissing[:5])
            elif dmissing:
                status, msg = "missing", "CHD absent"
            else:
                status, msg = "good", f"{dok} CHD OK"
            return {
                **base,
                "status": status,
                "expected": _chd_filename(name) if len(disks) == 1 else name + "/",
                "found": Path(first_found).name if first_found else "",
                "path": first_found,
                "is_zip": False,
                "message": msg,
                "components": components,
            }
        components = []
        for r in roms:
            st = "nodump" if r.get("status") == "nodump" else ("optional" if r.get("optional") else "missing")
            components.append(_comp(
                "rom", r.get("name") or "", st,
                crc_expected=r.get("crc") or "",
                size_expected=r.get("size") or 0,
                message="ZIP absent",
            ))
        for d in disks:
            st = "nodump" if d.get("status") == "nodump" else ("optional" if d.get("optional") else "missing")
            components.append(_comp(
                "chd", _chd_filename(d.get("name") or ""), st,
                sha1_expected=d.get("sha1") or "",
                message="CHD/ZIP absent",
            ))
        return {
            **base,
            "status": "missing",
            "expected": name + ".zip",
            "message": "ZIP absent",
            "components": components,
        }

    zpath = Path(zpath_str)
    base["path"] = str(zpath)
    base["found"] = zpath.name
    base["size"] = zpath.stat().st_size if zpath.exists() else 0

    if catalog is None:
        catalog = _zip_catalog(zpath)
    if catalog.get("error") and not catalog.get("crc_idx"):
        return {
            **base,
            "status": "error",
            "message": catalog.get("error") or "ZIP illisible",
            "components": [],
        }

    crc_idx: Dict[int, List[Tuple[str, int]]] = catalog.get("crc_idx") or {}
    name_idx: Dict[str, Tuple[str, int, int]] = catalog.get("name_idx") or {}

    if not crc_idx and required:
        return {**base, "status": "error", "message": "ZIP illisible ou vide", "components": []}

    components = []
    missing_roms = []
    bad_roms = []
    found_count = 0
    matched_members: set = set()  # actual zip member names matched
    reported_corrupt: set = set()  # membres corrompus déjà signalés sur une ROM

    # Noms DAT requis (pour ne jamais « voler » un membre qui est déjà le bon nom d'une autre ROM)
    required_names = {
        Path(r.get("name") or "").name.lower()
        for r in roms
        if (r.get("name") or "") and (r.get("status") or "good").lower() != "nodump"
    }

    # Pass 1 : réserver les correspondances nom exact + CRC (priorité absolue)
    exact_hits: Dict[int, Tuple[str, int]] = {}  # index dans roms → (fn, sz)
    for i, r in enumerate(roms):
        rname = r.get("name") or ""
        if not rname:
            continue
        rstatus = (r.get("status") or "good").lower()
        if rstatus == "nodump":
            continue
        crc_i = r.get("crc_int", -1)
        if crc_i < 0:
            continue
        rname_l = Path(rname).name.lower()
        size_e = r.get("size") or 0
        hits = crc_idx.get(crc_i) or []
        for fn, sz in hits:
            if fn in matched_members:
                continue
            if Path(fn).name.lower() == rname_l and (not size_e or sz == size_e):
                exact_hits[i] = (fn, sz)
                matched_members.add(fn)
                break

    for i, r in enumerate(roms):
        rname = r.get("name") or ""
        crc_s = (r.get("crc") or "").lower()
        crc_i = r.get("crc_int", -1)
        size_e = r.get("size") or 0
        rstatus = (r.get("status") or "good").lower()
        optional = bool(r.get("optional"))

        if rstatus == "nodump" or (crc_i < 0 and optional):
            components.append(_comp(
                "rom", rname, "nodump" if rstatus == "nodump" else "optional",
                crc_expected=crc_s, size_expected=size_e,
                message="nodump" if rstatus == "nodump" else "optionnel",
            ))
            continue
        if crc_i < 0:
            components.append(_comp(
                "rom", rname, "optional" if optional else "missing",
                crc_expected=crc_s, size_expected=size_e,
                message="pas de CRC dans le DAT",
            ))
            continue

        rname_l = Path(rname).name.lower()
        hits = crc_idx.get(crc_i) or []

        if i in exact_hits:
            fn, sz = exact_hits[i]
            found_count += 1
            if size_e and sz != size_e:
                components.append(_comp(
                    "rom", rname, "bad_size",
                    found=fn, crc_expected=crc_s, crc_found=crc_s,
                    size_expected=size_e, size_found=sz,
                    message="taille différente",
                ))
                bad_roms.append(rname)
            else:
                components.append(_comp(
                    "rom", rname, "ok",
                    found=fn, crc_expected=crc_s, crc_found=crc_s,
                    size_expected=size_e, size_found=sz,
                    message="OK",
                ))
            continue

        # Pass 2 : CRC dans un membre encore libre dont le nom N'EST PAS un autre nom DAT requis
        hit = None
        if size_e:
            for fn, sz in hits:
                if fn in matched_members:
                    continue
                if Path(fn).name.lower() in required_names:
                    continue
                if sz == size_e:
                    hit = (fn, sz)
                    break
        if hit is None:
            for fn, sz in hits:
                if fn in matched_members:
                    continue
                if Path(fn).name.lower() in required_names:
                    continue
                hit = (fn, sz)
                break

        if hit is not None:
            fn, sz = hit
            matched_members.add(fn)
            found_count += 1
            base_fn = Path(fn).name
            if base_fn.lower() != rname_l:
                components.append(_comp(
                    "rom", rname, "rename",
                    found=fn, crc_expected=crc_s, crc_found=crc_s,
                    size_expected=size_e, size_found=sz,
                    message=f"CRC OK, nom différent → {base_fn}",
                ))
            elif size_e and sz != size_e:
                components.append(_comp(
                    "rom", rname, "bad_size",
                    found=fn, crc_expected=crc_s, crc_found=crc_s,
                    size_expected=size_e, size_found=sz,
                    message="taille différente",
                ))
                bad_roms.append(rname)
            else:
                components.append(_comp(
                    "rom", rname, "ok",
                    found=fn, crc_expected=crc_s, crc_found=crc_s,
                    size_expected=size_e, size_found=sz,
                    message="OK",
                ))
            continue

        # Pass 3 : le CRC est dans le ZIP
        #  a) même nom déjà présent (entrée DAT répétée, ex. DECO Cassette)
        #     → un seul fichier suffit, c'est OK partagé
        #  b) autre nom → il faut DUPLIQUER sous le nom attendu
        if hits:
            same = None
            other = None
            for fn, sz in hits:
                if Path(fn).name.lower() == rname_l:
                    same = (fn, sz)
                    break
                if other is None:
                    other = (fn, sz)
            if same is None:
                # nom exact via name_idx (chemin différent / casse)
                by_name = name_idx.get(rname_l)
                if by_name and by_name[2] == crc_i:
                    same = (by_name[0], by_name[1])
            if same is not None:
                fn, sz = same
                found_count += 1
                components.append(_comp(
                    "rom", rname, "ok",
                    found=fn, crc_expected=crc_s, crc_found=crc_s,
                    size_expected=size_e, size_found=sz,
                    message="OK",
                ))
                continue
            fn, sz = other or hits[0]
            found_count += 1
            components.append(_comp(
                "rom", rname, "rename",
                found=fn, crc_expected=crc_s, crc_found=crc_s,
                size_expected=size_e, size_found=sz,
                message=f"CRC OK, à dupliquer depuis {Path(fn).name}",
            ))
            continue

        # Pass 4 : nom présent avec un autre CRC
        by_name = name_idx.get(rname_l)
        if by_name:
            fn, sz, found_crc = by_name
            if found_crc == crc_i:
                found_count += 1
                components.append(_comp(
                    "rom", rname, "ok",
                    found=fn, crc_expected=crc_s, crc_found=crc_s,
                    size_expected=size_e, size_found=sz,
                    message="OK",
                ))
            elif found_crc < 0:
                # membre au bon nom mais données corrompues (vérification approfondie)
                reported_corrupt.add(fn)
                components.append(_comp(
                    "rom", rname, "bad_crc",
                    found=fn, crc_expected=crc_s, crc_found="",
                    size_expected=size_e, size_found=sz,
                    message="données corrompues (vérification approfondie)",
                ))
                bad_roms.append(rname)
            else:
                components.append(_comp(
                    "rom", rname, "bad_crc",
                    found=fn, crc_expected=crc_s,
                    crc_found=f"{found_crc:08x}",
                    size_expected=size_e, size_found=sz,
                    message="CRC incorrect",
                ))
                bad_roms.append(rname)
            continue

        if optional:
            # ROM optionnelle (optional="yes") : son absence ne rend pas le set incomplet
            components.append(_comp(
                "rom", rname, "optional",
                crc_expected=crc_s, size_expected=size_e,
                message="optionnelle absente",
            ))
            continue
        components.append(_comp(
            "rom", rname, "missing",
            crc_expected=crc_s, size_expected=size_e,
            message="absente du ZIP",
        ))
        missing_roms.append(rname)

    # CHD (à côté du ZIP)
    dcomps, chd_missing, dbad, _dok = _eval_disks(zpath.parent)
    components.extend(dcomps)
    bad_roms.extend(dbad)

    # Membres ZIP non référencés dans le DAT (catalogue déjà en mémoire)
    required_crcs = {r.get("crc_int") for r in required if r.get("crc_int", -1) >= 0}
    for fn, sz, crc in (catalog.get("members") or []):
        if fn in matched_members:
            continue
        if crc in required_crcs:
            continue
        components.append(_comp(
            "extra", "", "extra",
            found=fn,
            crc_found=f"{crc:08x}",
            size_found=sz,
            message="fichier hors DAT",
        ))
    # Membres corrompus non rattachés à une ROM du DAT par leur nom
    for fn, err in (catalog.get("corrupt") or {}).items():
        if fn in reported_corrupt:
            continue
        components.append(_comp(
            "extra", "", "extra", found=fn,
            message=f"données corrompues (vérification approfondie) : {err}",
        ))

    parts = []
    if missing_roms:
        parts.append(f"{len(missing_roms)} ROM manquante(s)")
    if chd_missing:
        parts.append(f"{len(chd_missing)} CHD manquant(s)")
    if bad_roms:
        parts.append(f"{len(bad_roms)} mauvais hash")
    n_rename = sum(1 for c in components if c["status"] == "rename")
    if n_rename:
        parts.append(f"{n_rename} mal nommée(s)")
    req_name_set = {r.get("name") or "" for r in required}
    found_required = sum(
        1 for c in components
        if c["kind"] == "rom" and c["expected"] in req_name_set
        and c["status"] in ("ok", "rename", "bad_size")
    )

    if not required and not disks:
        status = "good"
        msg = "Aucune ROM"
    elif missing_roms and found_count == 0 and not crc_idx:
        status = "error"
        msg = "ZIP vide"
    elif missing_roms and found_count == 0:
        status = "bad"
        msg = "; ".join(parts) if parts else "ROMs absentes"
    elif missing_roms or chd_missing:
        status = "incomplete"
        msg = "; ".join(parts)
        if missing_roms[:5]:
            msg += " : " + ", ".join(missing_roms[:5])
            if len(missing_roms) > 5:
                msg += f"… (+{len(missing_roms)-5})"
    elif bad_roms:
        status = "bad"
        msg = "; ".join(parts)
    else:
        status = "good"
        msg = f"{found_count} ROM OK"
        if disks:
            msg += f", {len(disks) - len(chd_missing)} CHD OK"
        if n_rename:
            status = "rename"
            msg += f", {n_rename} à renommer dans le ZIP"

    # Nom du zip parent
    if status in ("good", "rename") and zpath.name.lower() != (name + ".zip").lower():
        if status == "good":
            status = "rename"
        msg = (msg + " · ").lstrip(" · ") + f"ZIP → {name}.zip"

    return {
        **base,
        "status": status,
        "message": msg,
        "crc": f"{found_required}/{len(required)}" if required else "",
        "components": components,
    }



def _annotate_available_roms(results: List[Dict], pool: Dict[int, List[Dict[str, Any]]]) -> None:
    """Marque les composantes missing dont le CRC existe dans un autre ZIP."""
    for row in results:
        own_name = Path(row.get("path") or row.get("found") or "").name.lower()
        comps = row.get("components") or []
        n_avail = 0
        for c in comps:
            if (c.get("kind") or "rom") != "rom":
                continue
            if (c.get("status") or "") != "missing":
                continue
            crc_s = (c.get("crc_expected") or "").strip().lower()
            if not crc_s:
                continue
            try:
                crc_i = int(crc_s, 16)
            except ValueError:
                continue
            size_e = int(c.get("size_expected") or 0)
            cands = pool.get(crc_i) or []
            pick = None
            for cand in cands:
                # ne pas proposer le même zip
                if own_name and Path(cand["zip"]).name.lower() == own_name:
                    continue
                if size_e and cand.get("size") and cand["size"] != size_e:
                    continue
                pick = cand
                break
            if not pick and cands:
                # fallback sans filtre taille si aucun zip propre
                for cand in cands:
                    if own_name and Path(cand["zip"]).name.lower() == own_name:
                        continue
                    pick = cand
                    break
            if pick:
                c["status"] = "available"
                c["found"] = f"{pick['zip_name']} → {Path(pick['member']).name}"
                c["source_zip"] = pick["zip"]
                c["source_member"] = pick["member"]
                c["crc_found"] = crc_s
                c["size_found"] = pick.get("size") or 0
                c["message"] = f"Dans {pick['zip_name']}"
                n_avail += 1
        if n_avail:
            msg = (row.get("message") or "").strip()
            extra = f"{n_avail} récupérable(s)"
            row["message"] = f"{msg}; {extra}" if msg else extra
            row["available_count"] = n_avail
            # si incomplete/missing/bad et on peut compléter → toujours repairable
            if row.get("status") in ("missing", "incomplete", "bad", "error"):
                pass  # status global inchangé (incomplete reste incomplete)


def scan_mame_nonmerged(
    rom_dir: Path, machines: List[Dict], max_depth: int = 0, deep: bool = False,
) -> List[Dict]:
    """Scan romset MAME non-merged : 1 ZIP par machine, CRC via catalogue ZIP.
    max_depth>0 autorise les zip dans des sous-dossiers (rare, ex. classement A-Z).
    Post-passe : ROMs manquantes trouvées dans d'autres ZIP → status 'available'.
    """
    # Index des zip (stem lower → path) — priorité à la racine (profondeur plus faible)
    zips: Dict[str, str] = {}
    zips_depth: Dict[str, int] = {}
    SCAN_PROGRESS.phase("list")
    for fpath in _iter_rom_paths(rom_dir, max_depth=max_depth):
        SCAN_PROGRESS.step()
        try:
            if fpath.suffix.lower() != ".zip":
                continue
            stem = fpath.stem.lower()
            # profondeur relative
            try:
                rel = fpath.parent.relative_to(rom_dir)
                depth = len(rel.parts)
            except ValueError:
                depth = 0
            prev = zips_depth.get(stem)
            if prev is None or depth < prev:
                zips[stem] = str(fpath)
                zips_depth[stem] = depth
        except OSError:
            continue

    # 1) Catalogue parallèle de tous les ZIP (1 ouverture / fichier — plus de double passe)
    zip_paths = list(zips.values())
    catalog_by_path: Dict[str, Dict[str, Any]] = {}
    workers = max(4, min(_MAX_WORKERS, (os.cpu_count() or 4) * 2))
    if zip_paths:
        SCAN_PROGRESS.phase("archives", total=len(zip_paths))
        task = _tracked(functools.partial(_zip_catalog_task, deep=deep))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for cat in ex.map(task, zip_paths, chunksize=4):
                SCAN_PROGRESS.step()
                catalog_by_path[cat["path"]] = cat
    SCAN_PROGRESS.phase("match", total=len(machines))

    # Pool CRC construit pendant le catalogue
    pool: Dict[int, List[Dict[str, Any]]] = {}
    for cat in catalog_by_path.values():
        zpath = cat["path"]
        zname = cat["name"]
        for fn, sz, crc in cat.get("members") or []:
            pool.setdefault(crc, []).append({
                "zip": zpath,
                "zip_name": zname,
                "member": fn,
                "size": sz,
            })

    # 2) Matching machine ↔ catalogue (CPU, sans I/O)
    tasks = []
    for m in machines:
        key = (m["game"] or "").lower()
        zpath_str = zips.get(key)
        cat = catalog_by_path.get(zpath_str) if zpath_str else None
        tasks.append((m, zpath_str, cat, str(rom_dir)))

    results: List[Dict] = []
    match_workers = max(4, min(_MAX_WORKERS, 32))
    with ThreadPoolExecutor(max_workers=match_workers) as ex:
        for row in ex.map(_scan_one_mame_machine, tasks, chunksize=32):
            results.append(row)

    # 2b) ZIP mal nommés : association rapide par CRC (index inversé)
    try:
        used_zip_paths = {
            (r.get("path") or "").lower()
            for r in results
            if r.get("path") and r.get("status") not in ("missing",)
        }
        # crc → list[(frozenset_crcs, path)]
        crc_to_orphans: Dict[int, List[Tuple[frozenset, str]]] = {}
        orphan_fps: Dict[str, frozenset] = {}
        for path, cat in catalog_by_path.items():
            if path.lower() in used_zip_paths:
                continue
            crcs = frozenset(
                crc for (_fn, _sz, crc) in (cat.get("members") or []) if crc is not None
            )
            if not crcs:
                continue
            orphan_fps[path] = crcs
            # indexer chaque CRC (nécessaire si le zip a des extras hors DAT)
            for c in crcs:
                crc_to_orphans.setdefault(c, []).append((crcs, path))

        by_game = { (m.get("game") or ""): m for m in machines }
        reassigned: List[Tuple[int, Dict]] = []

        # Tous les couples (machine manquante, ZIP orphelin) plausibles, avec leur score :
        #  - recouvrement des ROM requises (≥ 80 %, 1.0 si empreinte exacte),
        #  - part du ZIP qui appartient à cette machine (départage parent / clone : le ZIP
        #    d'un clone contient des ROM propres au clone, absentes du parent).
        # Attribution GLOBALE des meilleurs couples d'abord : un parent ne peut plus
        # « voler » le ZIP mal nommé d'un clone parce qu'il est traité avant lui.
        cand_list: List[Tuple[Tuple[float, float], int, str, Dict]] = []
        for i, row in enumerate(results):
            if (row.get("status") or "") != "missing":
                continue
            game = (row.get("game") or "").strip()
            machine = by_game.get(game)
            if not machine:
                continue
            req = [
                r["crc_int"] for r in (machine.get("roms") or [])
                if r.get("crc_int", -1) >= 0 and r.get("status") != "nodump" and not r.get("optional")
            ]
            if not req:
                continue
            need = frozenset(req)
            seen_p = set()
            for c in need:
                for fp, path in crc_to_orphans.get(c, []):
                    if path in seen_p:
                        continue
                    seen_p.add(path)
                    inter = len(need & fp)
                    score = 1.0 if fp == need else inter / float(len(need))
                    if score < 0.8:
                        continue
                    zip_cov = inter / float(len(fp))
                    cand_list.append(((round(score, 9), round(zip_cov, 9)), i, path, machine))

        cand_list.sort(key=lambda t: t[0], reverse=True)
        keys_by_path: Dict[str, List[Tuple[Tuple[float, float], int]]] = {}
        keys_by_row: Dict[int, List[Tuple[Tuple[float, float], str]]] = {}
        for key, i, path, _m in cand_list:
            keys_by_path.setdefault(path, []).append((key, i))
            keys_by_row.setdefault(i, []).append((key, path))

        taken_rows: set = set()
        taken_paths: set = set()
        for key, i, path, machine in cand_list:
            if i in taken_rows or path in taken_paths:
                continue
            # Égalité parfaite → ambigu : on n'associe pas (comme avant), sauf si le rival
            # a déjà été attribué ailleurs
            if any(k == key and j != i and j not in taken_rows for k, j in keys_by_path[path]):
                continue
            if any(k == key and pp != path and pp not in taken_paths for k, pp in keys_by_row[i]):
                continue
            taken_rows.add(i)
            taken_paths.add(path)
            game = (results[i].get("game") or "").strip()
            zpath_str = path
            cat = catalog_by_path.get(zpath_str)
            new_row = _scan_one_mame_machine((machine, zpath_str, cat, str(rom_dir)))
            # ZIP mal nommé → toujours rename si le contenu est utilisable
            zname = Path(zpath_str).name
            if zname.lower() != (game + ".zip").lower():
                if (new_row.get("status") or "") in ("good", "rename", "incomplete"):
                    if new_row.get("status") == "good":
                        new_row["status"] = "rename"
                    msg = (new_row.get("message") or "").strip()
                    suffix = f"ZIP → {game}.zip"
                    if suffix not in msg:
                        new_row["message"] = f"{msg} · {suffix}".strip(" ·")
            reassigned.append((i, new_row))
            used_zip_paths.add(zpath_str.lower())

        for i, new_row in reassigned:
            results[i] = new_row
    except Exception:
        traceback.print_exc()

    # 3) Annoter ROMs manquantes récupérables ailleurs
    try:
        _annotate_available_roms(results, pool)
    except Exception:
        traceback.print_exc()

    # ZIPs orphelins restants (non associés)
    matched_paths = {
        (r.get("path") or "").lower()
        for r in results
        if r.get("path")
    }
    for stem, path in zips.items():
        if path.lower() in matched_paths:
            continue
        results.append({
            "status": "bad",
            "game": "",
            "expected": "",
            "found": Path(path).name,
            "path": path,
            "size": Path(path).stat().st_size if Path(path).exists() else 0,
            "crc": "", "sha1": "", "md5": "",
            "is_zip": True, "zip_member": "",
            "cloneof": "", "parent": "", "is_clone": False,
            "mtype": "extra", "driver_status": "", "bios": "",
            "description": "",
            "message": "ZIP hors DAT",
        })

    # Flag repairable (pour l'UI sans components[])
    for r in results:
        r["repairable"] = _row_is_repairable(r)

    order = {"good": 0, "rename": 1, "incomplete": 2, "bad": 3, "missing": 4, "error": 5}
    results.sort(key=lambda r: (
        order.get(r["status"], 9),
        (r.get("game") or r.get("found") or "").lower(),
    ))
    return results


def scan_roms(*args, **kwargs) -> List[Dict]:
    """Scan d'un dossier (voir _scan_roms) ; avancement lisible via GET /api/scan/progress."""
    SCAN_PROGRESS.begin()
    try:
        return _scan_roms(*args, **kwargs)
    finally:
        SCAN_PROGRESS.end()


def _scan_roms(
    rom_dir: Path,
    rom_map: Dict,
    games_list: List[Dict],
    size_set: Optional[set] = None,
    hash_mode: str = "crc",
    dat_mode: str = "standard",
    max_depth: int = 0,
    deep: bool = False,
    quick_id: bool = True,
) -> List[Dict]:
    """
    deep=True : contenu des archives décompressé et contrôlé, CRC complet des gros fichiers.
    quick_id=True : gros fichiers identifiés par nom + taille, jamais lus (voir _QuickIndex).
    """
    max_depth = max(0, min(int(max_depth or 0), 2))
    if dat_mode == "arcade":
        return scan_mame_nonmerged(rom_dir, games_list, max_depth=max_depth, deep=deep)
    if size_set is None:
        size_set = {g["size"] for g in games_list if g.get("size")}

    # Sous-dossiers auto : CHD, SHA1, ou softlist (dump_name)
    has_disks = any(g.get("is_disk") for g in games_list)
    has_softlist = any(g.get("dump_name") for g in games_list)
    if max_depth == 0 and (hash_mode == "sha1" or has_disks or has_softlist):
        max_depth = 2

    # Set 100 % CHD/disk : inutile d'ouvrir les ZIP (gain net sur gros dossiers mixtes)
    n_disk = sum(1 for g in games_list if g.get("is_disk"))
    skip_zips = n_disk > 0 and n_disk == len(games_list)

    results: List[Dict] = []
    inventory = _collect_files(
        rom_dir, size_set, hash_mode, max_depth=max_depth, skip_zips=skip_zips, deep=deep,
        games_list=games_list, quick_id=quick_id,
    )
    SCAN_PROGRESS.phase("match")

    # Index hash → TOUS les fichiers (un même contenu peut exister en plusieurs exemplaires)
    by_crc: Dict[Tuple, List[Dict]] = {}
    by_crc_only: Dict[int, List[Dict]] = {}  # fallback si taille DAT ≠ fichier
    by_sha1: Dict[str, List[Dict]] = {}

    for it in inventory:
        if it.get("error") or not it.get("hashes"):
            continue
        if it.get("size_mismatch") or it.get("corrupt"):
            continue
        h = it["hashes"]
        crc_i = h.get("crc_int", -1)
        if crc_i is not None and crc_i >= 0:
            by_crc.setdefault((crc_i, h["size"]), []).append(it)
            by_crc_only.setdefault(crc_i, []).append(it)
        sha1 = (h.get("sha1") or "").lower()
        if sha1:
            by_sha1.setdefault(sha1, []).append(it)

    def _candidates(g: Dict) -> List[Dict]:
        """Fichiers dont le contenu correspond à l'entrée DAT, du plus sûr au moins sûr."""
        out: List[Dict] = []
        sha1 = (g.get("sha1") or "").lower()
        # CHD / mode SHA1 pur
        if g.get("is_disk") or (hash_mode == "sha1" and (g.get("crc_int") is None or int(g.get("crc_int") or -1) < 0)):
            out.extend(by_sha1.get(sha1, []) if sha1 else [])
            if g.get("is_disk"):
                return out
        # CRC (+ taille), puis CRC seul, puis SHA1
        crc_i = g.get("crc_int")
        if crc_i is None:
            try:
                crc_i = int(g["crc"], 16) if g.get("crc") else -1
            except ValueError:
                crc_i = -1
        if crc_i is not None and crc_i >= 0:
            out.extend(by_crc.get((crc_i, g.get("size") or 0), []))
            out.extend(by_crc_only.get(crc_i, []))
        if sha1:
            out.extend(by_sha1.get(sha1, []))
        seen_c: set = set()
        uniq = []
        for it in out:
            if id(it) not in seen_c:
                seen_c.add(id(it))
                uniq.append(it)
        return uniq

    def _name_ok(g: Dict, it: Dict) -> bool:
        if it["is_zip"]:
            zname = Path(it["path"]).name
            member = it.get("zip_member") or ""
            # softlist : membre = dump_name MAME (ex. c1001_cubeup.bin)
            member_ok = (
                _names_match(member, g["rom_name"])
                or _names_match(member, g.get("dump_name") or "")
            )
            ext = Path(zname).suffix.lower()
            ext = ext if ext in _ARCHIVE_EXTS else ".zip"  # Jeu.zip ou Jeu.7z
            return _names_match(zname, g["game"] + ext) and member_ok
        found_name = Path(it["found"]).name
        return (
            _names_match(found_name, g["rom_name"])
            or _names_match(found_name, g.get("dump_name") or "")
        )

    # Attribution : un fichier ne sert qu'UNE entrée DAT.
    # Passe 1 : fichiers déjà au bon nom ; passe 2 : contenu identique, nom à corriger.
    cands_by_g: List[List[Dict]] = [_candidates(g) for g in games_list]
    assigned: Dict[int, Dict] = {}
    owner: Dict[int, int] = {}  # id(fichier) → index de l'entrée DAT qui l'utilise
    candidate_ids: set = set()
    for gi, cands in enumerate(cands_by_g):
        candidate_ids.update(id(it) for it in cands)
        for it in cands:
            if id(it) not in owner and _name_ok(games_list[gi], it):
                assigned[gi] = it
                owner[id(it)] = gi
                break
    for gi, cands in enumerate(cands_by_g):
        if gi in assigned:
            continue
        for it in cands:
            if id(it) not in owner:
                assigned[gi] = it
                owner[id(it)] = gi
                break

    for gi, g in enumerate(games_list):
        it = assigned.get(gi)
        if it is None:
            message = ""
            cands = cands_by_g[gi]
            if cands:
                # Contenu présent mais déjà utilisé par une autre entrée : ne JAMAIS
                # proposer de le renommer (casserait l'autre jeu) → il faut une copie.
                other = games_list[owner[id(cands[0])]]["game"] if id(cands[0]) in owner else ""
                message = f"Même contenu que {cands[0]['found']}"
                if other and other != g["game"]:
                    message += f" (déjà utilisé par {other})"
                message += " : copie nécessaire"
            results.append({
                "status": "missing", "game": g["game"], "expected": g["rom_name"],
                "found": "", "path": "", "size": g["size"], "crc": g.get("crc", ""),
                "md5": g.get("md5", ""), "sha1": g.get("sha1", ""), "is_zip": False,
                "zip_member": "", "message": message,
                "description": g.get("description") or "",
                "cloneof": g.get("cloneof") or "",
            })
            continue

        h = it["hashes"]
        status = "good" if _name_ok(g, it) else "rename"
        results.append({
            "status": status,
            "game": g["game"],
            "expected": g["rom_name"],
            "found": it["found"],
            "path": it["path"],
            "size": h.get("size", 0),
            "crc": h.get("crc", ""),
            "md5": h.get("md5", ""),
            "sha1": h.get("sha1", ""),
            "is_zip": it["is_zip"],
            "zip_member": it.get("zip_member") or "",
            "message": QUICK_MATCH_MSGS.get(it.get("quick") or "", ""),
            "quick": bool(it.get("quick")),
            "description": g.get("description") or "",
            "cloneof": g.get("cloneof") or "",
        })

    for it in inventory:
        if id(it) in owner:
            continue
        if it.get("error"):
            results.append(_err_row(
                it["found"], it["path"], it["error"],
                is_zip=it["is_zip"], member=it.get("zip_member") or "",
            ))
            continue
        h = it.get("hashes") or {}
        if it.get("corrupt"):
            results.append({
                "status": "bad", "game": "", "expected": "", "found": it["found"],
                "path": it["path"], "size": h.get("size", 0), "crc": h.get("crc", ""),
                "md5": "", "sha1": "", "is_zip": it["is_zip"],
                "zip_member": it.get("zip_member") or "",
                "message": f"Données corrompues (vérification approfondie) : {it['corrupt']}",
            })
            continue
        if it.get("size_mismatch"):
            results.append({
                "status": "bad", "game": "", "expected": "", "found": it["found"],
                "path": it["path"], "size": h.get("size", 0), "crc": "",
                "md5": "", "sha1": "", "is_zip": it["is_zip"],
                "zip_member": it.get("zip_member") or "",
                "message": "Taille hors DAT (CRC non calcule)",
            })
            continue
        if it.get("unidentified"):
            results.append({
                "status": "bad", "game": "", "expected": "", "found": it["found"],
                "path": it["path"], "size": h.get("size", 0), "crc": "",
                "md5": "", "sha1": "", "is_zip": False, "zip_member": "",
                "message": UNIDENTIFIED_MSG, "unidentified": True,
            })
            continue
        if id(it) in candidate_ids:
            msg = "Doublon (meme contenu)"
        else:
            msg = "SHA1 inconnu" if hash_mode == "sha1" else "CRC inconnu"
        results.append({
            "status": "bad", "game": "", "expected": "", "found": it["found"],
            "path": it["path"], "size": h.get("size", 0), "crc": h.get("crc", ""),
            "md5": "", "sha1": h.get("sha1", ""), "is_zip": it["is_zip"],
            "zip_member": it.get("zip_member") or "", "message": msg,
        })

    order = {"good": 0, "rename": 1, "bad": 2, "missing": 3, "error": 4}
    results.sort(key=lambda r: (order.get(r["status"], 9), r["game"].lower(), r["found"].lower()))
    return results


# ---------------------------------------------------------------------------
#  Écritures sûres : remplacement atomique, corbeille, copie de membres ZIP
# ---------------------------------------------------------------------------

_REPLACE_RETRIES = 6
_REPLACE_DELAY = 0.2  # secondes, croissant à chaque essai
TRASH_DIR_NAME = ".rsv_trash"  # ignoré par le scan (dossier commençant par ".")
_TRASH_SESSION = time.strftime("%Y%m%d-%H%M%S")


def _safe_replace(tmp: Path, dst: Path) -> None:
    """
    Remplace dst par tmp sans JAMAIS supprimer dst au préalable.
    Retente quelques fois (antivirus, Explorateur, NAS qui verrouillent un instant).
    Échec définitif : dst reste intact, tmp est supprimé, RuntimeError.
    Si dst n'existe pas (création), tmp est conservé pour ne rien perdre.
    """
    tmp, dst = Path(tmp), Path(dst)
    last: Optional[OSError] = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(str(tmp), str(dst))
            return
        except OSError as e:
            last = e
            if attempt < _REPLACE_RETRIES - 1:
                time.sleep(_REPLACE_DELAY * (attempt + 1))
    if dst.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RuntimeError(f"Fichier verrouillé, original conservé ({dst.name}) : {last}")
    raise RuntimeError(f"Écriture impossible de {dst.name} (copie laissée dans {tmp.name}) : {last}")


def _trash_root_for(path: Path) -> Path:
    """Dossier sous lequel créer la corbeille : racine ROMs du scan si path y est, sinon son dossier."""
    roms = (STATE.get("roms_path") or "").strip()
    if roms:
        try:
            Path(path).resolve().relative_to(Path(roms).resolve())
            return Path(roms)
        except (ValueError, OSError):
            pass
    return Path(path).parent


def _trash_dest(path: Path, sub: str = "") -> Path:
    """Chemin libre dans <racine>/.rsv_trash/<session>/<chemin relatif>."""
    path = Path(path)
    root = _trash_root_for(path)
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        rel = Path(path.name)
    dest = root / TRASH_DIR_NAME / _TRASH_SESSION / rel
    if sub:
        dest = dest / sub
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        n = 2
        while True:
            cand = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
            if not cand.exists():
                dest = cand
                break
            n += 1
    return dest


def move_to_trash(path: Path, copy: bool = False) -> Path:
    """Déplace (ou copie) un fichier dans la corbeille de l'application. Retourne sa destination."""
    path = Path(path)
    dest = _trash_dest(path)
    if copy:
        shutil.copy2(str(path), str(dest))
    else:
        try:
            os.replace(str(path), str(dest))
        except OSError:
            shutil.move(str(path), str(dest))  # autre volume
    return dest


def _copy_zip_member(
    zin: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    zout: zipfile.ZipFile,
    new_name: Optional[str] = None,
    compress_type: Optional[int] = None,
    compresslevel: Optional[int] = None,
) -> None:
    """Copie un membre ZIP en flux (pas de chargement complet en mémoire)."""
    zi = zipfile.ZipInfo(filename=new_name or info.filename, date_time=info.date_time)
    zi.compress_type = info.compress_type if compress_type is None else compress_type
    zi.external_attr = info.external_attr
    zi.create_system = info.create_system
    zi.comment = info.comment
    zi.file_size = info.file_size  # décide du ZIP64 avant écriture
    if compresslevel is not None:
        # écrire avec une ZipInfo ignore le compresslevel du ZipFile : le fixer ici
        if hasattr(zi, "compress_level"):
            zi.compress_level = compresslevel  # Python 3.13+
        else:
            zi._compresslevel = compresslevel  # type: ignore[attr-defined]
    with zin.open(info, "r") as src, zout.open(zi, "w", force_zip64=info.file_size > 0xFFFF0000) as dst:
        shutil.copyfileobj(src, dst, _READ_BUF)


def _remove_zip_members(zip_path: Path, members: List[str]) -> Tuple[int, int]:
    """
    Retire des membres d'un ZIP (réécriture en flux + remplacement sûr).
    Chaque membre retiré est d'abord extrait dans la corbeille.
    Retourne (nb retirés, nb restants).
    """
    zip_path = Path(zip_path)
    wanted = {_norm_zip_name(m) for m in members if m}
    tmp = zip_path.with_name(zip_path.name + ".rsv_tmp")
    if tmp.exists():
        tmp.unlink()
    removed = kept = 0
    try:
        with zipfile.ZipFile(str(zip_path), "r") as zin, zipfile.ZipFile(str(tmp), "w") as zout:
            for info in zin.infolist():
                if _norm_zip_name(info.filename) in wanted:
                    dest = _trash_dest(zip_path, sub=Path(info.filename).name)
                    with zin.open(info, "r") as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out, _READ_BUF)
                    removed += 1
                    continue
                if info.is_dir():
                    zout.writestr(info, b"")
                    continue
                _copy_zip_member(zin, info, zout)
                kept += 1
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    if not removed:
        tmp.unlink()
        return 0, kept
    _safe_replace(tmp, zip_path)
    return removed, kept


def _norm_zip_name(name: str) -> str:
    """Normalise un chemin de membre ZIP (toujours des /)."""
    return (name or "").replace("\\", "/").strip()


def _batch_rename_zip_members(zip_path: Path, mapping: Dict[str, str]) -> List[str]:
    """
    Renomme plusieurs membres d'un ZIP en une seule réécriture.
    mapping: chemin_actuel_dans_zip → nom_cible (basename à la racine).
    Windows : lit tout, ferme le ZIP, puis remplace le fichier.
    """
    raw = {
        _norm_zip_name(o): _norm_zip_name(n)
        for o, n in (mapping or {}).items()
        if o and n
    }
    raw = {o: n for o, n in raw.items() if o != n}
    if not raw:
        return []

    targets = list(raw.values())
    if len(targets) != len(set(targets)):
        raise RuntimeError("Collision : deux ROMs renommées vers le même nom")

    applied: List[str] = []

    with zipfile.ZipFile(str(zip_path), "r") as zin:
        real_names: Dict[str, str] = {}
        for info in zin.infolist():
            if info.is_dir():
                continue
            real_names[_norm_zip_name(info.filename)] = info.filename

        resolved: Dict[str, str] = {}
        for old, new in raw.items():
            real_old = real_names.get(old)
            if real_old is None:
                base = Path(old).name.lower()
                matches = [
                    real for norm, real in real_names.items()
                    if Path(norm).name.lower() == base
                ]
                if len(matches) == 1:
                    real_old = matches[0]
                elif not matches:
                    raise RuntimeError(f"Membre introuvable dans le ZIP : {old}")
                else:
                    raise RuntimeError(f"Plusieurs membres correspondent à : {old}")
            new_norm = _norm_zip_name(new)
            if new_norm in real_names and real_names[new_norm] != real_old:
                sources_real = set()
                for o2 in raw:
                    r2 = real_names.get(o2)
                    if r2 is None:
                        b2 = Path(o2).name.lower()
                        m2 = [real for norm, real in real_names.items() if Path(norm).name.lower() == b2]
                        if len(m2) == 1:
                            r2 = m2[0]
                    if r2:
                        sources_real.add(r2)
                if real_names[new_norm] not in sources_real:
                    raise RuntimeError(f"Cible déjà présente dans le ZIP : {new}")
            resolved[real_old] = Path(new).name

        # Écriture en flux (source ouverte en lecture seule), remplacement après fermeture
        tmp = zip_path.with_name(zip_path.name + ".rsv_tmp")
        if tmp.exists():
            tmp.unlink()
        try:
            with zipfile.ZipFile(str(tmp), "w") as zout:
                for item in zin.infolist():
                    if item.is_dir():
                        zout.writestr(item, b"")
                        continue
                    filename = resolved.get(item.filename, item.filename)
                    if filename != item.filename:
                        applied.append(f"{item.filename} → {filename}")
                    _copy_zip_member(zin, item, zout, new_name=filename)
        except Exception:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise

    _safe_replace(tmp, zip_path)
    return applied



def _arcade_rename_mapping(row: Dict) -> Dict[str, str]:
    """
    Construit old→new pour les composantes ROM en statut rename (mode arcade non-merged).
    """
    mapping: Dict[str, str] = {}
    for c in row.get("components") or []:
        kind = (c.get("kind") or "rom").lower()
        if kind not in ("rom",):
            continue
        if (c.get("status") or "") != "rename":
            continue
        found = _norm_zip_name(c.get("found") or "")
        expected = _norm_zip_name(c.get("expected") or "")
        if not found or not expected:
            continue
        mapping[found] = Path(expected).name
    return mapping


def _row_has_internal_renames(row: Dict) -> bool:
    return any(
        (c.get("kind") or "rom") == "rom" and (c.get("status") or "") == "rename"
        for c in (row.get("components") or [])
    )



def _row_is_repairable(row: Dict) -> bool:
    """True si une action Réparer/Renommer a un effet possible."""
    if not row:
        return False
    st = (row.get("status") or "").lower()
    if st == "rename":
        return True
    if (row.get("available_count") or 0) > 0:
        return True
    game = (row.get("game") or "").strip()
    path_s = (row.get("path") or "").strip()
    if game and path_s and path_s.lower().endswith(".zip"):
        # ZIP au mauvais nom (jamais pour un fichier loose ni un CHD)
        try:
            if Path(path_s).name.lower() != (game + ".zip").lower():
                if st in ("good", "rename", "incomplete", "bad"):
                    return True
        except Exception:
            pass
    comps = row.get("components") or []
    present_crcs = set()
    for c in comps:
        cst = (c.get("status") or "").lower()
        if cst in ("rename", "extra", "available"):
            return True
        if cst in ("ok", "rename", "bad_size") and (c.get("crc_expected") or c.get("crc_found")):
            present_crcs.add((c.get("crc_expected") or c.get("crc_found") or "").lower())
    # incomplete : ROM manquante mais même CRC déjà présent → duplication possible
    if st in ("incomplete", "missing", "bad"):
        for c in comps:
            if (c.get("status") or "").lower() != "missing":
                continue
            crc = (c.get("crc_expected") or "").lower()
            if crc and crc in present_crcs:
                return True
    return False


_PLAYLIST_EXTS = (".cue", ".gdi", ".m3u", ".m3u8")
_PLAYLIST_MAX = 1024 * 1024  # 1 Mio : au-delà ce n'est pas une feuille de pistes


def _update_playlist_refs(folder: Path, old_name: str, new_name: str) -> List[str]:
    """
    Après le renommage old_name → new_name dans folder, réécrit les .cue / .gdi / .m3u
    du même dossier qui citent l'ancien nom (FILE "x.bin" BINARY, piste .gdi, ligne .m3u).
    Seule une référence entière est remplacée (bornée par début de ligne, espace, guillemet
    ou séparateur de chemin). Copie de l'original dans la corbeille avant écriture.
    Un .cue conforme au DAT ne cite que les noms du DAT : il n'est donc jamais modifié.
    Retourne des notes « Jeu.cue mis à jour ».
    """
    if not old_name or old_name == new_name:
        return []
    pattern = re.compile(
        r'(?<![^\s"/\\])' + re.escape(old_name) + r'(?![^\s"])',
        re.IGNORECASE,
    )
    notes: List[str] = []
    try:
        entries = list(Path(folder).iterdir())
    except OSError:
        return notes
    for pl in entries:
        try:
            if pl.suffix.lower() not in _PLAYLIST_EXTS or not pl.is_file():
                continue
            if pl.stat().st_size > _PLAYLIST_MAX:
                continue
            raw = pl.read_bytes()
            for codec in ("utf-8", "cp1252", "latin-1"):
                try:
                    text = raw.decode(codec)
                    break
                except UnicodeDecodeError:
                    continue
            new_text, n = pattern.subn(lambda _m: new_name, text)
            if not n:
                continue
            try:
                data = new_text.encode(codec)
            except UnicodeEncodeError:
                notes.append(f"{pl.name} non mis à jour : « {new_name} » impossible à écrire en {codec}")
                continue
            move_to_trash(pl, copy=True)
            tmp = pl.with_name(pl.name + ".rsv_tmp")
            tmp.write_bytes(data)
            _safe_replace(tmp, pl)
            notes.append(f"{pl.name} mis à jour ({n} référence(s))")
        except Exception as e:  # une feuille illisible ne doit pas faire échouer le renommage
            notes.append(f"{pl.name} non mis à jour : {e}")
    return notes


def do_rename(row: Dict) -> Tuple[bool, str]:
    """
    Corrige les noms. Mode arcade (MAME/FBNeo) → toujours do_repair.
    """
    if (STATE.get("dat_mode") or "") == "arcade" or row.get("mtype") or row.get("components"):
        if row.get("is_zip") or (row.get("path") or "").lower().endswith(".zip"):
            return do_repair(row)

    path_s = (row.get("path") or "").strip()
    if not path_s:
        return False, "Pas de fichier"
    src = Path(path_s)
    if not src.exists():
        return False, "Introuvable"

    has_internal = _row_has_internal_renames(row)
    status = (row.get("status") or "").lower()
    game = (row.get("game") or "").strip()
    expected = (row.get("expected") or "").strip()

    # Autoriser rename si statut rename, ou composants internes à corriger,
    # ou zip mal nommé (expected se termine par .zip / game connu)
    # Extension de l'archive : Jeu.zip ou Jeu.7z
    arch_ext = src.suffix.lower() if src.suffix.lower() in _ARCHIVE_EXTS else ".zip"
    zip_name_wrong = False
    if row.get("is_zip") and game:
        zip_name_wrong = src.name.lower() != (game + arch_ext).lower()
    elif row.get("is_zip") and expected.lower().endswith(".zip"):
        zip_name_wrong = src.name.lower() != expected.lower()

    if status not in ("rename",) and not has_internal and not zip_name_wrong:
        # Dernière chance : standard rename
        if status != "rename":
            return False, "Rien à renommer"

    try:
        notes: List[str] = []

        if row.get("is_zip"):
            mapping = _arcade_rename_mapping(row)

            # Mode standard : un seul zip_member si pas de components arcade
            if not mapping:
                member = _norm_zip_name(row.get("zip_member") or "")
                rom_target = expected
                if rom_target.lower().endswith(".zip"):
                    rom_target = ""
                if member and rom_target:
                    target = Path(rom_target).name
                    if Path(member).name != target or member != target:
                        mapping[member] = target

            if mapping and arch_ext == ".7z":
                return False, (
                    "Renommage d'un fichier dans un .7z impossible (archive en lecture seule) : "
                    "renommez-le à la main ou convertissez l'archive en .zip"
                )
            if mapping:
                applied = _batch_rename_zip_members(src, mapping)
                notes.extend(applied)
                # MAJ components
                # mapping keys may be normalized; match flexibly
                norm_map = {_norm_zip_name(k): v for k, v in mapping.items()}
                for c in row.get("components") or []:
                    if (c.get("status") or "") != "rename":
                        continue
                    found = _norm_zip_name(c.get("found") or "")
                    exp = Path(_norm_zip_name(c.get("expected") or "")).name
                    # direct or basename
                    new_name = norm_map.get(found)
                    if new_name is None:
                        for ok, nv in norm_map.items():
                            if Path(ok).name.lower() == Path(found).name.lower():
                                new_name = nv
                                break
                    if new_name and exp and new_name.lower() == exp.lower():
                        c["found"] = new_name
                        c["status"] = "ok"
                        c["message"] = "OK"
                        if c.get("crc_expected"):
                            c["crc_found"] = c.get("crc_expected")

            # Conteneur .zip
            zip_target_name = None
            if game:
                zip_target_name = game + arch_ext
            elif expected.lower().endswith(".zip"):
                zip_target_name = Path(expected).name

            dst = src
            if zip_target_name and src.name.lower() != zip_target_name.lower():
                dst = src.parent / zip_target_name
                if dst.exists() and dst.resolve() != src.resolve():
                    return False, f"Existe déjà : {dst.name}"
                if dst.resolve() != src.resolve():
                    src.rename(dst)
                    notes.append(f"{src.name} → {dst.name}")
                    notes.extend(_update_playlist_refs(dst.parent, src.name, dst.name))

            row["path"] = str(dst)
            if row.get("components") is not None:
                row["found"] = dst.name
                row["zip_member"] = ""
            else:
                member = _norm_zip_name(row.get("zip_member") or "")
                if mapping and member:
                    member = mapping.get(member) or mapping.get(Path(member).name) or member
                    for o, n in mapping.items():
                        if Path(o).name.lower() == Path(member).name.lower():
                            member = n
                            break
                row["zip_member"] = member
                row["found"] = f"{dst.name} → {member}" if member else dst.name

            # Recalcul statut global
            comps = row.get("components") or []
            if comps:
                still_bad = any(
                    (c.get("status") or "") in ("missing", "bad_crc", "bad_size", "rename")
                    for c in comps
                    if (c.get("kind") or "rom") in ("rom", "chd")
                )
                still_rename = any((c.get("status") or "") == "rename" for c in comps)
                if still_bad and not still_rename:
                    # missing/bad remain
                    if any((c.get("status") or "") in ("missing", "bad_crc", "bad_size") for c in comps):
                        row["status"] = "incomplete" if any(
                            (c.get("status") or "") == "ok" for c in comps
                        ) else "bad"
                    else:
                        row["status"] = "good"
                elif still_rename:
                    row["status"] = "rename"
                else:
                    row["status"] = "good"
                    # nettoyer message
                    n_ok = sum(1 for c in comps if c.get("status") == "ok")
                    row["message"] = f"{n_ok} ROM OK"
            else:
                row["status"] = "good"

            msg = " ; ".join(notes[:8]) if notes else f"→ {dst.name}"
            if len(notes) > 8:
                msg += f" … (+{len(notes)-8})"
            if not notes and row["status"] == "good":
                msg = f"→ {dst.name}"
            if not notes and not mapping and not zip_name_wrong:
                return False, "Rien à renommer"
            return True, msg

        # Loose file (ROM / CHD)
        target_name = (expected or game or "").strip()
        if not target_name:
            return False, "Nom cible inconnu"
        # expected peut être un chemin ou un .zip : ne garder que le nom de fichier
        target_name = Path(target_name).name
        if target_name.lower().endswith(".zip") and src.suffix.lower() != ".zip":
            # pas un zip : préférer le nom du jeu + extension source
            if game:
                target_name = game + (src.suffix if src.suffix else "")
            else:
                target_name = src.name
        # CHD : garantir l'extension .chd
        if src.suffix.lower() == ".chd" and not target_name.lower().endswith(".chd"):
            target_name = _chd_filename(target_name)

        dst = src.parent / target_name
        # Même fichier (casse différente sous Windows) ?
        same = False
        try:
            same = src.resolve() == dst.resolve()
        except OSError:
            same = src.name.lower() == dst.name.lower() and src.parent == dst.parent

        if same and src.name == dst.name:
            row["status"] = "good"
            row["found"] = src.name
            return True, "Déjà correct"

        if dst.exists() and not same:
            # Collision réelle
            return False, f"Existe déjà : {dst.name}"

        if same and src.name != dst.name:
            # Rename case-only Windows : passage par un nom temporaire
            tmp = src.parent / (src.stem + ".__rsv_tmp__" + src.suffix)
            if tmp.exists():
                return False, f"Temp existe : {tmp.name}"
            src.rename(tmp)
            tmp.rename(dst)
        elif not same:
            src.rename(dst)
        HASH_CACHE.move(src, dst)

        row["path"] = str(dst)
        row["found"] = dst.name
        row["expected"] = expected or target_name
        row["status"] = "good"
        # .cue / .gdi / .m3u voisins qui citent l'ancien nom : mis à jour (sinon jeu cassé)
        refs = _update_playlist_refs(dst.parent, src.name, dst.name)
        return True, " ; ".join([f"{src.name} → {dst.name}"] + refs)
    except Exception as e:
        return False, str(e)




def do_repair(row: Dict) -> Tuple[bool, str]:
    """
    Répare un set arcade (MAME / FBNeo) non-merged — version robuste.

    Sources de vérité (dans l'ordre) :
      1) CRC du ZIP sur disque vs DAT
      2) components[] du scan (found → expected) si CRC rate
    Windows : lecture complète → fermeture → écriture temp → os.replace.
    Vérification obligatoire avant succès.
    """
    game = (row.get("game") or "").strip()
    path_s = (row.get("path") or "").strip()
    _repair_log(f"REPAIR start game={game!r} path={path_s!r} status={row.get('status')!r}")

    if not game:
        return False, "Pas de nom de jeu"

    # Machine DAT (insensible à la casse)
    machine = None
    gl = STATE.get("games_list") or []
    for m in gl:
        if (m.get("game") or "") == game:
            machine = m
            break
    if machine is None:
        game_l = game.lower()
        for m in gl:
            if (m.get("game") or "").lower() == game_l:
                machine = m
                game = m.get("game") or game
                break
    if machine is None:
        msg = f"Jeu absent du DAT chargé : {game}"
        _repair_log(msg)
        return False, msg

    src = Path(path_s) if path_s else None
    notes: List[str] = []

    def _rom_crc_int(r: Dict) -> int:
        v = r.get("crc_int", -1)
        try:
            v = int(v)
        except (TypeError, ValueError):
            v = -1
        if v < 0:
            cs = (r.get("crc") or "").strip().lower()
            if cs:
                try:
                    v = int(cs, 16)
                except ValueError:
                    v = -1
        return v

    roms_req: List[Dict] = []
    for r in (machine.get("roms") or []):
        st = (r.get("status") or "good").lower()
        if st == "nodump":
            continue
        crc_i = _rom_crc_int(r)
        if crc_i < 0:
            continue
        # les ROM optionnelles présentes sont aussi remises au bon nom
        rr = dict(r)
        rr["crc_int"] = crc_i
        roms_req.append(rr)

    _repair_log(f"  dat_roms={len(roms_req)} components={len(row.get('components') or [])}")

    # ZIP manquant : reconstruction depuis available
    if not src or not src.is_file():
        components = list(row.get("components") or [])
        avail = [
            c for c in components
            if (c.get("kind") or "rom") == "rom"
            and (c.get("status") or "") == "available"
            and c.get("source_zip") and c.get("source_member")
        ]
        if not avail:
            msg = "ZIP absent et aucune ROM récupérable ailleurs"
            _repair_log(msg)
            return False, msg
        dest_dir = Path(avail[0]["source_zip"]).parent
        src = dest_dir / (game + ".zip")
        tmp = src.with_name(src.name + ".rsv_repair")
        try:
            if tmp.exists():
                tmp.unlink()
            written = 0
            with zipfile.ZipFile(str(tmp), "w", compression=zipfile.ZIP_DEFLATED) as zout:
                used = set()
                for c in avail:
                    expected = Path(_norm_zip_name(c.get("expected") or "")).name
                    if not expected or expected in used:
                        continue
                    try:
                        with zipfile.ZipFile(str(c["source_zip"]), "r") as ez:
                            _copy_zip_member(
                                ez, ez.getinfo(c["source_member"]), zout,
                                new_name=expected, compress_type=zipfile.ZIP_DEFLATED,
                            )
                        used.add(expected)
                        written += 1
                        notes.append(f"← {Path(c['source_zip']).name} → {expected}")
                    except Exception as e:
                        notes.append(f"Échec {expected}: {e}")
            if written == 0:
                if tmp.exists():
                    tmp.unlink()
                return False, "Impossible de reconstruire"
            if src.exists():
                # un fichier homonyme existe : le sauvegarder, jamais l'effacer
                saved = move_to_trash(src, copy=True)
                notes.append(f"ancien {src.name} sauvegardé → {saved}")
            _safe_replace(tmp, src)
            notes.insert(0, f"ZIP créé ({written} ROM)")
        except Exception as e:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            _repair_log(f"  rebuild fail: {e}")
            return False, str(e)
    else:
        if src.suffix.lower() != ".zip":
            return False, "Pas un ZIP"

        try:
            by_crc: Dict[int, List[Tuple[str, int]]] = {}
            by_base: Dict[str, str] = {}  # basename lower → real name
            # sources retenues (nom réel dans le ZIP, ou "ext:<zip>|<membre>") ; copiées
            # en flux à l'écriture, jamais chargées en mémoire
            selected: set = set()
            all_reals: List[str] = []
            all_files: List[str] = []  # tous les membres fichiers (vides inclus)

            with zipfile.ZipFile(str(src), "r") as zin:
                for info in zin.infolist():
                    if info.is_dir():
                        continue
                    all_files.append(info.filename)
                    if info.file_size <= 0:
                        continue
                    crc = info.CRC & 0xFFFFFFFF
                    by_crc.setdefault(crc, []).append((info.filename, info.file_size))
                    by_base[Path(info.filename).name.lower()] = info.filename
                    all_reals.append(info.filename)

                # plan: expected_basename → source (nom réel, ou ext:<zip>|<membre>)
                plan: Dict[str, str] = {}

                # 1) Match DAT CRC → fichier local
                #    Passe A : réserver les noms exacts (évite de voler 107.b2 pour 104.f5)
                used_sources: set = set()
                required_basenames = {
                    Path(r.get("name") or "").name.lower()
                    for r in roms_req
                    if r.get("name")
                }
                for r in roms_req:
                    expected = Path(r.get("name") or "").name
                    if not expected or expected in plan:
                        continue
                    crc_i = int(r["crc_int"])
                    cands = by_crc.get(crc_i) or []
                    size_e = int(r.get("size") or 0)
                    exp_l = expected.lower()
                    hit = None
                    for real, sz in cands:
                        if real in used_sources:
                            continue
                        if Path(real).name.lower() == exp_l and (not size_e or sz == size_e):
                            hit = real
                            break
                    if hit is None:
                        for real, sz in cands:
                            if real in used_sources:
                                continue
                            if Path(real).name.lower() == exp_l:
                                hit = real
                                break
                    if hit is not None:
                        plan[expected] = hit
                        used_sources.add(hit)
                        if hit not in selected:
                            selected.add(hit)

                # Passe B : CRC sur un membre dont le nom n'est PAS un autre nom DAT
                for r in roms_req:
                    expected = Path(r.get("name") or "").name
                    if not expected or expected in plan:
                        continue
                    crc_i = int(r["crc_int"])
                    cands = by_crc.get(crc_i) or []
                    size_e = int(r.get("size") or 0)
                    hit = None
                    if size_e:
                        for real, sz in cands:
                            if real in used_sources:
                                continue
                            if Path(real).name.lower() in required_basenames:
                                continue
                            if sz == size_e:
                                hit = real
                                break
                    if hit is None:
                        for real, sz in cands:
                            if real in used_sources:
                                continue
                            if Path(real).name.lower() in required_basenames:
                                continue
                            hit = real
                            break
                    if hit is not None:
                        plan[expected] = hit
                        used_sources.add(hit)
                        selected.add(hit)

                # Passe C : même CRC déjà lu → DUPLIQUER sous le nom attendu
                for r in roms_req:
                    expected = Path(r.get("name") or "").name
                    if not expected or expected in plan:
                        continue
                    crc_i = int(r["crc_int"])
                    cands = by_crc.get(crc_i) or []
                    if not cands:
                        continue
                    hit = None
                    for real, sz in cands:
                        if real in selected:
                            hit = real
                            break
                    if hit is None:
                        hit = cands[0][0]
                    plan[expected] = hit
                    selected.add(hit)
                    notes.append(f"dupliquer {Path(hit).name} → {expected}")

                # 2) Fallback : components du scan (rename/ok/extra with matching CRC)
                for c in (row.get("components") or []):
                    kind = (c.get("kind") or "rom").lower()
                    if kind != "rom":
                        continue
                    expected = Path(_norm_zip_name(c.get("expected") or "")).name
                    if not expected or expected in plan:
                        continue
                    st = (c.get("status") or "").lower()
                    if st in ("nodump", "optional", "missing", "available"):
                        if st == "available" and c.get("source_zip") and c.get("source_member"):
                            key = f"ext:{c['source_zip']}|{c['source_member']}"
                            try:
                                with zipfile.ZipFile(str(c["source_zip"]), "r") as ez:
                                    ez.getinfo(c["source_member"])  # existe toujours ?
                                selected.add(key)
                                plan[expected] = key
                                notes.append(f"← {Path(c['source_zip']).name} → {expected}")
                            except Exception as e:
                                notes.append(f"Échec emprunt {expected}: {e}")
                        continue
                    found = _norm_zip_name(c.get("found") or "")
                    if "→" in found:
                        found = found.split("→")[-1].strip()
                    if not found:
                        continue
                    # résoudre nom réel dans le zip
                    real = (
                        by_base.get(Path(found).name.lower())
                        or (found if found in all_reals else None)
                    )
                    if real is None:
                        # essayer via CRC component
                        cs = (c.get("crc_found") or c.get("crc_expected") or "").strip().lower()
                        if cs:
                            try:
                                ci = int(cs, 16)
                            except ValueError:
                                ci = -1
                            if ci >= 0 and by_crc.get(ci):
                                real = by_crc[ci][0][0]
                    if real is None:
                        continue
                    plan[expected] = real
                    selected.add(real)

                # 2b) Forcer dans le plan les components "rename" encore absents
                for c in (row.get("components") or []):
                    if (c.get("kind") or "rom") != "rom":
                        continue
                    if (c.get("status") or "") != "rename":
                        continue
                    expected = Path(_norm_zip_name(c.get("expected") or "")).name
                    if not expected or expected in plan:
                        continue
                    found = _norm_zip_name(c.get("found") or "")
                    if "→" in found:
                        found = found.split("→")[-1].strip()
                    if not found:
                        continue
                    real = (
                        by_base.get(Path(found).name.lower())
                        or (found if found in all_reals else None)
                    )
                    if real is None:
                        continue
                    # Autoriser copie d'un membre déjà utilisé (même CRC, 2 noms DAT)
                    plan[expected] = real
                    selected.add(real)

                _repair_log(f"  plan={len(plan)}/{len(roms_req)} members_zip={len(all_reals)}")

            # Source FERMÉE
            needs_rewrite = False
            for expected, src_key in plan.items():
                if str(src_key).startswith("ext:"):
                    needs_rewrite = True
                    break
                if Path(src_key).name.lower() != expected.lower():
                    needs_rewrite = True
                    break
            used_reals = {v for v in plan.values() if not str(v).startswith("ext:")}
            # Membres non utilisés par le plan : conservés tels quels (hors DAT, devices,
            # autre révision…), sauf s'ils portent le nom d'une ROM du plan (ex. mauvais CRC)
            # → retirés du ZIP mais sauvegardés (copie du ZIP original dans la corbeille).
            plan_names_l = {k.lower() for k in plan}
            extras = [
                f for f in all_files
                if f not in used_reals and Path(f).name.lower() not in plan_names_l
            ]
            dropped = [f for f in all_files if f not in used_reals and f not in extras]
            # Toujours réécrire s'il reste des rename dans les components
            if any((c.get("status") or "") == "rename" for c in (row.get("components") or [])):
                needs_rewrite = True
            # Toujours réécrire si plan a plus d'entrées que de sources uniques (copies CRC)
            if len(plan) > len(used_reals):
                needs_rewrite = True

            _repair_log(
                f"  needs_rewrite={needs_rewrite} plan={len(plan)} "
                f"extras_kept={len(extras)} dropped={len(dropped)}"
            )

            if needs_rewrite and plan:
                tmp = src.with_name(src.name + ".rsv_repair")
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                written = 0
                renamed = 0
                try:
                    with zipfile.ZipFile(str(tmp), "w", compression=zipfile.ZIP_DEFLATED) as zout:
                        ext_zips: Dict[str, zipfile.ZipFile] = {}
                        try:
                            with zipfile.ZipFile(str(src), "r") as zin2:
                                for expected in sorted(plan.keys()):
                                    key = plan[expected]
                                    if key not in selected:
                                        continue
                                    if key.startswith("ext:"):
                                        ext_path, ext_member = key[4:].split("|", 1)
                                        if ext_path not in ext_zips:
                                            ext_zips[ext_path] = zipfile.ZipFile(ext_path, "r")
                                        zsrc, sname = ext_zips[ext_path], ext_member
                                    else:
                                        zsrc, sname = zin2, key
                                        if Path(key).name != expected:
                                            renamed += 1
                                            notes.append(f"{Path(key).name} → {expected}")
                                    _copy_zip_member(
                                        zsrc, zsrc.getinfo(sname), zout,
                                        new_name=expected, compress_type=zipfile.ZIP_DEFLATED,
                                    )
                                    written += 1
                                for name in extras:
                                    _copy_zip_member(zin2, zin2.getinfo(name), zout)
                        finally:
                            for ez in ext_zips.values():
                                ez.close()
                    if written == 0:
                        if tmp.exists():
                            tmp.unlink()
                        return False, "Aucune ROM écrite"
                    if dropped:
                        backup = move_to_trash(src, copy=True)
                        notes.append(
                            f"{len(dropped)} fichier(s) retiré(s) du ZIP, original sauvegardé → {backup}"
                        )
                        _repair_log(f"  backup {backup} dropped={dropped}")
                    try:
                        _safe_replace(tmp, src)
                    except RuntimeError as e:
                        msg = f"{e} — fermez MAME / l'explorateur puis réessayez"
                        _repair_log(f"  {msg}")
                        return False, msg
                    if renamed:
                        notes.insert(0, f"{renamed} ROM renommée(s)")
                    if extras:
                        notes.append(f"{len(extras)} fichier(s) hors DAT conservé(s)")
                    _repair_log(f"  wrote {written} roms renamed={renamed} extras={len(extras)}")
                except Exception as e:
                    try:
                        if tmp.exists():
                            tmp.unlink()
                    except OSError:
                        pass
                    _repair_log(f"  write fail: {e}")
                    return False, str(e)
            elif not plan and roms_req:
                _repair_log("  plan vide — aucune ROM du DAT trouvée dans le ZIP par CRC")
                # on tente quand même le rename du conteneur + vérif
            else:
                _repair_log("  pas de rewrite interne nécessaire")

        except zipfile.BadZipFile:
            return False, "ZIP corrompu"
        except Exception as e:
            traceback.print_exc()
            _repair_log(f"  exception: {e}")
            return False, str(e)

    # Rename conteneur → game.zip
    dst = src
    target_name = game + ".zip"
    if src.name != target_name:
        dst = src.parent / target_name
        try:
            same = (
                src.exists()
                and dst.exists()
                and os.path.normcase(str(src.resolve())) == os.path.normcase(str(dst.resolve()))
            )
        except OSError:
            same = False
        if dst.exists() and not same:
            try:
                if str(src.resolve()) != str(dst.resolve()):
                    msg = f"Existe déjà : {dst.name}"
                    _repair_log(msg)
                    return False, msg
            except OSError:
                return False, f"Existe déjà : {dst.name}"
        if not same and src.name != dst.name:
            try:
                if src.name.lower() == dst.name.lower():
                    mid = src.with_name(src.stem + ".__rsv__.zip")
                    if mid.exists():
                        mid.unlink()
                    src.rename(mid)
                    mid.rename(dst)
                else:
                    src.rename(dst)
                notes.append(f"ZIP → {dst.name}")
                _repair_log(f"  container {src.name} → {dst.name}")
            except OSError as e:
                msg = f"Rename ZIP échoué : {e}"
                _repair_log(msg)
                return False, msg

    # Vérification disque
    try:
        cat = _zip_catalog(dst)
        verified = _scan_one_mame_machine((machine, str(dst), cat))
    except Exception as e:
        return False, f"Écrit mais vérif impossible : {e}"

    for k, v in verified.items():
        row[k] = v
    row["path"] = str(dst)
    row["found"] = dst.name
    row["is_zip"] = True
    row["repairable"] = _row_is_repairable(row)

    st = (row.get("status") or "").lower()
    n_ren = sum(1 for c in (row.get("components") or []) if (c.get("status") or "") == "rename")
    _repair_log(f"  verify status={st} renames_left={n_ren} msg={row.get('message')!r}")

    if n_ren > 0 or st == "rename":
        msg = f"Vérif : encore {n_ren} ROM mal nommée(s). " + (" ; ".join(notes[:6]) if notes else "")
        _repair_log(f"  FAIL {msg}")
        return False, msg

    msg = " ; ".join(notes[:12]) if notes else (row.get("message") or "OK")
    _repair_log(f"  OK {msg}")
    return True, msg



def _row_is_zip_member(row: Dict) -> bool:
    """Ligne « membre d'un ZIP » (mode standard) : le fichier sur disque est partagé avec d'autres lignes."""
    return bool(row.get("is_zip") and (row.get("zip_member") or "").strip() and row.get("components") is None)


def _bulk_deletable(row: Dict) -> bool:
    """
    Lignes que « Supprimer les Bad » peut traiter.
    Exclus : erreurs de lecture (souvent passagères), ZIP hors DAT en arcade
    (BIOS absent d'un DAT filtré, set d'une autre version MAME…).
    """
    if (row.get("status") or "") != "bad" or not row.get("path"):
        return False
    if (row.get("mtype") or "") == "extra":
        return False
    if row.get("unidentified"):  # jamais lu : rien ne prouve qu'il est mauvais
        return False
    return True


def do_delete(row: Dict) -> Tuple[bool, str]:
    """
    Supprime le fichier d'une ligne en le DÉPLAÇANT dans <racine ROMs>/.rsv_trash/<session>/.
    Ligne membre d'un ZIP (mode standard) : seul ce membre est retiré du ZIP
    (et extrait dans la corbeille) ; les autres ROM du ZIP sont conservées.
    """
    if not row.get("path"):
        return False, "Pas de fichier à supprimer"
    src = Path(row["path"])
    if not src.exists():
        return False, "Déjà absent"
    try:
        if _row_is_zip_member(row) and src.suffix.lower() == ".7z":
            # .7z en lecture seule : on ne retire pas un fichier d'une archive qui en contient d'autres
            if py7zr is None:
                return False, _7Z_MISSING
            if len(_list_7z(src)) > 1:
                return False, (
                    f"Impossible de retirer {row['zip_member']} de {src.name} "
                    "(archive .7z en lecture seule, elle contient d'autres fichiers)"
                )
        elif _row_is_zip_member(row):
            member = row["zip_member"]
            with zipfile.ZipFile(str(src), "r") as zf:
                n_files = sum(1 for i in zf.infolist() if not i.is_dir())
            if n_files > 1:
                removed, _kept = _remove_zip_members(src, [member])
                if not removed:
                    return False, f"Membre introuvable dans {src.name} : {member}"
                row["status"] = "deleted"
                row["found"] = ""
                row["path"] = ""
                return True, f"{member} retiré de {src.name} (copie dans {TRASH_DIR_NAME})"
        dest = move_to_trash(src)
        row["status"] = "deleted"
        row["found"] = ""
        row["path"] = ""
        return True, f"Déplacé dans {TRASH_DIR_NAME} ({dest.parent.name})"
    except Exception as e:
        return False, str(e)


def fetch_adb_mame(game_names: List[str], lang: str = "en") -> Dict[str, Dict[str, Any]]:
    """
    Interroge l'API Arcade Database (adb.arcadeitalia.net) pour des romsets MAME.
    Retourne {game_name: {title, manufacturer, year, genre, cloneof, status, url, ...}}
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not game_names:
        return out
    # L'API accepte plusieurs noms séparés par ;
    # lots de ~40 pour rester raisonnable
    batch_size = 40
    for i in range(0, len(game_names), batch_size):
        batch = game_names[i : i + batch_size]
        names = ";".join(batch)
        url = (
            "https://adb.arcadeitalia.net/service_scraper.php"
            f"?ajax=query_mame&lang={lang}&game_name={urllib.parse.quote(names)}"
        )
        try:
            raw = _http_get(url, timeout=60)
            data = json.loads(raw.decode("utf-8", errors="replace"))
            for item in data.get("result") or []:
                gname = (item.get("game_name") or "").strip()
                if not gname:
                    continue
                out[gname.lower()] = {
                    "title": item.get("title") or gname,
                    "manufacturer": item.get("manufacturer") or "",
                    "year": str(item.get("year") or ""),
                    "genre": item.get("genre") or "",
                    "cloneof": item.get("cloneof") or "",
                    "status": (item.get("status") or "").lower(),
                    "players": item.get("players"),
                    "url": item.get("url") or "",
                    "history": (item.get("history") or "")[:2000],
                    "icon": item.get("url_icon") or "",
                }
        except Exception:
            traceback.print_exc()
            continue
    return out



# ---------------------------------------------------------------------------
# Profils multi-systèmes (RetroBat / Batocera / Recalbox)
# ---------------------------------------------------------------------------

# system id (es_systems) → motifs de recherche DAT (sous-chaînes, ordre de préférence)
SYSTEM_DAT_PRESETS: Dict[str, Dict[str, Any]] = {
    # Nintendo
    "nes": {"pack": "nointro", "patterns": ["nintendo entertainment system", "nes"]},
    "fds": {"pack": "nointro", "patterns": ["famicom disk system", "fds"]},
    "snes": {"pack": "nointro", "patterns": ["super nintendo", "snes"]},
    "n64": {"pack": "nointro", "patterns": ["nintendo 64", "n64"]},
    "gb": {"pack": "nointro", "patterns": ["game boy", "nintendo - game boy"]},
    "gbc": {"pack": "nointro", "patterns": ["game boy color"]},
    "gba": {"pack": "nointro", "patterns": ["game boy advance"]},
    "nds": {"pack": "nointro", "patterns": ["nintendo ds"]},
    "3ds": {"pack": "nointro", "patterns": ["nintendo 3ds"]},
    "gc": {"pack": "mameredump", "patterns": ["gamecube", "game cube"]},
    "wii": {"pack": "mameredump", "patterns": ["wii"]},
    "wiiu": {"pack": "nointro", "patterns": ["wii u"]},
    "virtualboy": {"pack": "nointro", "patterns": ["virtual boy"]},
    "pokemini": {"pack": "nointro", "patterns": ["pokemon mini", "pokémon mini"]},
    "satellaview": {"pack": "nointro", "patterns": ["satellaview"]},
    "sufami": {"pack": "nointro", "patterns": ["sufami"]},
    # Sega
    "mastersystem": {"pack": "nointro", "patterns": ["master system"]},
    "megadrive": {"pack": "nointro", "patterns": ["mega drive", "genesis"]},
    "genesis": {"pack": "nointro", "patterns": ["mega drive", "genesis"]},
    "sega32x": {"pack": "nointro", "patterns": ["32x"]},
    "segacd": {"pack": "redump", "patterns": ["mega-cd", "sega cd"]},
    "gamegear": {"pack": "nointro", "patterns": ["game gear"]},
    "saturn": {"pack": "redump", "patterns": ["saturn"]},
    "dreamcast": {"pack": "redump", "patterns": ["dreamcast"]},
    "sg1000": {"pack": "nointro", "patterns": ["sg-1000", "sg1000"]},
    # Sony
    "psx": {"pack": "redump", "patterns": ["playstation", "psx"]},
    "ps2": {"pack": "redump", "patterns": ["playstation 2", "ps2"]},
    "psp": {"pack": "redump", "patterns": ["playstation portable", "psp"]},
    "ps3": {"pack": "redump", "patterns": ["playstation 3"]},
    # Atari
    "atari2600": {"pack": "nointro", "patterns": ["atari 2600"]},
    "atari5200": {"pack": "nointro", "patterns": ["atari 5200"]},
    "atari7800": {"pack": "nointro", "patterns": ["atari 7800"]},
    "atarilynx": {"pack": "nointro", "patterns": ["lynx"]},
    "atarist": {"pack": "nointro", "patterns": ["atari st"]},
    "jaguar": {"pack": "nointro", "patterns": ["jaguar"]},
    # Nec
    "pcengine": {"pack": "nointro", "patterns": ["pc engine", "turbografx"]},
    "pcenginecd": {"pack": "redump", "patterns": ["pc engine cd", "turbografx cd"]},
    "supergrafx": {"pack": "nointro", "patterns": ["supergrafx"]},
    "pcfx": {"pack": "redump", "patterns": ["pc-fx", "pcfx"]},
    # SNK
    "ngp": {"pack": "nointro", "patterns": ["neo-geo pocket"]},
    "ngpc": {"pack": "nointro", "patterns": ["neo-geo pocket color"]},
    # Arcade FBNeo (zips non-merged, même moteur que MAME)
    "neogeo": {"pack": "fbneo", "patterns": ["neogeo", "neo-geo", "fbneo"], "arcade": True},
    "cps1": {"pack": "fbneo", "patterns": ["cps1", "cps-1", "capcom play system"], "arcade": True},
    "cps2": {"pack": "fbneo", "patterns": ["cps2", "cps-2"], "arcade": True},
    "cps3": {"pack": "fbneo", "patterns": ["cps3", "cps-3"], "arcade": True},
    "cave": {"pack": "fbneo", "patterns": ["cave"], "arcade": True},
    "fbneo": {"pack": "fbneo", "patterns": ["fbneo", "finalburn", "final burn"], "arcade": True},
    # Arcade MAME
    "mame": {"pack": "mame", "patterns": ["mame", "arcade"], "arcade": True},
    "mame60": {"pack": "mame", "patterns": ["mame"], "arcade": True},
    "naomi": {"pack": "mame", "patterns": ["naomi"], "arcade": True},
    "atomiswave": {"pack": "mame", "patterns": ["atomiswave"], "arcade": True},
    "model2": {"pack": "mame", "patterns": ["model 2"], "arcade": True},
    "model3": {"pack": "mame", "patterns": ["model 3"], "arcade": True},
    # Microsoft
    "xbox": {"pack": "redump", "patterns": ["xbox"]},
    "msx": {"pack": "nointro", "patterns": ["msx"]},
    "msx1": {"pack": "nointro", "patterns": ["msx"]},
    "msx2": {"pack": "nointro", "patterns": ["msx2", "msx 2"]},
    # Commodore
    "c64": {"pack": "nointro", "patterns": ["commodore 64", "c64"]},
    "amiga": {"pack": "nointro", "patterns": ["amiga"]},
    "amigacd32": {"pack": "redump", "patterns": ["amiga cd32", "cd32"]},
    # Divers
    "3do": {"pack": "redump", "patterns": ["3do"]},
    "jaguarcd": {"pack": "redump", "patterns": ["jaguar cd"]},
    "pc88": {"pack": "nointro", "patterns": ["pc-8800", "pc88"]},
    "pc98": {"pack": "nointro", "patterns": ["pc-98", "pc98"]},
    "x68000": {"pack": "nointro", "patterns": ["x68000"]},
    "zxspectrum": {"pack": "nointro", "patterns": ["zx spectrum"]},
    "zx81": {"pack": "nointro", "patterns": ["zx81", "zx 81"]},
    "colecovision": {"pack": "nointro", "patterns": ["coleco"]},
    "intellivision": {"pack": "nointro", "patterns": ["intellivision"]},
    "vectrex": {"pack": "nointro", "patterns": ["vectrex"]},
    "odyssey2": {"pack": "nointro", "patterns": ["odyssey2", "odyssey 2"]},
    "channelf": {"pack": "nointro", "patterns": ["channel f"]},
    "supervision": {"pack": "nointro", "patterns": ["supervision"]},
    "wswan": {"pack": "nointro", "patterns": ["wonderswan"]},
    "wswanc": {"pack": "nointro", "patterns": ["wonderswan color"]},
    "wonderwitch": {"pack": "nointro", "patterns": ["wonderwitch"]},
    "scummvm": {"pack": "", "patterns": []},
    "dos": {"pack": "", "patterns": []},
    "windows": {"pack": "", "patterns": []},
}


def _profile_path(profile_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", (profile_id or "").strip())[:80]
    return PROFILES_DIR / f"{safe}.json"


def _list_profile_files() -> List[Path]:
    ensure_folders()
    return sorted(PROFILES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


def load_profile(profile_id: str) -> Dict[str, Any]:
    path = _profile_path(profile_id)
    if not path.is_file():
        # essai nom exact
        alt = PROFILES_DIR / f"{profile_id}.json"
        if alt.is_file():
            path = alt
        else:
            raise FileNotFoundError(f"Profil introuvable : {profile_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_file"] = str(path)
    data["id"] = data.get("id") or path.stem
    return data


def save_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    ensure_folders()
    pid = profile.get("id") or re.sub(r"[^a-zA-Z0-9_\-]", "_", profile.get("name") or "profil")[:60]
    profile["id"] = pid
    profile["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if not profile.get("created_at"):
        profile["created_at"] = profile["updated_at"]
    path = _profile_path(pid)
    out = {k: v for k, v in profile.items() if not k.startswith("_")}
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out["_file"] = str(path)
    return out


def delete_profile(profile_id: str) -> None:
    path = _profile_path(profile_id)
    if path.is_file():
        path.unlink()
    else:
        raise FileNotFoundError(profile_id)


def list_profiles() -> List[Dict[str, Any]]:
    items = []
    for p in _list_profile_files():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            systems = data.get("systems") or []
            scanned = [s for s in systems if s.get("last_counts")]
            total_games = sum(int((s.get("last_counts") or {}).get("total") or 0) for s in scanned)
            total_good = sum(int((s.get("last_counts") or {}).get("good") or 0) for s in scanned)
            items.append({
                "id": data.get("id") or p.stem,
                "name": data.get("name") or p.stem,
                "type": data.get("type") or "retrobat",
                "root": data.get("root") or "",
                "systems_count": len(systems),
                "scanned_count": len(scanned),
                "total_games": total_games,
                "total_good": total_good,
                "updated_at": data.get("updated_at") or "",
            })
        except Exception:
            continue
    return items


def find_es_systems_cfg(root: Path) -> Optional[Path]:
    """Cherche es_systems.cfg sous une racine RetroBat / ES / Batocera."""
    candidates = [
        root / "emulationstation" / "es_systems.cfg",
        root / "system" / "templates" / "emulationstation" / "es_systems.cfg",  # batocera-ish
        root / "configs" / "emulationstation" / "es_systems.cfg",
        root / "es_systems.cfg",
        root / "emulationstation" / ".emulationstation" / "es_systems.cfg",
    ]
    for c in candidates:
        if c.is_file():
            return c
    # recherche limitée
    try:
        for p in root.rglob("es_systems.cfg"):
            return p
    except OSError:
        pass
    return None


def parse_es_systems_cfg(cfg_path: Path, root: Path) -> List[Dict[str, Any]]:
    """Parse es_systems.cfg → liste de systèmes {id, fullname, path, extensions}."""
    systems: List[Dict[str, Any]] = []
    try:
        raw = cfg_path.read_bytes()
        parser = etree.XMLParser(recover=True, huge_tree=True)
        tree = etree.parse(io.BytesIO(raw), parser)
        xml = tree.getroot()
    except Exception as e:
        raise RuntimeError(f"es_systems.cfg illisible : {e}")

    for node in xml.xpath(".//system"):
        def _txt(tag: str) -> str:
            el = node.find(tag)
            return (el.text or "").strip() if el is not None else ""

        sid = _txt("name") or _txt("system")
        if not sid:
            continue
        fullname = _txt("fullname") or sid
        rel = _txt("path") or ""
        ext = _txt("extension") or ""
        # Résoudre le chemin ROMs
        rom_path = ""
        if rel:
            rel_clean = rel.replace("\\", "/")
            if rel_clean.startswith("./"):
                rel_clean = rel_clean[2:]
            cand = Path(rel)
            if not cand.is_absolute():
                cand = (root / rel_clean).resolve() if root else Path(rel_clean)
            else:
                cand = Path(rel)
            rom_path = str(cand)
        systems.append({
            "id": sid.lower(),
            "fullname": fullname,
            "path": rom_path,
            "extensions": ext,
            "enabled": True,
            "dat": "",
            "dat_status": "none",
            "last_counts": None,
            "last_scan": None,
        })
    return systems


def discover_rom_folders(root: Path) -> List[Dict[str, Any]]:
    """Fallback : dossiers sous root/roms/."""
    systems = []
    roms = root / "roms"
    if not roms.is_dir():
        roms = root
    try:
        with os.scandir(str(roms)) as it:
            for entry in it:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.name.startswith("."):
                    continue
                sid = entry.name.lower()
                systems.append({
                    "id": sid,
                    "fullname": entry.name,
                    "path": entry.path,
                    "extensions": "",
                    "enabled": True,
                    "dat": "",
                    "dat_status": "none",
                    "last_counts": None,
                    "last_scan": None,
                })
    except OSError:
        pass
    systems.sort(key=lambda s: s["id"])
    return systems


def _iter_dat_files() -> List[Path]:
    ensure_folders()
    out: List[Path] = []
    try:
        for p in DEFAULT_DAT_DIR.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".dat", ".xml"):
                out.append(p)
    except OSError:
        pass
    return out


def suggest_dat_for_system(system_id: str) -> Dict[str, Any]:
    """
    Propose le meilleur DAT local pour un system id.
    Retourne {path, name, mtime, score, pack}.
    """
    sid = (system_id or "").lower().strip()
    preset = SYSTEM_DAT_PRESETS.get(sid) or {"pack": "", "patterns": [sid]}
    patterns = [p.lower() for p in (preset.get("patterns") or [sid]) if p]
    pack = (preset.get("pack") or "").lower()

    best = None
    best_score = -1
    for p in _iter_dat_files():
        name_l = p.name.lower()
        try:
            rel = p.relative_to(DEFAULT_DAT_DIR)
            rel_l = str(rel).lower()
            # anciennes versions téléchargées volontairement (dat/fbneo/versions/…) :
            # sélectionnables à la main, jamais proposées automatiquement
            if rel.parts[:2] == ("fbneo", FBNEO_VERSIONS_DIR):
                continue
        except ValueError:
            rel_l = name_l
        score = 0
        for i, pat in enumerate(patterns):
            if pat and pat in name_l:
                score += 100 - i * 5
            elif pat and pat in rel_l:
                score += 60 - i * 5
        if pack and pack in rel_l.replace("\\", "/"):
            score += 25
        if score <= 0:
            continue
        try:
            mtime = int(p.stat().st_mtime)
        except OSError:
            mtime = 0
        # bonus récence
        score += min(20, mtime // 10_000_000)
        if score > best_score:
            best_score = score
            best = {
                "path": str(p),
                "name": p.name,
                "mtime": mtime,
                "score": score,
                "pack": pack or "",
            }
    return best or {}


def detect_systems_for_root(root_str: str, profile_type: str = "retrobat") -> Dict[str, Any]:
    root = Path(root_str).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Racine introuvable : {root}")

    cfg = find_es_systems_cfg(root)
    source = "es_systems"
    if cfg:
        systems = parse_es_systems_cfg(cfg, root)
    else:
        systems = discover_rom_folders(root)
        source = "roms_folders"

    # Suggestions DAT
    matched = 0
    for s in systems:
        sug = suggest_dat_for_system(s["id"])
        if sug.get("path"):
            s["dat"] = sug["path"]
            s["dat_name"] = sug.get("name") or ""
            s["dat_status"] = "ok"
            matched += 1
        else:
            s["dat"] = ""
            s["dat_name"] = ""
            preset = SYSTEM_DAT_PRESETS.get(s["id"])
            if preset and not (preset.get("patterns")):
                s["dat_status"] = "skip"  # scummvm, dos…
                s["enabled"] = False
            else:
                s["dat_status"] = "missing"

    return {
        "root": str(root),
        "es_systems": str(cfg) if cfg else "",
        "source": source,
        "systems": systems,
        "matched_dats": matched,
        "total": len(systems),
    }


def profile_summary(profile: Dict[str, Any]) -> Dict[str, Any]:
    systems = profile.get("systems") or []
    enabled = [s for s in systems if s.get("enabled", True)]
    scanned = [s for s in enabled if s.get("last_counts")]
    counts = {"good": 0, "rename": 0, "bad": 0, "missing": 0, "error": 0, "incomplete": 0, "total": 0}
    for s in scanned:
        lc = s.get("last_counts") or {}
        for k in counts:
            counts[k] += int(lc.get(k) or 0)
    pct = int(100 * counts["good"] / counts["total"]) if counts["total"] else 0
    return {
        "systems_total": len(systems),
        "systems_enabled": len(enabled),
        "systems_scanned": len(scanned),
        "counts": counts,
        "pct_good": pct,
    }



# Un seul verrou pour tout ce qui lit/modifie STATE ou réécrit des fichiers :
# scan, chargement DAT, réparation, renommage, suppression, recompression.
# (Flask tourne en threaded=True.)
STATE_LOCK = threading.Lock()
_BUSY_MSG = "Une opération est déjà en cours (scan, réparation…) : réessayez dans un instant."


def _exclusive(wait: float = 0.0):
    """Exécute la route sous STATE_LOCK. wait=0 : refus immédiat si occupé ; sinon attente bornée."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            got = STATE_LOCK.acquire(timeout=wait) if wait > 0 else STATE_LOCK.acquire(blocking=False)
            if not got:
                return jsonify({"error": _BUSY_MSG, "busy": True})
            try:
                return fn(*args, **kwargs)
            finally:
                STATE_LOCK.release()
        return wrapper
    return deco


def _new_scan_id() -> str:
    """Change à chaque scan / DAT chargé : invalide les index envoyés par une fenêtre périmée."""
    STATE["scan_id"] = f"{time.time_ns():x}"
    return STATE["scan_id"]


_STALE_MSG = "Résultats périmés (un autre scan ou DAT a été chargé depuis) : relancez le scan."


def _stale(body: Dict) -> bool:
    """True si la requête ne vise pas les résultats actuellement en mémoire."""
    return (body.get("scan_id") or "") != (STATE.get("scan_id") or "")


def _norm_path_s(p: str) -> str:
    return os.path.normcase((p or "").replace("\\", "/")).rstrip("/")


def _row_mismatch(row: Dict, expected_path: Optional[str]) -> bool:
    """True si le client attendait un autre fichier pour cette ligne (None = pas de contrôle)."""
    if expected_path is None:
        return False
    return _norm_path_s(row.get("path") or "") != _norm_path_s(expected_path)

def _repair_log(msg: str) -> None:
    """Journal append-only pour diagnostiquer les repairs (repair.log)."""
    try:
        p = SCRIPT_DIR / "repair.log"
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


STATE: Dict[str, Any] = {
    "header": {}, "rom_map": {}, "games_list": [], "size_set": set(), "hash_mode": "crc", "dat_mode": "standard", "results": [],
    "dat_path": "", "roms_path": str(DEFAULT_ROMS_DIR), "scan_id": "",
}

app = Flask(__name__)

_LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(netloc: str) -> str:
    """Nom d'hôte sans le port ; gère [::1]:8080."""
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):
        return netloc[1:netloc.find("]")] if "]" in netloc else netloc
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


@app.before_request
def _guard_local_only():
    """
    Le serveur écoute sur 127.0.0.1, mais un site web visité peut quand même l'atteindre :
    - DNS rebinding → l'en-tête Host porte le domaine de l'attaquant : refusé ;
    - requête intersite (formulaire, fetch) → Origin / Sec-Fetch-Site étrangers : refusé
      pour toute requête qui modifie quelque chose.
    """
    if _hostname(request.host) not in _LOCAL_HOSTNAMES:
        return jsonify({"error": "Hôte non autorisé"}), 403
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin")
    if origin is not None:
        o_host = (urllib.parse.urlsplit(origin).hostname or "").lower() if origin != "null" else ""
        if o_host not in _LOCAL_HOSTNAMES:
            return jsonify({"error": "Origine non autorisée"}), 403
    if (request.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
        return jsonify({"error": "Requête intersite refusée"}), 403
    return None

@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(405)
@app.errorhandler(413)
@app.errorhandler(415)
@app.errorhandler(500)
def _json_error(e):
    """Les routes /api/ répondent toujours en JSON (l'UI attend {"error": …}, pas une page HTML)."""
    from werkzeug.exceptions import HTTPException
    code = e.code if isinstance(e, HTTPException) else 500
    if not request.path.startswith("/api/"):
        return e if isinstance(e, HTTPException) else ("Erreur interne", 500)
    orig = getattr(e, "original_exception", None)  # 500 : l'exception réelle (déjà journalisée par Flask)
    desc = (f"{orig.__class__.__name__}: {orig}" if orig else None) or getattr(e, "description", None) or str(e)
    return jsonify({"error": f"{code} : {desc}"}), code


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


# HTML is loaded from a separate file for clarity
HTML_PATH = _asset("_ui.html")


@app.route("/about-author.jpg")
def about_author_img():
    p = _asset("about-author.jpg")
    if not p.is_file():
        return ("", 404)
    return send_file(p, mimetype="image/jpeg")

@app.route("/icon.png")
def icon_png():
    p = _asset("icon.png")
    if not p.is_file():
        p = _asset("icon-512.png")
    if not p.is_file():
        return ("", 404)
    return send_file(p, mimetype="image/png")


@app.route("/favicon.ico")
def favicon():
    p = _asset("favicon.ico")
    if p.is_file():
        return send_file(p, mimetype="image/x-icon")
    for name in ("icon-32.png", "icon-256.png", "icon.png"):
        p = _asset(name)
        if p.is_file():
            return send_file(p, mimetype="image/png")
    return ("", 404)


@app.route("/icon-32.png")
def icon_32():
    p = _asset("icon-32.png")
    if not p.is_file():
        p = _asset("icon.png")
    if not p.is_file():
        return ("", 404)
    return send_file(p, mimetype="image/png")


@app.route("/icon-256.png")
def icon_256():
    p = _asset("icon-256.png")
    if not p.is_file():
        p = _asset("icon.png")
    if not p.is_file():
        return ("", 404)
    return send_file(p, mimetype="image/png")


@app.route("/icon-512.png")
def icon_512():
    p = _asset("icon-512.png")
    if not p.is_file():
        p = _asset("icon.png")
    if not p.is_file():
        return ("", 404)
    return send_file(p, mimetype="image/png")


@app.route("/")
def index():
    ensure_folders()
    dat_default = str(DEFAULT_DAT_DIR)
    for pattern in ("*.dat", "*.DAT", "*.xml", "*.XML"):
        found = sorted(DEFAULT_DAT_DIR.glob(pattern))
        if found:
            dat_default = str(found[0])
            break
    html = HTML_PATH.read_text(encoding="utf-8")
    return render_template_string(html, dat_default=dat_default, roms_default=str(DEFAULT_ROMS_DIR))


@app.route("/api/i18n/<lang>")
def api_i18n(lang: str):
    """Serve language JSON (en, fr, …)."""
    lang = (lang or "fr").lower().split("-")[0]
    base = _asset("i18n")
    path = base / f"{lang}.json"
    if not path.is_file():
        path = base / "en.json"
    if not path.is_file():
        return jsonify({"error": "No language files"}), 404
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return jsonify({"ok": True, "lang": lang, "strings": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/i18n")
def api_i18n_list():
    base = _asset("i18n")
    langs = []
    if base.is_dir():
        for p in sorted(base.glob("*.json")):
            langs.append(p.stem)
    return jsonify({"ok": True, "languages": langs or ["fr", "en"]})

@app.route("/api/load_dat", methods=["POST"])
@_exclusive()
def api_load_dat():
    path = Path(((request.get_json(silent=True) or {}).get("path") or "").strip())
    if not path.is_file():
        return jsonify({"error": f"Fichier introuvable : {path}"})
    try:
        header, rom_map, games_list, size_set, hash_mode = parse_dat(path)
        STATE["header"] = header
        STATE["rom_map"] = rom_map
        STATE["games_list"] = games_list
        STATE["size_set"] = size_set
        STATE["hash_mode"] = hash_mode
        STATE["dat_mode"] = header.get("dat_mode", "standard")
        STATE["dat_path"] = str(path)
        STATE["results"] = []
        return jsonify({
            "ok": True,
            "scan_id": _new_scan_id(),
            "name": header.get("name", path.name),
            "description": header.get("description", ""),
            "count": len(games_list),
            "hash_mode": hash_mode,
            "dat_mode": header.get("dat_mode", "standard"),
            "merge_mode": header.get("merge_mode", ""),
            "path": str(path.resolve()),
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/result_detail", methods=["POST"])
def api_result_detail():
    """Détail d'une entrée de scan (composantes ROM/CHD)."""
    body = request.get_json(silent=True) or {}
    idx = body.get("index", -1)
    results = STATE.get("results") or []
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return jsonify({"error": "Index invalide"})
    if idx < 0 or idx >= len(results):
        return jsonify({"error": "Index hors limites"})
    row = results[idx]
    # Si components absents (ancien scan standard), en fabriquer un minimal
    comps = row.get("components")
    if comps is None:
        comps = []
        if row.get("status") == "missing":
            comps.append({
                "kind": "rom", "expected": row.get("expected") or "", "found": "",
                "status": "missing", "crc_expected": "", "crc_found": "",
                "sha1_expected": "", "sha1_found": "", "size_expected": 0, "size_found": 0,
                "message": row.get("message") or "absent",
            })
        elif row.get("path"):
            comps.append({
                "kind": "rom",
                "expected": row.get("expected") or "",
                "found": row.get("found") or row.get("zip_member") or "",
                "status": "ok" if row.get("status") in ("good", "rename") else row.get("status") or "bad",
                "crc_expected": "",
                "crc_found": row.get("crc") or "",
                "sha1_expected": "",
                "sha1_found": row.get("sha1") or "",
                "size_expected": 0,
                "size_found": row.get("size") or 0,
                "message": row.get("message") or "",
            })
    public = {k: v for k, v in row.items() if k != "components"}
    public["components"] = comps
    return jsonify({"ok": True, "row": public})

@app.route("/api/scan", methods=["POST"])
@_exclusive()
def api_scan():
    if not STATE.get("games_list"):
        return jsonify({"error": "Chargez d'abord un DAT"})
    body = request.get_json(silent=True) or {}
    raw = str(body.get("path") or "").strip()
    if not raw:  # Path("") vaut « . » : sans ce contrôle, on scannerait le dossier courant
        return jsonify({"error": "Indiquez un dossier de ROMs"})
    path = Path(raw)
    if not path.is_dir():
        return jsonify({"error": f"Dossier introuvable : {path}"})
    return _api_scan_body(path, body)


def _api_scan_body(path: Path, body: dict):
    # Nouveau scan : vider l'ancien résultat côté serveur immédiatement
    STATE["results"] = []
    _new_scan_id()
    # Option sous-dossiers : 0 (défaut), 1 ou 2
    try:
        max_depth = int(body.get("max_depth") or 0)
    except (TypeError, ValueError):
        max_depth = 0
    max_depth = max(0, min(max_depth, 2))
    try:
        hash_mode = STATE.get("hash_mode") or "crc"
        dat_mode = STATE.get("dat_mode") or "standard"
        # CHD auto-depth appliqué dans scan_roms si max_depth==0
        HASH_CACHE.hits = 0
        results = scan_roms(
            path,
            STATE["rom_map"],
            STATE["games_list"],
            STATE.get("size_set") or set(),
            hash_mode,
            dat_mode,
            max_depth=max_depth,
            deep=bool(body.get("deep")),
            quick_id=body.get("quick_id", True) is not False,
        )
        # Profondeur effective (peut avoir été relevée pour CHD)
        has_disks = any(g.get("is_disk") for g in (STATE.get("games_list") or []))
        effective_depth = max_depth
        if max_depth == 0 and (hash_mode == "sha1" or has_disks):
            effective_depth = 2
        STATE["results"] = results
        STATE["roms_path"] = str(path)
        STATE["scan_max_depth"] = effective_depth
        public = _public_results(results)
        return jsonify({
            "ok": True,
            "scan_id": _new_scan_id(),
            "results": public,
            "counts": _counts(results),
            "max_depth": effective_depth,
            "max_depth_auto": effective_depth != max_depth,
            "hash_cache_hits": HASH_CACHE.hits,
        })
    except ScanCancelled as e:
        return jsonify({"error": str(e), "cancelled": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/scan/progress")
def api_scan_progress():
    """Avancement du scan en cours (lu pendant que /api/scan travaille : pas de verrou)."""
    return jsonify(SCAN_PROGRESS.snapshot())


@app.route("/api/scan/cancel", methods=["POST"])
def api_scan_cancel():
    return jsonify({"ok": True, "running": SCAN_PROGRESS.request_cancel()})

@app.route("/api/hash_cache/clear", methods=["POST"])
@_exclusive(wait=5)
def api_hash_cache_clear():
    HASH_CACHE.load()
    n = len(HASH_CACHE.data)
    HASH_CACHE.clear()
    return jsonify({"ok": True, "cleared": n})


@app.route("/api/rename", methods=["POST"])
@_exclusive(wait=5)
def api_rename():
    body = request.get_json(silent=True) or {}
    if _stale(body):
        return jsonify({"error": _STALE_MSG, "stale": True})
    idx = body.get("index", -1)
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return jsonify({"error": "Index invalide"})
    results = STATE.get("results") or []
    if idx < 0 or idx >= len(results):
        return jsonify({"error": "Index invalide"})
    row = results[idx]
    if _row_mismatch(row, body.get("path")):
        return jsonify({"error": _STALE_MSG, "stale": True})
    ok, msg = do_rename(row)
    if not ok:
        return jsonify({"error": msg})
    row["repairable"] = _row_is_repairable(row)
    return jsonify({
        "ok": True,
        "message": msg,
        "row": _public_row(row),
        "counts": _counts(results),
    })


@app.route("/api/repair", methods=["POST"])
@_exclusive(wait=5)
def api_repair():
    """Répare une ou plusieurs entrées (indices ou game/path). Reconstruction ZIP non-merged."""
    body = request.get_json(silent=True) or {}
    if _stale(body):
        return jsonify({"error": _STALE_MSG, "stale": True, "repaired": 0, "failed": []})
    results = STATE.get("results") or []
    indices = body.get("indices")
    if indices is None and body.get("index") is not None:
        indices = [body.get("index")]
    # Résolution alternative : par game ou path (évite les décalages d'index)
    if not indices:
        indices = []
        game_q = (body.get("game") or "").strip().lower()
        path_q = (body.get("path") or "").strip().lower()
        if game_q or path_q:
            for i, r in enumerate(results):
                if game_q and (r.get("game") or "").lower() == game_q:
                    indices.append(i)
                elif path_q and (r.get("path") or "").replace("\\", "/").lower() == path_q.replace("\\", "/"):
                    indices.append(i)
    if not indices:
        return jsonify({"error": "Aucun index / jeu à réparer", "repaired": 0, "failed": []})

    done = 0
    failed = []
    rows_out = []
    for idx in indices:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            failed.append({"index": idx, "error": "Index invalide"})
            continue
        if idx < 0 or idx >= len(results):
            failed.append({"index": idx, "error": "Index hors limites"})
            continue
        row = results[idx]
        # Une seule ligne visée avec son chemin : vérifier qu'il s'agit bien d'elle
        if len(indices) == 1 and "path" in body and _row_mismatch(row, body.get("path")):
            failed.append({"index": idx, "error": _STALE_MSG})
            continue
        try:
            ok, msg = do_repair(row)
        except Exception as e:
            traceback.print_exc()
            ok, msg = False, str(e)
        if ok:
            done += 1
            row["repairable"] = _row_is_repairable(row)
            rows_out.append({"index": idx, "row": _public_row(row, include_components=True), "message": msg})
        else:
            failed.append({
                "index": idx,
                "error": msg,
                "game": row.get("game") or row.get("found"),
                "row": _public_row(row, include_components=True),
            })
    return jsonify({
        "ok": True,
        "repaired": done,
        "failed": failed,
        "rows": rows_out,
        "counts": _counts(results),
    })



def recompress_zip_file(zip_path: Path, compresslevel: int = 9) -> Dict[str, Any]:
    """
    Recompresse un ZIP en DEFLATE niveau max (compatible zip standard).
    Équivalent du script rezip.bat (7-Zip -mx=9) sans dépendance externe.
    Remplacement atomique via fichier temporaire.
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        return {"ok": False, "error": "Introuvable", "path": str(zip_path)}
    if zip_path.suffix.lower() != ".zip":
        return {"ok": False, "error": "Pas un .zip", "path": str(zip_path)}

    size_before = zip_path.stat().st_size
    tmp = zip_path.with_name(zip_path.name + ".rsv_rezip")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass

    members = 0
    try:
        level = max(0, min(9, int(compresslevel)))
        # Copie en flux (pas tout le ZIP en RAM) ; la source est fermée avant le remplacement
        with zipfile.ZipFile(str(zip_path), "r") as zin, zipfile.ZipFile(
            str(tmp), "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level
        ) as zout:
            for info in zin.infolist():
                if info.is_dir() or info.filename.endswith("/"):
                    continue
                # garder le chemin interne (sous-dossiers éventuels)
                name = info.filename.replace("\\", "/")
                _copy_zip_member(
                    zin, info, zout, new_name=name,
                    compress_type=zipfile.ZIP_DEFLATED, compresslevel=level,
                )
                members += 1

        try:
            _safe_replace(tmp, zip_path)
        except RuntimeError as e:
            return {
                "ok": False,
                "error": str(e),
                "path": str(zip_path),
                "name": zip_path.name,
            }

        size_after = zip_path.stat().st_size
        saved = size_before - size_after
        return {
            "ok": True,
            "path": str(zip_path),
            "name": zip_path.name,
            "members": members,
            "size_before": size_before,
            "size_after": size_after,
            "saved": saved,
            "saved_pct": round(100.0 * saved / size_before, 1) if size_before else 0.0,
        }
    except zipfile.BadZipFile:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "error": "ZIP corrompu", "path": str(zip_path), "name": zip_path.name}
    except Exception as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "error": str(e), "path": str(zip_path), "name": zip_path.name}


def list_zip_files(folder: Path, max_depth: int = 0) -> List[Path]:
    """
    Liste tous les .zip d'un dossier — logique type rezip.bat :
    for %%F in (*.zip)
    """
    folder = Path(folder)
    if not folder.exists():
        raise FileNotFoundError(f"Dossier introuvable : {folder}")
    if not folder.is_dir():
        raise NotADirectoryError(f"Pas un dossier : {folder}")

    max_depth = max(0, min(int(max_depth or 0), 2))
    zips: List[Path] = []

    def _collect_dir(d: Path) -> None:
        # os.listdir : simple et fiable (NAS / Windows / Linux)
        try:
            names = os.listdir(str(d))
        except OSError as e:
            raise OSError(f"Impossible de lire {d} : {e}") from e
        for name in names:
            if not name.lower().endswith(".zip"):
                continue
            p = d / name
            try:
                # is_file suit les liens ; on accepte les fichiers réels
                if p.is_file():
                    zips.append(p)
            except OSError:
                # en dernier recours : si le nom finit par .zip, on tente
                zips.append(p)

    _collect_dir(folder)
    if max_depth >= 1:
        try:
            for name in os.listdir(str(folder)):
                if name.startswith("."):  # .rsv_trash, dossiers cachés
                    continue
                sub = folder / name
                try:
                    if not sub.is_dir():
                        continue
                except OSError:
                    continue
                _collect_dir(sub)
                if max_depth >= 2:
                    try:
                        for name2 in os.listdir(str(sub)):
                            if name2.startswith("."):
                                continue
                            sub2 = sub / name2
                            try:
                                if sub2.is_dir():
                                    _collect_dir(sub2)
                            except OSError:
                                continue
                    except OSError:
                        pass
        except OSError:
            pass

    # dédup + tri
    seen = set()
    out: List[Path] = []
    for p in sorted(zips, key=lambda x: str(x).lower()):
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def recompress_zip_folder(
    folder: Path,
    max_depth: int = 0,
    compresslevel: int = 9,
) -> Dict[str, Any]:
    """Recompresse tous les .zip d'un dossier (profondeur 0 = racine seule)."""
    folder = Path(folder)
    if not folder.is_dir():
        return {"ok": False, "error": f"Dossier introuvable : {folder}"}

    zips = list_zip_files(folder, max_depth=max_depth)

    results = []
    ok_n = 0
    fail_n = 0
    total_before = 0
    total_after = 0
    for zp in zips:
        r = recompress_zip_file(zp, compresslevel=compresslevel)
        results.append(r)
        if r.get("ok"):
            ok_n += 1
            total_before += int(r.get("size_before") or 0)
            total_after += int(r.get("size_after") or 0)
        else:
            fail_n += 1

    return {
        "ok": True,
        "folder": str(folder),
        "total": len(zips),
        "recompressed": ok_n,
        "failed": fail_n,
        "size_before": total_before,
        "size_after": total_after,
        "saved": total_before - total_after,
        "items": results,
    }


@app.route("/api/delete", methods=["POST"])
@_exclusive(wait=5)
def api_delete():
    body = request.get_json(silent=True) or {}
    if _stale(body):
        return jsonify({"error": _STALE_MSG, "stale": True})
    try:
        idx = int(body.get("index", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "Index invalide"})
    if idx < 0 or idx >= len(STATE["results"]):
        return jsonify({"error": "Index invalide"})
    row = STATE["results"][idx]
    if _row_mismatch(row, body.get("path")):
        return jsonify({"error": _STALE_MSG, "stale": True})
    ok, msg = do_delete(row)
    if not ok:
        return jsonify({"error": msg})
    return jsonify({
        "ok": True,
        "message": msg,
        "row": _public_row(row),
        "counts": _counts(STATE["results"]),
    })


@app.route("/api/tools/rezip_list", methods=["POST"])
def api_tools_rezip_list():
    """Liste les .zip d'un dossier (pour traitement progressif côté client)."""
    body = request.get_json(silent=True) or {}
    path_s = (body.get("path") or STATE.get("roms_path") or str(DEFAULT_ROMS_DIR) or "").strip()
    if not path_s:
        return jsonify({"error": "Indiquez un dossier de ROMs"})
    folder = Path(path_s)
    if not folder.is_dir():
        return jsonify({"error": f"Dossier introuvable : {folder}"})
    try:
        max_depth = int(body.get("max_depth") or 0)
    except (TypeError, ValueError):
        max_depth = 0
    max_depth = max(0, min(2, max_depth))
    try:
        zips = list_zip_files(folder, max_depth=max_depth)
        return jsonify({
            "ok": True,
            "folder": str(folder),
            "total": len(zips),
            "files": [str(p) for p in zips],
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/tools/rezip", methods=["POST"])
@_exclusive(wait=5)
def api_tools_rezip():
    """
    Recompresse des ZIP en DEFLATE niveau 9 (max), sans 7-Zip.
    body:
      path: dossier (si pas de files) — traite TOUT le dossier
      max_depth: 0|1|2
      compresslevel: 0-9 (défaut 9)
      files: liste de chemins .zip (traitement unitaire / batch)
      list_only: si true, renvoie uniquement la liste des zip (pas de recompression)
      slim: si true, ne renvoie pas le détail de chaque item OK (gros sets)
    """
    body = request.get_json(silent=True) or {}

    # --- Listage seul (évite une 2e route si client ancien / partiel) ---
    if body.get("list_only"):
        path_s = (body.get("path") or STATE.get("roms_path") or str(DEFAULT_ROMS_DIR) or "").strip()
        if not path_s:
            return jsonify({"error": "Indiquez un dossier de ROMs"})
        folder = Path(path_s)
        if not folder.is_dir():
            return jsonify({"error": f"Dossier introuvable : {folder}"})
        try:
            max_depth = int(body.get("max_depth") or 0)
        except (TypeError, ValueError):
            max_depth = 0
        max_depth = max(0, min(2, max_depth))
        try:
            zips = list_zip_files(folder, max_depth=max_depth)
            sample = []
            try:
                sample = os.listdir(str(folder))[:40]
            except OSError as e:
                sample = [f"(listdir: {e})"]
            return jsonify({
                "ok": True,
                "list_only": True,
                "folder": str(folder),
                "total": len(zips),
                "files": [str(p) for p in zips],
                "sample_entries": sample,
                "resolved_path": str(folder.resolve()) if folder.exists() else str(folder),
            })
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)})

    level = body.get("compresslevel", 9)
    try:
        level = int(level)
    except (TypeError, ValueError):
        level = 9
    level = max(0, min(9, level))
    slim = bool(body.get("slim"))

    files = body.get("files")
    if files is not None:
        if not isinstance(files, list):
            return jsonify({"error": "files doit être une liste"})
        items = []
        ok_n = fail_n = 0
        total_before = total_after = 0
        for f in files:
            p = Path(str(f).strip())
            if not str(f).strip():
                continue
            r = recompress_zip_file(p, compresslevel=level)
            if r.get("ok"):
                ok_n += 1
                total_before += int(r.get("size_before") or 0)
                total_after += int(r.get("size_after") or 0)
                if not slim:
                    items.append(r)
            else:
                fail_n += 1
                items.append(r)  # toujours les échecs
        return jsonify({
            "ok": True,
            "total": ok_n + fail_n,
            "recompressed": ok_n,
            "failed": fail_n,
            "size_before": total_before,
            "size_after": total_after,
            "saved": total_before - total_after,
            "items": items,
        })

    path_s = (body.get("path") or STATE.get("roms_path") or str(DEFAULT_ROMS_DIR) or "").strip()
    if not path_s:
        return jsonify({"error": "Indiquez un dossier de ROMs"})
    folder = Path(path_s)
    try:
        max_depth = int(body.get("max_depth") or 0)
    except (TypeError, ValueError):
        max_depth = 0
    max_depth = max(0, min(2, max_depth))
    try:
        result = recompress_zip_folder(folder, max_depth=max_depth, compresslevel=level)
        if not result.get("ok") and result.get("error"):
            return jsonify({"error": result["error"]})
        # gros set : ne pas renvoyer 9000 items complets
        if slim and isinstance(result.get("items"), list) and len(result["items"]) > 50:
            result = dict(result)
            result["items"] = [x for x in result["items"] if not x.get("ok")]
            result["items_trimmed"] = True
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/fix_all", methods=["POST"])
@_exclusive(wait=5)
def api_fix_all():
    if _stale(request.get_json(silent=True) or {}):
        return jsonify({"error": _STALE_MSG, "stale": True})
    results = STATE.get("results") or []
    rows_out = []
    n = 0
    for i, r in enumerate(results):
        if r.get("status") != "rename":
            continue
        ok, msg = do_rename(r)
        if ok:
            n += 1
            rows_out.append({"index": i, "row": _public_row(r), "message": msg})
    return jsonify({
        "ok": True,
        "message": f"{n} fichier(s) renommé(s)",
        "renamed": n,
        "rows": rows_out,
        "counts": _counts(results),
    })


@app.route("/api/delete_selected", methods=["POST"])
@_exclusive(wait=5)
def api_delete_selected():
    body = request.get_json(silent=True) or {}
    if _stale(body):
        return jsonify({"error": _STALE_MSG, "stale": True})
    indices = body.get("indices") or []
    paths = body.get("paths")  # optionnel : chemin attendu pour chaque index
    if paths is not None and (not isinstance(paths, list) or len(paths) != len(indices)):
        return jsonify({"error": "paths doit avoir la même longueur que indices"})
    results = STATE.get("results") or []
    deleted = 0
    failed = []
    rows_out = []
    for pos, idx in enumerate(indices):
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            failed.append({"index": idx, "error": "Index invalide"})
            continue
        if idx < 0 or idx >= len(results):
            failed.append({"index": idx, "error": "Index hors limites"})
            continue
        row = results[idx]
        if paths is not None and _row_mismatch(row, paths[pos]):
            failed.append({"index": idx, "error": _STALE_MSG, "path": row.get("path") or ""})
            continue
        ok, msg = do_delete(row)
        if ok:
            deleted += 1
            rows_out.append({"index": idx, "row": _public_row(row), "message": msg})
        else:
            failed.append({
                "index": idx,
                "error": msg,
                "path": row.get("path") or row.get("found"),
            })
    return jsonify({
        "ok": True,
        "deleted": deleted,
        "failed": failed,
        "rows": rows_out,
        "counts": _counts(results),
    })


@app.route("/api/delete_bads", methods=["POST"])
@_exclusive(wait=5)
def api_delete_bads():
    if _stale(request.get_json(silent=True) or {}):
        return jsonify({"error": _STALE_MSG, "stale": True})
    results = STATE.get("results") or []
    rows_out = []
    n = 0
    for i, r in enumerate(results):
        if not _bulk_deletable(r):
            continue
        ok, msg = do_delete(r)
        if ok:
            n += 1
            rows_out.append({"index": i, "row": _public_row(r), "message": msg})
    return jsonify({
        "ok": True,
        "message": f"{n} fichier(s) déplacé(s) dans {TRASH_DIR_NAME}",
        "deleted": n,
        "rows": rows_out,
        "counts": _counts(results),
    })




def list_drives() -> List[Dict[str, str]]:
    """Lecteurs Windows (C:\\, D:\\, …) ou points de montage utiles sous Linux/macOS."""
    drives: List[Dict[str, str]] = []
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root):
                drives.append({"name": f"{letter}:", "path": root})
    else:
        # Racine + montages courants
        candidates = ["/", "/mnt", "/media", "/run/media", str(Path.home())]
        # Sous /media et /run/media : utilisateurs puis volumes
        extra: List[Path] = []
        for base in ("/media", "/run/media", "/mnt"):
            bp = Path(base)
            if bp.is_dir():
                try:
                    for child in bp.iterdir():
                        if child.is_dir():
                            extra.append(child)
                            try:
                                for sub in child.iterdir():
                                    if sub.is_dir():
                                        extra.append(sub)
                            except PermissionError:
                                pass
                except PermissionError:
                    pass
        seen = set()
        for c in candidates + [str(x) for x in extra]:
            try:
                rp = str(Path(c).resolve())
            except Exception:
                rp = c
            if rp in seen:
                continue
            if Path(rp).is_dir():
                seen.add(rp)
                drives.append({"name": rp, "path": rp})
    return drives


@app.route("/api/browse")
def api_browse():
    """
    Navigation dossiers.
    mode=roms : uniquement les sous-dossiers (ignore les milliers de .zip).
    mode=dat  : dossiers + fichiers .dat/.xml uniquement.
    """
    raw = request.args.get("path", str(DEFAULT_ROMS_DIR))
    mode = request.args.get("mode", "roms")
    # Éviter resolve() sur chemins réseau Windows (lent) — expanduser suffit
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p)
        p = Path(os.path.normpath(str(p)))
    except Exception:
        p = Path(DEFAULT_ROMS_DIR)

    try:
        exists = p.exists()
    except OSError:
        exists = False
    if not exists:
        p = Path(DEFAULT_ROMS_DIR)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    try:
        if p.is_file():
            p = p.parent
    except OSError:
        pass

    dirs: List[Dict[str, Any]] = []
    files: List[Dict[str, Any]] = []
    try:
        # scandir : un seul passage ; stat() pour date/taille des fichiers (mode dat/xml/exe)
        with os.scandir(str(p)) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        item: Dict[str, Any] = {
                            "name": entry.name,
                            "path": str(Path(p) / entry.name),
                            "size": 0,
                            "mtime": 0,
                        }
                        try:
                            st = entry.stat(follow_symlinks=False)
                            item["mtime"] = int(st.st_mtime)
                        except OSError:
                            pass
                        dirs.append(item)
                    elif mode in ("dat", "xml"):
                        low = entry.name.lower()
                        if low.endswith(".dat") or low.endswith(".xml"):
                            item = {
                                "name": entry.name,
                                "path": str(Path(p) / entry.name),
                                "size": 0,
                                "mtime": 0,
                            }
                            try:
                                st = entry.stat(follow_symlinks=False)
                                item["size"] = int(st.st_size)
                                item["mtime"] = int(st.st_mtime)
                            except OSError:
                                pass
                            files.append(item)
                    elif mode == "exe":
                        low = entry.name.lower()
                        if low.endswith(".exe") or low.endswith(".bat") or low.endswith(".cmd"):
                            item = {
                                "name": entry.name,
                                "path": str(Path(p) / entry.name),
                                "size": 0,
                                "mtime": 0,
                            }
                            try:
                                st = entry.stat(follow_symlinks=False)
                                item["size"] = int(st.st_size)
                                item["mtime"] = int(st.st_mtime)
                            except OSError:
                                pass
                            files.append(item)
                    # mode roms : ignore fichiers
                except OSError:
                    continue
    except PermissionError:
        return jsonify({"error": "Accès refusé", "drives": _list_drives_cached()})
    except OSError as e:
        return jsonify({"error": str(e), "drives": _list_drives_cached()})

    dirs.sort(key=lambda d: d["name"].lower())
    # Fichiers : par défaut date décroissante (DAT le plus récent en premier)
    if files:
        files.sort(key=lambda f: (-(f.get("mtime") or 0), f["name"].lower()))

    parent = None
    try:
        par = p.parent
        if par != p:
            parent = str(par)
    except Exception:
        parent = None
    if os.name == "nt":
        s = str(p)
        if len(s) <= 3 and len(s) >= 2 and s[1] == ":" and (len(s) == 2 or s[2] in "\\/"):
            parent = None

    return jsonify({
        "current": str(p),
        "parent": parent,
        "dirs": dirs,
        "files": files,
        "drives": _list_drives_cached(),
    })


_DRIVES_CACHE: Dict[str, Any] = {"t": 0.0, "list": []}


def _list_drives_cached() -> List[Dict[str, str]]:
    """Cache les lecteurs 60 s — list_drives() peut être lent sous Windows."""
    now = time.time()
    if _DRIVES_CACHE["list"] and (now - float(_DRIVES_CACHE["t"])) < 60:
        return _DRIVES_CACHE["list"]  # type: ignore
    try:
        drives = list_drives()
    except Exception:
        drives = []
    _DRIVES_CACHE["t"] = now
    _DRIVES_CACHE["list"] = drives
    return drives

def _public_row(row: Dict, include_components: bool = False) -> Dict:
    """Copie légère d'une ligne de résultat (sans components[] par défaut)."""
    if include_components:
        return dict(row)
    return {k: v for k, v in row.items() if k != "components"}


def _public_results(results: List[Dict]) -> List[Dict]:
    return [_public_row(r) for r in results]


def _counts(results: List[Dict]) -> Dict[str, int]:
    c = {"good": 0, "rename": 0, "bad": 0, "missing": 0, "error": 0, "incomplete": 0, "total": len(results or [])}
    for r in results or []:
        st = r.get("status") or ""
        if st in c:
            c[st] += 1
    # total DAT entries préféré si dispo (complétude vs set)
    gl = STATE.get("games_list") or []
    if gl:
        c["total"] = len(gl)
    return c

# ---------------------------------------------------------------------------
#  Catalogue & téléchargement No-Intro / Redump
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_download(url: str, dest: Path, timeout: int = 600) -> None:
    """Télécharge un fichier volumineux vers dest (écriture atomique)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    tmp.replace(dest)


def fetch_catalog(pack_id: str) -> List[Dict[str, str]]:
    """Catalogue distant (XML) ou DAT locaux déjà extraits pour le pack."""
    if pack_id not in DAT_PACKS:
        raise ValueError(f"Pack inconnu : {pack_id}")
    info = DAT_PACKS[pack_id]

    if not info.get("xml"):
        items: List[Dict[str, str]] = []
        root = DEFAULT_DAT_DIR / info["subdir"]
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.is_file() and p.suffix.lower() in (".dat", ".xml"):
                    rel = str(p.relative_to(root))
                    items.append({
                        "name": p.stem,
                        "version": rel,
                        "file": str(p),
                        "local": "1",
                    })
        return items

    raw = _http_get(info["xml"], timeout=60)
    try:
        root_el = etree.fromstring(raw)
    except etree.XMLSyntaxError:
        root_el = etree.fromstring(b"<root>" + raw + b"</root>")
    items = []
    for df in root_el.findall(".//datfile"):
        name = (df.findtext("name") or "").strip()
        version = (df.findtext("version") or "").strip()
        file_ = (df.findtext("file") or "").strip()
        if name and file_:
            items.append({"name": name, "version": version, "file": file_})
    items.sort(key=lambda x: x["name"].lower())
    return items


def list_local_dats() -> List[Dict[str, str]]:
    """DAT présents dans dat/ (récursif)."""
    out: List[Dict[str, str]] = []
    if not DEFAULT_DAT_DIR.exists():
        return out
    for p in sorted(DEFAULT_DAT_DIR.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".dat", ".xml"):
            rel = str(p.relative_to(DEFAULT_DAT_DIR))
            out.append({
                "name": p.stem,
                "path": str(p),
                "rel": rel,
                "size": p.stat().st_size,
            })
    return out


def _strip_github_root(name: str) -> str:
    """Enlève le premier segment type Repo-main/ des archives GitHub."""
    parts = Path(name).parts
    if not parts:
        return name
    root = parts[0]
    if (
        root.endswith("-main")
        or root.endswith("-master")
        or "-main" in root
        or "-master" in root
    ):
        return str(Path(*parts[1:])) if len(parts) > 1 else ""
    return name


def download_and_extract_pack(pack_id: str) -> Dict[str, Any]:
    """Télécharge le zip du pack et extrait les .dat/.xml dans dat/<subdir>/."""
    if pack_id not in DAT_PACKS:
        raise ValueError(f"Pack inconnu : {pack_id}")
    info = DAT_PACKS[pack_id]
    zip_path = CACHE_DIR / f"{pack_id}.zip"
    target = DEFAULT_DAT_DIR / info["subdir"]
    target.mkdir(parents=True, exist_ok=True)

    _http_download(info["zip"], zip_path)

    kind = info.get("kind", "flat")
    include_prefixes = info.get("include_prefixes")
    keep_structure = info.get("keep_structure", False)
    extracted = 0

    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            if ".." in name or name.startswith("/"):
                continue
            base = Path(name).name
            low = base.lower()
            if not (low.endswith(".dat") or low.endswith(".xml")):
                continue

            if kind == "github_archive":
                rel = _strip_github_root(name).replace("\\", "/")
                if not rel:
                    continue
                if include_prefixes:
                    matched = None
                    for pfx in include_prefixes:
                        if rel.startswith(pfx) or rel.startswith(pfx.replace("/", "\\")):
                            matched = pfx
                            break
                        # match folder name anywhere
                        segs = rel.replace("\\", "/").split("/")
                        if pfx.rstrip("/") in segs:
                            matched = pfx
                            break
                    if not matched:
                        continue
                    out_rel = rel
                    if rel.startswith(matched):
                        out_rel = rel[len(matched):]
                    dest = (target / out_rel) if keep_structure else (target / Path(out_rel).name)
                else:
                    dest = (target / rel) if keep_structure else (target / base)
            else:
                dest = target / base

            if not str(dest.resolve()).startswith(str(target.resolve())):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(dest, "wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            extracted += 1

    return {
        "ok": True,
        "pack": pack_id,
        "label": info["label"],
        "extracted": extracted,
        "dir": str(target),
        "zip": str(zip_path),
        "zip_size": zip_path.stat().st_size if zip_path.exists() else 0,
    }


PACK_CACHE_MAX_AGE = 24 * 3600  # les packs No-Intro / Redump sont reconstruits chaque jour


def _find_in_pack(zip_path: Path, file_name: str) -> Optional[str]:
    """Membre du pack dont le nom de fichier est EXACTEMENT file_name (casse ignorée)."""
    wanted = Path(file_name).name.lower()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            if name.lower() == file_name.lower() or Path(name).name.lower() == wanted:
                return name
    return None


def extract_one_from_pack(pack_id: str, file_name: str) -> Dict[str, Any]:
    """
    Extrait un seul DAT du pack en cache.
    Le cache est re-téléchargé s'il a plus de 24 h, ou si le DAT demandé n'y est pas
    (le catalogue est lu en ligne : un nom absent du cache signifie un cache périmé).
    Correspondance EXACTE uniquement : l'ancien repli par sous-chaîne pouvait donner
    « Game Boy Color » pour « Game Boy ».
    """
    if pack_id not in DAT_PACKS:
        raise ValueError(f"Pack inconnu : {pack_id}")
    info = DAT_PACKS[pack_id]
    zip_path = CACHE_DIR / f"{pack_id}.zip"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _fresh() -> bool:
        try:
            return zip_path.is_file() and (time.time() - zip_path.stat().st_mtime) < PACK_CACHE_MAX_AGE
        except OSError:
            return False

    downloaded = False
    if not _fresh():
        _http_download(info["zip"], zip_path)
        downloaded = True
    match = _find_in_pack(zip_path, file_name)
    if match is None and not downloaded:
        _http_download(info["zip"], zip_path)
        match = _find_in_pack(zip_path, file_name)
    if match is None:
        raise FileNotFoundError(
            f"DAT introuvable dans le pack {info['label']} : {file_name} "
            "(utilisez « Tout télécharger » pour obtenir le pack complet)"
        )

    target = DEFAULT_DAT_DIR / info["subdir"]
    target.mkdir(parents=True, exist_ok=True)
    base = Path(match).name
    dest = target / base
    part = dest.with_name(dest.name + ".part")
    with zipfile.ZipFile(zip_path, "r") as zf, zf.open(match) as src, open(part, "wb") as out:
        shutil.copyfileobj(src, out, _READ_BUF)
    _safe_replace(part, dest)
    return {"ok": True, "path": str(dest), "file": base, "pack": pack_id, "refreshed": downloaded}


@app.route("/api/dats/local")
def api_dats_local():
    try:
        return jsonify({"ok": True, "dats": list_local_dats()})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/dats/catalog")
def api_dats_catalog():
    pack_id = request.args.get("pack", "nointro")
    try:
        items = fetch_catalog(pack_id)
        return jsonify({
            "ok": True,
            "pack": pack_id,
            "label": DAT_PACKS.get(pack_id, {}).get("label", pack_id),
            "count": len(items),
            "items": items,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/dats/download_pack", methods=["POST"])
def api_dats_download_pack():
    pack_id = (request.get_json(silent=True) or {}).get("pack", "nointro")
    try:
        result = download_and_extract_pack(pack_id)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/dats/download_one", methods=["POST"])
def api_dats_download_one():
    body = request.get_json(silent=True) or {}
    pack_id = body.get("pack", "nointro")
    file_name = body.get("file", "").strip()
    if not file_name:
        return jsonify({"error": "Nom de fichier manquant"})
    try:
        p = Path(file_name)
        if p.is_file() and p.suffix.lower() in (".dat", ".xml"):
            return jsonify({"ok": True, "path": str(p.resolve()), "file": p.name, "pack": pack_id})
        result = extract_one_from_pack(pack_id, file_name)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})




@app.route("/api/mame/info")
def api_mame_info():
    """Infos ADB pour un ou plusieurs jeux (?games=a;b;c)."""
    games = request.args.get("games") or request.args.get("game") or ""
    names = [g.strip() for g in games.replace(",", ";").split(";") if g.strip()]
    if not names:
        return jsonify({"error": "Paramètre games manquant"})
    try:
        info = fetch_adb_mame(names)
        return jsonify({"ok": True, "items": info, "count": len(info)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/mame/enrich", methods=["POST"])
@_exclusive()
def api_mame_enrich():
    """
    Enrichit les résultats du scan arcade avec les titres ADB (Nom complet).
    Body optionnel: { "games": ["1942", ...] } — sinon tous les résultats avec un game.
    """
    if not STATE.get("results"):
        return jsonify({"error": "Aucun résultat à enrichir — lancez un scan d'abord"})
    body = request.get_json(silent=True) or {}
    games = body.get("games")
    if not games:
        games = []
        for r in STATE["results"]:
            g = (r.get("game") or "").strip()
            if g and g not in games:
                games.append(g)
    if not games:
        return jsonify({"error": "Aucun nom de jeu trouvé"})
    try:
        info = fetch_adb_mame(games)
        updated = 0
        rows_out = []
        for i, r in enumerate(STATE["results"]):
            g = (r.get("game") or "").strip().lower()
            if not g or g not in info:
                continue
            meta = info[g]
            changed = False
            title = meta.get("title") or ""
            if title and r.get("description") != title:
                r["description"] = title
                r["adb_title"] = title
                changed = True
            elif title:
                r["adb_title"] = title
            for key_src, key_dst in (
                ("manufacturer", "manufacturer"),
                ("year", "year"),
                ("genre", "genre"),
                ("url", "adb_url"),
                ("history", "history"),
            ):
                val = meta.get(key_src)
                if val and r.get(key_dst) != val:
                    r[key_dst] = val
                    changed = True
            if changed or title:
                updated += 1
                rows_out.append({"index": i, "row": _public_row(r)})
        return jsonify({
            "ok": True,
            "updated": updated,
            "queried": len(games),
            "found": len(info),
            "rows": rows_out,
            "counts": _counts(STATE["results"]),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})



# ---------------------------------------------------------------------------
#  Génération DAT / listxml depuis MAME
# ---------------------------------------------------------------------------

def mame_tag_from_version(version: str) -> str:
    """0.289 / 0289 / mame0289 → mame0289"""
    v = version.strip().lower()
    if re.match(r"^mame\d+$", v):
        return v
    digits = re.sub(r"\D", "", v)
    if not digits:
        raise ValueError(f"Version MAME invalide : {version}")
    if len(digits) <= 3:
        digits = digits.zfill(4)
    return "mame" + digits



# ---------------------------------------------------------------------------
#  Correspondances MAME ↔ RetroBat / Batocera / Recalbox
# ---------------------------------------------------------------------------

_FRONTEND_MAP_CACHE: Dict[str, Any] = {"t": 0.0, "map": {}}


def _parse_retrobat_changelog(text: str) -> Dict[str, str]:
    """frontend_version -> mame_version (bump introduit dans cette release)."""
    bumps: Dict[str, str] = {}
    current = None
    for L in text.splitlines():
        h = re.match(r"^##\s+RetroBat\s+v?(\d+\.\d+(?:\.\d+)?)", L, re.I)
        if h:
            current = h.group(1)
            continue
        if not current:
            continue
        m = re.search(r"Bump\s+MAME(?:64)?[^\n]*?\s+to\s+(0\.\d+)", L, re.I)
        if not m:
            m = re.search(r"Bump\s+mame[^\n]*?\s+to\s+(0\.\d+)", L, re.I)
        if m:
            ver = m.group(1)
            prev = bumps.get(current)
            if prev is None or float(ver[2:]) > float(prev[2:]):
                bumps[current] = ver
    return bumps


def _parse_batocera_changelog(text: str) -> Dict[str, str]:
    bumps: Dict[str, str] = {}
    current = None
    for L in text.splitlines():
        h = re.match(
            r"^#\s+\d{4}/[\dx]{2}/[\dx]{2}\s+-\s+batocera\.linux\s+(\d+(?:\.\d+)?)",
            L,
            re.I,
        )
        if h:
            current = h.group(1)
            continue
        if not current:
            continue
        ver = None
        for pat in (
            r"Groovy\s*MAME\s+to\s+v?(0\.\d+)",
            r"Libretro[- ]?MAME\s+to\s+v?(0\.\d+)",
            r"\bMAME\s+to\s+v?(0\.\d+)",
            r"bump:\s*MAME\s+to\s+v?(0\.\d+)",
            r"bump:\s*mame\s+to\s+v?(0\.\d+)",
            r"bump:\s*libretro-mame\s+to\s+v?(0\.\d+)",
            r"bump:\s*MAME\s+to\s+(\d{3})\b",
            r"lr-mame\s+to\s+v?(0\.\d+)",
        ):
            m = re.search(pat, L, re.I)
            if m:
                ver = m.group(1)
                if re.match(r"^\d{3}$", ver):
                    ver = f"0.{int(ver)}"
                break
        if ver and ver.startswith("0."):
            prev = bumps.get(current)
            if prev is None or float(ver[2:]) > float(prev[2:]):
                bumps[current] = ver
    return bumps


def _parse_recalbox_changelog(text: str) -> Dict[str, str]:
    bumps: Dict[str, str] = {}
    current = None
    for L in text.splitlines():
        h = re.match(r"^##\s*\[(\d+\.\d+(?:\.\d+)?)[^\]]*\]", L)
        if h:
            current = h.group(1)
            continue
        if not current:
            continue
        ver = None
        for pat in (
            r"Bump\s+Mame\s+libretro\s+core\s+to\s+(0\.\d{2,4})",
            r"Bump\s+libretro-mame\s+to\s+[^\n]*?\((0\.\d{2,4})\)",
            r"libretro-mame[^\n]*?\((0\.\d{2,4})\)",
            r"Romset\s+(0\.\d{2,4})",
        ):
            m = re.search(pat, L, re.I)
            if m:
                ver = m.group(1)
                break
        if ver and ver.startswith("0.") and len(ver) >= 5:  # ignore 0.2 false positives
            prev = bumps.get(current)
            if prev is None or float(ver[2:]) > float(prev[2:]):
                bumps[current] = ver
    return bumps


def _invert_frontend_bumps(bumps: Dict[str, str]) -> Dict[str, str]:
    """mame_version -> meilleure (plus récente) version frontend qui l'a introduite."""
    inv: Dict[str, str] = {}
    # bumps items order arbitrary; keep highest frontend? prefer first seen with highest mame
    # For same mame bumped in multiple FE versions, keep the newest FE version string
    def fe_key(v: str):
        parts = re.findall(r"\d+", v)
        return tuple(int(x) for x in parts) if parts else (0,)

    for fe, mame in bumps.items():
        if mame not in inv or fe_key(fe) > fe_key(inv[mame]):
            inv[mame] = fe
    return inv


def build_frontend_mame_map(force: bool = False) -> Dict[str, Dict[str, str]]:
    """
    Retourne { "0.288": {"retrobat": "8.2.0", "batocera": "44", "recalbox": ""}, ... }
    Cache 24h. Télécharge les changelogs officiels.
    """
    now = time.time()
    if not force and _FRONTEND_MAP_CACHE["map"] and (now - float(_FRONTEND_MAP_CACHE["t"])) < 86400:
        return _FRONTEND_MAP_CACHE["map"]  # type: ignore

    sources = {
        "retrobat": (
            "https://raw.githubusercontent.com/RetroBat-Official/retrobat/main/CHANGELOG.md",
            _parse_retrobat_changelog,
        ),
        "batocera": (
            "https://raw.githubusercontent.com/batocera-linux/batocera.linux/master/batocera-Changelog.md",
            _parse_batocera_changelog,
        ),
        "recalbox": (
            "https://gitlab.com/recalbox/recalbox/-/raw/master/CHANGELOG.md",
            _parse_recalbox_changelog,
        ),
    }

    per_fe_inv: Dict[str, Dict[str, str]] = {}
    for key, (url, parser) in sources.items():
        try:
            raw = _http_get(url, timeout=25)
            text = raw.decode("utf-8", errors="replace")
            bumps = parser(text)
            per_fe_inv[key] = _invert_frontend_bumps(bumps)
        except Exception:
            traceback.print_exc()
            per_fe_inv[key] = {}

    # Union de toutes les versions MAME connues
    all_mame = set()
    for inv in per_fe_inv.values():
        all_mame.update(inv.keys())

    result: Dict[str, Dict[str, str]] = {}
    for mv in all_mame:
        result[mv] = {
            "retrobat": per_fe_inv.get("retrobat", {}).get(mv, ""),
            "batocera": per_fe_inv.get("batocera", {}).get(mv, ""),
            "recalbox": per_fe_inv.get("recalbox", {}).get(mv, ""),
        }

    # Seed minimal si réseau KO (exemples utilisateur)
    if not result:
        result = {
            "0.288": {"retrobat": "8.2.0", "batocera": "44", "recalbox": ""},
            "0.285": {"retrobat": "", "batocera": "43", "recalbox": ""},
            "0.235": {"retrobat": "", "batocera": "", "recalbox": "8.0"},
        }

    _FRONTEND_MAP_CACHE["t"] = now
    _FRONTEND_MAP_CACHE["map"] = result
    return result


def format_frontend_suffix(mame_version: str, fmap: Dict[str, Dict[str, str]]) -> str:
    """Ex: ' — RetroBat 8.2.0 · Batocera 44'"""
    info = fmap.get(mame_version) or {}
    parts = []
    if info.get("retrobat"):
        parts.append(f"RetroBat {info['retrobat']}")
    if info.get("batocera"):
        parts.append(f"Batocera {info['batocera']}")
    if info.get("recalbox"):
        parts.append(f"Recalbox {info['recalbox']}")
    if not parts:
        return ""
    return " — " + " · ".join(parts)



# ---------------------------------------------------------------------------
#  FBNeo : versions / builds ↔ RetroBat / Batocera / Recalbox
# ---------------------------------------------------------------------------

_FBNEO_MAP_CACHE: Dict[str, Any] = {"t": 0.0, "map": {}}
_FBNEO_VER_CACHE: Dict[str, Any] = {"t": 0.0, "items": []}


def _norm_build_date(*parts) -> Optional[str]:
    """Normalise une date de build en YYYY-MM-DD si possible."""
    try:
        if len(parts) == 1 and isinstance(parts[0], str):
            s = parts[0].strip()
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
            if m:
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                return f"{y:04d}-{mo:02d}-{d:02d}"
            return None
        if len(parts) == 3:
            y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
            return f"{y:04d}-{mo:02d}-{d:02d}"
    except Exception:
        return None
    return None


_MONTHS = {
    "jan": 1, "january": 1, "januar": 1,
    "feb": 2, "february": 2, "februar": 2,
    "mar": 3, "march": 3, "märz": 3, "marz": 3,
    "apr": 4, "april": 4,
    "may": 5, "mai": 5,
    "jun": 6, "june": 6, "juni": 6,
    "jul": 7, "july": 7, "juli": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "okt": 10, "oktober": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12, "dezember": 12,
}


def _parse_english_date(s: str) -> Optional[str]:
    """11th of January 2026 / January 11, 2026 / Feb 23, 2024 / april 2025"""
    s = s.strip()
    m = re.search(
        r"(\d{1,2})(?:st|nd|rd|th)?\s+of\s+([A-Za-z]+)\s+(\d{4})",
        s, re.I,
    )
    if m:
        day, mon, year = int(m.group(1)), m.group(2).lower()[:3], int(m.group(3))
        mo = _MONTHS.get(mon) or _MONTHS.get(m.group(2).lower())
        if mo:
            return f"{year:04d}-{mo:02d}-{day:02d}"
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", s, re.I)
    if m:
        mon, day, year = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        mo = _MONTHS.get(mon[:3]) or _MONTHS.get(mon)
        if mo:
            return f"{year:04d}-{mo:02d}-{day:02d}"
    m = re.search(r"([A-Za-z]+)\s+(\d{4})", s, re.I)
    if m:
        mon, year = m.group(1).lower(), int(m.group(2))
        mo = _MONTHS.get(mon[:3]) or _MONTHS.get(mon)
        if mo:
            return f"{year:04d}-{mo:02d}-01"
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return _norm_build_date(m.group(0))
    return None


def _parse_retrobat_fbneo_changelog(text: str) -> Dict[str, str]:
    """frontend_version -> fbneo build date YYYY-MM-DD"""
    bumps: Dict[str, str] = {}
    current = None
    for L in text.splitlines():
        h = re.match(r"^##\s+RetroBat\s+v?(\d+\.\d+(?:\.\d+)?)", L, re.I)
        if h:
            current = h.group(1)
            continue
        if not current:
            continue
        if not re.search(r"fbneo|finalburn|fba\b", L, re.I):
            continue
        # Bump FBNEO ... from 22/04/2026 | from april 2025 | to version from ...
        d = None
        m = re.search(r"from\s+(\d{1,2}/\d{1,2}/\d{4})", L, re.I)
        if m:
            d = _norm_build_date(m.group(1))
        if not d:
            m = re.search(r"from\s+([A-Za-z]+\s+\d{4})", L, re.I)
            if m:
                d = _parse_english_date(m.group(1))
        if not d:
            m = re.search(r"to\s+(?:version\s+from\s+)?([A-Za-z]+\s+\d{4})", L, re.I)
            if m:
                d = _parse_english_date(m.group(1))
        if not d:
            m = re.search(r"\((\d{4}-\d{2}-\d{2})\)", L)
            if m:
                d = m.group(1)
        if d:
            prev = bumps.get(current)
            if prev is None or d > prev:
                bumps[current] = d
    return bumps


def _parse_batocera_fbneo_changelog(text: str) -> Dict[str, str]:
    bumps: Dict[str, str] = {}
    current = None
    for L in text.splitlines():
        h = re.match(
            r"^#\s+\d{4}/[\dx]{2}/[\dx]{2}\s+-\s+batocera\.linux\s+(\d+(?:\.\d+)?)",
            L,
            re.I,
        )
        if h:
            current = h.group(1)
            continue
        if not current:
            continue
        if not re.search(r"fbneo|finalburn|fba\b", L, re.I):
            continue
        d = None
        # Libretro FBNeo to 11th of January 2026 build
        m = re.search(
            r"(?:fbneo|FBNeo|Fbneo)[^\n]*?\bto\s+(.+?)\s+build",
            L,
            re.I,
        )
        if m:
            d = _parse_english_date(m.group(1))
        if not d:
            m = re.search(r"to\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})", L, re.I)
            if m:
                d = _parse_english_date(m.group(1))
        if not d:
            m = re.search(r"\((v?[\d.]+)\)", L)
            # keep semantic as pseudo version with line date unknown — skip
        if d:
            prev = bumps.get(current)
            if prev is None or d > prev:
                bumps[current] = d
    return bumps


def _parse_recalbox_fbneo_changelog(text: str) -> Dict[str, str]:
    bumps: Dict[str, str] = {}
    current = None
    for L in text.splitlines():
        h = re.match(r"^#+\s*(?:Recalbox\s+)?v?(\d+\.\d+(?:\.\d+)?)", L, re.I)
        if h:
            current = h.group(1)
            continue
        if not current:
            continue
        if not re.search(r"fbneo|finalburn|fba\b", L, re.I):
            continue
        d = None
        m = re.search(r"(\d{4}-\d{2}-\d{2})", L)
        if m:
            d = m.group(1)
        if not d:
            m = re.search(r"to\s+(.+?)\s+build", L, re.I)
            if m:
                d = _parse_english_date(m.group(1))
        if d:
            prev = bumps.get(current)
            if prev is None or d > prev:
                bumps[current] = d
    return bumps


def build_frontend_fbneo_map(force: bool = False) -> Dict[str, Dict[str, str]]:
    """
    { "2026-04-22": {"retrobat": "x.y", "batocera": "44", "recalbox": ""}, ... }
    Clé = date de build FBNeo (YYYY-MM-DD).
    """
    now = time.time()
    if not force and _FBNEO_MAP_CACHE["map"] and (now - float(_FBNEO_MAP_CACHE["t"])) < 86400:
        return _FBNEO_MAP_CACHE["map"]  # type: ignore

    sources = {
        "retrobat": (
            "https://raw.githubusercontent.com/RetroBat-Official/retrobat/main/CHANGELOG.md",
            _parse_retrobat_fbneo_changelog,
        ),
        "batocera": (
            "https://raw.githubusercontent.com/batocera-linux/batocera.linux/master/batocera-Changelog.md",
            _parse_batocera_fbneo_changelog,
        ),
        "recalbox": (
            "https://gitlab.com/recalbox/recalbox/-/raw/master/CHANGELOG.md",
            _parse_recalbox_fbneo_changelog,
        ),
    }

    per_fe_inv: Dict[str, Dict[str, str]] = {}
    for key, (url, parser) in sources.items():
        try:
            raw = _http_get(url, timeout=25)
            text = raw.decode("utf-8", errors="replace")
            bumps = parser(text)  # fe -> date
            per_fe_inv[key] = _invert_frontend_bumps(bumps)  # date -> fe
        except Exception:
            traceback.print_exc()
            per_fe_inv[key] = {}

    all_dates = set()
    for inv in per_fe_inv.values():
        all_dates.update(inv.keys())

    result: Dict[str, Dict[str, str]] = {}
    for d in all_dates:
        result[d] = {
            "retrobat": per_fe_inv.get("retrobat", {}).get(d, ""),
            "batocera": per_fe_inv.get("batocera", {}).get(d, ""),
            "recalbox": per_fe_inv.get("recalbox", {}).get(d, ""),
        }

    _FBNEO_MAP_CACHE["t"] = now
    _FBNEO_MAP_CACHE["map"] = result
    return result


def format_fbneo_frontend_suffix(build_date: str, fmap: Dict[str, Dict[str, str]]) -> str:
    info = fmap.get(build_date) or {}
    parts = []
    if info.get("retrobat"):
        parts.append(f"RetroBat {info['retrobat']}")
    if info.get("batocera"):
        parts.append(f"Batocera {info['batocera']}")
    if info.get("recalbox"):
        parts.append(f"Recalbox {info['recalbox']}")
    if not parts:
        return ""
    return " — " + " · ".join(parts)


def fetch_fbneo_versions(limit: int = 40, force: bool = False) -> List[Dict[str, Any]]:
    """
    Historique des builds FBNeo :
    - entrée « latest » (master / DAT pack actuel)
    - dates issues des changelogs RetroBat / Batocera / Recalbox
    - tags GitHub (finalburnneo / libretro) si dispo
    """
    now = time.time()
    if (
        not force
        and _FBNEO_VER_CACHE["items"]
        and (now - float(_FBNEO_VER_CACHE["t"])) < 3600
    ):
        return _FBNEO_VER_CACHE["items"][:limit]  # type: ignore

    fmap = build_frontend_fbneo_map(force=force)
    items: List[Dict[str, Any]] = []
    seen = set()

    def _add(entry: Dict[str, Any]):
        key = entry.get("id") or entry.get("tag") or entry.get("build_date")
        if not key or key in seen:
            return
        seen.add(key)
        bd = entry.get("build_date") or ""
        entry["frontends"] = fmap.get(bd) or {}
        entry["frontend_label"] = format_fbneo_frontend_suffix(bd, fmap)
        items.append(entry)

    # Latest (pack téléchargeable)
    _add({
        "id": "latest",
        "tag": "latest",
        "name": "FBNeo latest (master)",
        "build_date": "",
        "published": "",
        "source": "github_master",
        "is_latest": True,
    })

    # Tags GitHub
    for repo in ("finalburnneo/FBNeo", "libretro/FBNeo"):
        try:
            raw = _http_get(
                f"https://api.github.com/repos/{repo}/releases?per_page=15",
                timeout=12,
            )
            releases = json.loads(raw.decode("utf-8"))
            if not isinstance(releases, list):
                continue
            for rel in releases:
                tag = (rel.get("tag_name") or "").strip()
                if not tag or tag.lower() == "latest":
                    continue
                pub = (rel.get("published_at") or "")[:10]
                _add({
                    "id": f"tag:{tag}",
                    "tag": tag,
                    "name": f"FBNeo {tag}",
                    "build_date": pub,
                    "published": pub,
                    "source": f"github:{repo}",
                    "is_latest": False,
                })
        except Exception:
            traceback.print_exc()

    # Dates depuis changelogs (historique frontend)
    for bd in sorted(fmap.keys(), reverse=True):
        _add({
            "id": f"build:{bd}",
            "tag": bd,
            "name": f"FBNeo build {bd}",
            "build_date": bd,
            "published": bd,
            "source": "changelog",
            "is_latest": False,
        })

    # Tri : latest d'abord, puis date desc
    def _sort_key(it: Dict[str, Any]):
        if it.get("is_latest"):
            return ("9", "9999")
        return ("0", it.get("build_date") or it.get("published") or "")

    items.sort(key=_sort_key, reverse=True)
    _FBNEO_VER_CACHE["t"] = now
    _FBNEO_VER_CACHE["items"] = items
    return items[:limit]


FBNEO_VERSIONS_DIR = "versions"  # dat/fbneo/versions/<version>/ : jamais proposé automatiquement


def download_fbneo_dat_version(version_id: str) -> Dict[str, Any]:
    """
    Télécharge le pack DAT FBNeo.
    - latest / vide → archive master (comportement pack fbneo)
    - build:YYYY-MM-DD → commit le plus proche sur dats/ puis archive
    - tag:xxx → archive de ce tag
    """
    version_id = (version_id or "latest").strip()
    cache = CACHE_DIR / "fbneo"
    cache.mkdir(parents=True, exist_ok=True)

    sha = None
    label = version_id
    if version_id in ("latest", "master", ""):
        zip_url = "https://github.com/libretro/FBNeo/archive/refs/heads/master.zip"
        label = "latest"
    elif version_id.startswith("tag:"):
        tag = version_id[4:]
        zip_url = f"https://github.com/libretro/FBNeo/archive/refs/tags/{tag}.zip"
        label = tag
    elif version_id.startswith("build:") or re.match(r"^\d{4}-\d{2}-\d{2}$", version_id):
        bd = version_id.split(":")[-1]
        label = bd
        # Commit sur dats/ antérieur à la date
        api = (
            "https://api.github.com/repos/libretro/FBNeo/commits"
            f"?path=dats&until={bd}T23:59:59Z&per_page=1"
        )
        try:
            raw = _http_get(api, timeout=20)
            commits = json.loads(raw.decode("utf-8"))
            if isinstance(commits, list) and commits:
                sha = commits[0].get("sha")
        except Exception:
            traceback.print_exc()
        if not sha:
            raise ValueError(f"Aucun commit dats/ trouvé pour {bd}")
        zip_url = f"https://github.com/libretro/FBNeo/archive/{sha}.zip"
        label = f"{bd}-{sha[:7]}"
    else:
        # tag nu
        zip_url = f"https://github.com/libretro/FBNeo/archive/refs/tags/{version_id}.zip"
        label = version_id

    safe_label = re.sub(r'[^A-Za-z0-9._-]+', '_', label)
    # latest → dat/fbneo/ (DAT courant, proposé automatiquement aux profils) ;
    # autre version → dat/fbneo/versions/<version>/ : ne remplace plus le DAT courant
    if label == "latest":
        target = DEFAULT_DAT_DIR / "fbneo"
    else:
        target = DEFAULT_DAT_DIR / "fbneo" / FBNEO_VERSIONS_DIR / safe_label
    target.mkdir(parents=True, exist_ok=True)
    zip_path = cache / f"fbneo_{safe_label}.zip"
    _http_download(zip_url, zip_path)

    extracted = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.endswith("/") or ".." in name:
                continue
            base = Path(name).name
            low = base.lower()
            if not (low.endswith(".dat") or low.endswith(".xml")):
                continue
            # garder uniquement dats/
            rel = name.replace("\\", "/")
            if "/dats/" not in "/" + rel and not rel.split("/")[-2:][0] == "dats":
                # accepte si le segment dats est présent
                if "dats/" not in rel and not rel.endswith("/dats"):
                    # encore : fichier .dat à la racine d'un sous-dossier dats
                    segs = rel.split("/")
                    if "dats" not in segs:
                        continue
            dest = target / base
            if not str(dest.resolve()).startswith(str(target.resolve())):
                continue
            with zf.open(name) as src, open(dest, "wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            extracted += 1

    if extracted == 0:
        raise ValueError("Aucun fichier .dat/.xml extrait (dossier dats/ manquant ?)")

    return {
        "ok": True,
        "label": label,
        "version_id": version_id,
        "extracted": extracted,
        "dir": str(target),
        "zip": str(zip_path),
        "sha": sha or "",
    }



def fetch_mame_versions(limit: int = 60) -> List[Dict[str, Any]]:
    """
    Liste des versions MAME avec listxml (*lx.zip).
    Toujours une liste locale immédiate (pas de dépendance réseau),
    éventuellement enrichie via l'API GitHub si accessible.
    """
    def _entry(tag: str, ver_disp: str, published: str = "", lx_url: str = "",
               lx_name: str = "", lx_size: int = 0) -> Dict[str, Any]:
        if not lx_name:
            lx_name = f"{tag}lx.zip"
        if not lx_url:
            lx_url = f"https://github.com/mamedev/mame/releases/download/{tag}/{lx_name}"
        return {
            "tag": tag,
            "name": f"MAME {ver_disp}",
            "version": ver_disp,
            "published": published,
            "lx_url": lx_url,
            "lx_name": lx_name,
            "lx_size": lx_size,
            "bin_url": "",
            "bin_name": "",
        }

    # Base locale : MAME_LATEST_KNOWN → 0.160 (listxml GitHub fiable sur cette plage) ;
    # les versions plus récentes sont ajoutées par l'API GitHub ci-dessous
    latest = MAME_LATEST_KNOWN
    oldest = 160
    items: List[Dict[str, Any]] = []
    for n in range(latest, max(oldest - 1, latest - limit), -1):
        tag = f"mame{n:04d}"
        items.append(_entry(tag, f"0.{n}"))

    # Enrichissement optionnel via API (non bloquant si échec)
    try:
        url = "https://api.github.com/repos/mamedev/mame/releases?per_page=15"
        raw = _http_get(url, timeout=12)
        releases = json.loads(raw.decode("utf-8"))
        if isinstance(releases, list):
            by_tag = {it["tag"]: it for it in items}
            for rel in releases:
                tag = rel.get("tag_name") or ""
                assets = rel.get("assets") or []
                lx = next((a for a in assets if str(a.get("name", "")).endswith("lx.zip")), None)
                if not tag or not lx:
                    continue
                digits = re.sub(r"\D", "", tag)
                ver_disp = f"0.{int(digits)}" if digits else tag
                entry = _entry(
                    tag,
                    ver_disp,
                    published=(rel.get("published_at") or "")[:10],
                    lx_url=lx.get("browser_download_url") or "",
                    lx_name=lx.get("name") or f"{tag}lx.zip",
                    lx_size=int(lx.get("size") or 0),
                )
                if tag in by_tag:
                    by_tag[tag].update(entry)
                else:
                    items.insert(0, entry)
                    by_tag[tag] = entry
            # Retrier par numéro décroissant
            def _key(it: Dict[str, Any]) -> int:
                d = re.sub(r"\D", "", it.get("tag") or "")
                return int(d) if d else 0
            items.sort(key=_key, reverse=True)
    except Exception:
        pass

    # Annoter avec RetroBat / Batocera / Recalbox
    try:
        fmap = build_frontend_mame_map()
    except Exception:
        fmap = {}
    for it in items:
        ver = it.get("version") or ""
        it["frontends"] = fmap.get(ver) or {}
        it["frontend_label"] = format_frontend_suffix(ver, fmap)

    return items[:limit]


def generate_mame_listxml_from_lx(tag: str) -> Dict[str, Any]:
    """Télécharge mameXXXlx.zip (export officiel listxml) → dat/mame/."""
    tag = mame_tag_from_version(tag)
    versions = fetch_mame_versions(80)
    info = next((v for v in versions if v["tag"] == tag), None)
    if not info or not info.get("lx_url"):
        # URL directe GitHub
        lx_name = f"{tag}lx.zip"
        lx_url = f"https://github.com/mamedev/mame/releases/download/{tag}/{lx_name}"
    else:
        lx_url = info["lx_url"]
        lx_name = info["lx_name"] or f"{tag}lx.zip"

    out_dir = DEFAULT_DAT_DIR / "mame"
    out_dir.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = CACHE_DIR / lx_name

    _http_download(lx_url, zip_path)

    extracted = None
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.lower().endswith(".xml") or name.lower().endswith(".dat"):
                base = Path(name).name
                dest = out_dir / base
                with zf.open(name) as src, open(dest, "wb") as out:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                extracted = str(dest)
                break
    if not extracted:
        raise RuntimeError("Aucun XML trouvé dans " + lx_name)

    return {
        "ok": True,
        "path": extracted,
        "tag": tag,
        "source": "listxml-zip",
        "size": Path(extracted).stat().st_size,
    }


def generate_mame_listxml_from_exe(mame_exe: Path) -> Dict[str, Any]:
    """Exécute `mame -listxml` et enregistre le résultat dans dat/mame/."""
    mame_exe = Path(mame_exe).expanduser()
    if not mame_exe.is_file():
        raise FileNotFoundError(f"MAME introuvable : {mame_exe}")

    out_dir = DEFAULT_DAT_DIR / "mame"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Détecter la version
    version = "unknown"
    try:
        proc = subprocess.run(
            [str(mame_exe), "-help"],
            capture_output=True,
            timeout=30,
            cwd=str(mame_exe.parent),
        )
        help_txt = (proc.stdout or b"").decode("utf-8", errors="replace")
        help_txt += (proc.stderr or b"").decode("utf-8", errors="replace")
        m = re.search(r"v?(\d+\.\d+[a-z0-9.]*)", help_txt, re.I)
        if m:
            version = m.group(1)
    except Exception:
        pass

    safe_ver = re.sub(r"[^\w.]+", "_", version)
    dest = out_dir / f"MAME {safe_ver}.xml"
    part = dest.with_name(dest.name + ".part")  # un DAT existant n'est remplacé qu'en cas de succès

    # -listxml peut produire 100+ Mo : stdout écrit directement dans le fichier par l'OS,
    # stderr dans un fichier temporaire. Avec deux PIPE dont un seul est lu, MAME se
    # bloquait dès que ses avertissements remplissaient le tampon de stderr.
    import tempfile
    with open(part, "wb") as out, tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(
            [str(mame_exe), "-listxml"],
            stdout=out,
            stderr=errf,
            stdin=subprocess.DEVNULL,
            cwd=str(mame_exe.parent),
        )
        try:
            proc.wait(timeout=900)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            part.unlink(missing_ok=True)
            raise RuntimeError("mame -listxml ne s'est pas terminé en 15 minutes")
        errf.seek(0)
        err = errf.read(4000).decode("utf-8", errors="replace")

    size = part.stat().st_size
    if size < 1000:
        part.unlink(missing_ok=True)
        if proc.returncode not in (0, None):
            raise RuntimeError(f"mame -listxml a échoué (code {proc.returncode}) : {err[:500]}")
        raise RuntimeError("XML généré trop petit — échec listxml")
    # certains builds renvoient un code non nul alors que le XML est complet
    _safe_replace(part, dest)

    return {
        "ok": True,
        "path": str(dest),
        "version": version,
        "source": "mame -listxml",
        "size": dest.stat().st_size,
    }



_KNOWN_BIOS_EXPORT = frozenset({
    "neogeo", "neocdz", "neogeo_noslot",
    "qsound", "cps1", "cps2", "cps3",
    "pgm", "skns", "midssio", "nmk004", "decocass", "isgsm",
    "bubsys", "aleck64", "naomi", "naomi2", "naomigd", "stvbios",
    "hikaru", "triforce", "chihiro", "lindbergh", "konamigv", "konamigx",
    "ym2608", "ym2413", "cchip", "cv1k", "maxaflex", "megaplay", "megatech",
})


def _machine_flags(el) -> Dict[str, Any]:
    name = el.get("name") or ""
    isbios = (el.get("isbios") or "").lower() in ("yes", "1", "true")
    isdevice = (el.get("isdevice") or "").lower() in ("yes", "1", "true")
    ismechanical = (el.get("ismechanical") or "").lower() in ("yes", "1", "true")
    runnable = (el.get("runnable") or "yes").lower() not in ("no", "0", "false")
    cloneof = el.get("cloneof") or ""
    romof = el.get("romof") or ""
    driver_el = el.find("driver")
    status = "good"
    if driver_el is not None:
        status = (driver_el.get("status") or driver_el.get("emulation") or "good").lower()
    year = (el.findtext("year") or "").strip()
    manufacturer = (el.findtext("manufacturer") or "").strip()
    desc = (el.findtext("description") or name).strip()
    has_roms = el.find("rom") is not None
    has_disks = el.find("disk") is not None
    return {
        "name": name,
        "isbios": isbios,
        "isdevice": isdevice,
        "ismechanical": ismechanical,
        "runnable": runnable,
        "cloneof": cloneof,
        "romof": romof,
        "status": status,
        "year": year,
        "manufacturer": manufacturer,
        "description": desc,
        "has_roms": has_roms,
        "has_disks": has_disks,
    }


def _resolve_bios_name(flags: Dict[str, Any], bios_set: set, romof_map: Dict[str, str]) -> str:
    if flags["isbios"]:
        return flags["name"]
    ro = flags.get("romof") or ""
    co = flags.get("cloneof") or ""
    if ro and ro in bios_set:
        return ro
    if ro and ro != co and ro != flags["name"]:
        if ro in bios_set or ro in _KNOWN_BIOS_EXPORT:
            return ro
        # cible absente du DAT = souvent un BIOS
        if ro not in romof_map and ro not in bios_set:
            # still could be parent not in filtered set — only treat known
            if ro in _KNOWN_BIOS_EXPORT:
                return ro
    # héritage parent
    if co:
        pro = romof_map.get(co) or ""
        if pro in bios_set or pro in _KNOWN_BIOS_EXPORT:
            return pro
    if ro and ro != co and (ro in _KNOWN_BIOS_EXPORT):
        return ro
    return ""



# ---------------------------------------------------------------------------
#  Progetto-SNAPS catlist.ini / genre.ini (filtres catégories)
# ---------------------------------------------------------------------------

# Dans dat/ à côté de l'application (SCRIPT_DIR), pas dans le dossier temporaire
# d'un exécutable PyInstaller (_MEIPASS, effacé à la fermeture).
PROGETTO_SUPPORT_DIR = DEFAULT_DAT_DIR / "support" / "CatVer"
_PROGETTO_CACHE: Dict[str, Any] = {"genre": None, "catlist": None, "loaded": False}


def _progetto_support_url(mame_ver_digits: str = "288") -> str:
    # Pack support aligné version MAME (fallback 288)
    return (
        "https://www.progettosnaps.net/download/?tipo=support_pack"
        f"&file=/support/packs/pS_SupportFiles_{mame_ver_digits}.zip"
    )


def ensure_progetto_catver(mame_hint: str = "") -> Path:
    """
    Télécharge/extrait CatVer (genre.ini, catlist.ini) depuis Progetto-SNAPS
    vers dat/support/CatVer/ si absent.
    """
    PROGETTO_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    genre = PROGETTO_SUPPORT_DIR / "genre.ini"
    catlist = PROGETTO_SUPPORT_DIR / "catlist.ini"
    if genre.is_file() and catlist.is_file():
        return PROGETTO_SUPPORT_DIR

    digits = "288"
    m = re.search(r"(\d{3,4})", mame_hint or "")
    if m:
        digits = m.group(1)[-3:] if len(m.group(1)) > 3 else m.group(1)
        # 0288 -> 288
        digits = str(int(digits)) if digits.isdigit() else "288"

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = CACHE_DIR / f"pS_SupportFiles_{digits}.zip"
    urls = [
        _progetto_support_url(digits),
        _progetto_support_url("288"),
    ]
    last_err = None
    for url in urls:
        try:
            if not zip_path.is_file() or zip_path.stat().st_size < 1000:
                zip_path = CACHE_DIR / Path(url).name.replace("=", "_")
                # keep stable name
                zip_path = CACHE_DIR / f"pS_SupportFiles_{digits}.zip"
                _http_download(url, zip_path, timeout=180)
            break
        except Exception as e:
            last_err = e
            zip_path = CACHE_DIR / "pS_SupportFiles_288.zip"
            digits = "288"
            continue
    else:
        raise RuntimeError(f"Téléchargement Progetto-SNAPS impossible : {last_err}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            low = name.replace("\\", "/").lower()
            if not low.endswith(".ini"):
                continue
            base = Path(name).name.lower()
            if base in ("genre.ini", "catlist.ini", "catver.ini", "mature.ini", "genre_ows.ini"):
                dest = PROGETTO_SUPPORT_DIR / Path(name).name
                with zf.open(name) as src, open(dest, "wb") as out:
                    out.write(src.read())
    if not genre.is_file() and not catlist.is_file():
        raise RuntimeError("genre.ini / catlist.ini introuvables dans le pack Progetto-SNAPS")
    _PROGETTO_CACHE["loaded"] = False
    return PROGETTO_SUPPORT_DIR


def _parse_mame_folder_ini(path: Path) -> Dict[str, List[str]]:
    """
    Parse un .ini style folders MAME :
      [Section]
      romset
    Retourne { section: [romsets...] }
    """
    sections: Dict[str, List[str]] = {}
    cur = None
    skip_sections = {"FOLDER_SETTINGS", "ROOT_FOLDER"}
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1].strip()
            if cur not in skip_sections:
                sections.setdefault(cur, [])
            else:
                cur = None
            continue
        if cur is None:
            continue
        # ignore key value settings
        if "=" in line and line.split("=", 1)[0].strip() in (
            "RootFolderIcon", "SubFolderIcon", "Icon",
        ):
            continue
        # romset name = first token
        name = line.split()[0].strip()
        if name and not name.startswith("["):
            sections[cur].append(name)
    return sections


def load_progetto_maps(force: bool = False) -> Dict[str, Any]:
    """
    Charge genre.ini + catlist.ini.
    Retourne {
      genre_sections: {section: [games]},
      catlist_sections: {...},
      game_genres: {game: set(sections)},
      game_cats: {game: set(sections)},
      genre_names: [...],
      catlist_names: [...],
    }
    """
    if _PROGETTO_CACHE["loaded"] and not force and _PROGETTO_CACHE.get("data"):
        return _PROGETTO_CACHE["data"]  # type: ignore

    ensure_progetto_catver()
    genre_path = PROGETTO_SUPPORT_DIR / "genre.ini"
    cat_path = PROGETTO_SUPPORT_DIR / "catlist.ini"
    genre_sec = _parse_mame_folder_ini(genre_path) if genre_path.is_file() else {}
    cat_sec = _parse_mame_folder_ini(cat_path) if cat_path.is_file() else {}

    game_genres: Dict[str, set] = {}
    for sec, games in genre_sec.items():
        for g in games:
            game_genres.setdefault(g.lower(), set()).add(sec)
    game_cats: Dict[str, set] = {}
    for sec, games in cat_sec.items():
        for g in games:
            game_cats.setdefault(g.lower(), set()).add(sec)

    data = {
        "genre_sections": genre_sec,
        "catlist_sections": cat_sec,
        "game_genres": game_genres,
        "game_cats": game_cats,
        "genre_names": sorted(genre_sec.keys(), key=str.lower),
        "catlist_names": sorted(cat_sec.keys(), key=str.lower),
    }
    _PROGETTO_CACHE["data"] = data
    _PROGETTO_CACHE["loaded"] = True
    return data


def game_matches_category_filters(
    game: str,
    maps: Dict[str, Any],
    genres_include: List[str],
    genres_exclude: List[str],
    cats_include: List[str],
    cats_exclude: List[str],
) -> bool:
    """True si le jeu passe les filtres genre/catlist."""
    g = (game or "").lower()
    gset = maps.get("game_genres", {}).get(g) or set()
    cset = maps.get("game_cats", {}).get(g) or set()

    def _match_any(needles: List[str], haystacks: set) -> bool:
        if not needles:
            return False
        low_h = {h.lower() for h in haystacks}
        for n in needles:
            nl = n.lower().strip()
            if not nl:
                continue
            if nl in low_h:
                return True
            # sous-chaîne (ex. "Mahjong" dans "Arcade: Tabletop / Mahjong")
            for h in low_h:
                if nl in h:
                    return True
        return False

    # exclude prioritaire
    if genres_exclude and _match_any(genres_exclude, gset):
        return False
    if cats_exclude and _match_any(cats_exclude, cset):
        return False

    # include : si spécifié, au moins un match genre OU catlist
    if genres_include or cats_include:
        ok = False
        if genres_include and _match_any(genres_include, gset):
            ok = True
        if cats_include and _match_any(cats_include, cset):
            ok = True
        if not ok:
            return False
    return True


@app.route("/api/mame/categories")
def api_mame_categories():
    """Liste genres / catlist Progetto-SNAPS (télécharge si besoin)."""
    try:
        force = request.args.get("refresh") in ("1", "true", "yes")
        if force or not (PROGETTO_SUPPORT_DIR / "genre.ini").is_file():
            ensure_progetto_catver(request.args.get("mame") or "")
        maps = load_progetto_maps(force=force)
        # Compteurs utiles pour l'UI
        genre_counts = [
            {"name": k, "count": len(v)}
            for k, v in sorted(maps["genre_sections"].items(), key=lambda x: x[0].lower())
        ]
        # catlist : regrouper les racines intéressantes Mahjong/Casino/etc.
        return jsonify({
            "ok": True,
            "genres": genre_counts,
            "catlist": maps["catlist_names"],
            "path": str(PROGETTO_SUPPORT_DIR),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


def analyze_mame_xml(source: Path) -> Dict[str, Any]:
    """Statistiques + liste des BIOS détectés dans un listxml."""
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(str(source))

    stats = {
        "total": 0, "parents": 0, "clones": 0, "bios": 0, "devices": 0,
        "mechanical": 0, "with_roms": 0, "with_disks": 0,
        "status": {"good": 0, "imperfect": 0, "preliminary": 0, "other": 0},
    }
    bios_names: set = set()
    romof_map: Dict[str, str] = {}
    flags_list: List[Dict[str, Any]] = []

    root_attrib: Dict[str, str] = {}
    for _ev, el in etree.iterparse(str(source), events=("start",), tag=("mame", "datafile"), huge_tree=True):
        root_attrib = dict(el.attrib)
        break

    for _ev, el in etree.iterparse(str(source), events=("end",), tag="machine", huge_tree=True):
        f = _machine_flags(el)
        flags_list.append(f)
        stats["total"] += 1
        if f["isbios"]:
            stats["bios"] += 1
            bios_names.add(f["name"])
        if f["isdevice"]:
            stats["devices"] += 1
        if f["ismechanical"]:
            stats["mechanical"] += 1
        if f["cloneof"]:
            stats["clones"] += 1
        elif not f["isbios"] and not f["isdevice"]:
            stats["parents"] += 1
        if f["has_roms"]:
            stats["with_roms"] += 1
        if f["has_disks"]:
            stats["with_disks"] += 1
        st = f["status"]
        if st in stats["status"]:
            stats["status"][st] += 1
        else:
            stats["status"]["other"] += 1
        if f["romof"]:
            romof_map[f["name"]] = f["romof"]
        el.clear()

    bios_names |= set(_KNOWN_BIOS_EXPORT)
    # BIOS réellement référencés
    used_bios: Dict[str, int] = {}
    for f in flags_list:
        b = _resolve_bios_name(f, bios_names, romof_map)
        if b:
            used_bios[b] = used_bios.get(b, 0) + 1

    bios_list = [
        {"name": k, "count": v}
        for k, v in sorted(used_bios.items(), key=lambda x: (-x[1], x[0]))
    ]

    return {
        "ok": True,
        "path": str(source),
        "size": source.stat().st_size,
        "root": root_attrib,
        "stats": stats,
        "bios": bios_list,
    }



def export_filtered_mame_xml(source: Path, dest: Path, opts: Dict[str, Any]) -> Dict[str, Any]:
    """
    Filtre un listxml MAME et écrit un nouvel XML.
    Inclut automatiquement les sets BIOS référencés par les machines gardées
    (ex. neogeo pour les jeux Neo-Geo), sauf si opts['auto_bios'] is False.
    """
    source = Path(source)
    dest = Path(dest)
    if not source.is_file():
        raise FileNotFoundError(str(source))
    dest.parent.mkdir(parents=True, exist_ok=True)

    parents_only = bool(opts.get("parents_only"))
    include_clones = bool(opts.get("include_clones", True))
    include_bios = bool(opts.get("include_bios", True))
    include_devices = bool(opts.get("include_devices", False))
    include_mechanical = bool(opts.get("include_mechanical", False))
    runnable_only = bool(opts.get("runnable_only", False))
    require_roms = bool(opts.get("require_roms", True))
    auto_bios = bool(opts.get("auto_bios", True))
    bios_filter = [b.strip().lower() for b in (opts.get("bios_filter") or []) if b and str(b).strip()]
    status_filter = [s.strip().lower() for s in (opts.get("driver_status") or []) if s and str(s).strip()]
    manufacturer = (opts.get("manufacturer") or "").strip().lower()
    description_q = (opts.get("description") or "").strip().lower()
    year_from = opts.get("year_from")
    year_to = opts.get("year_to")
    try:
        year_from_i = int(year_from) if year_from not in (None, "") else None
    except ValueError:
        year_from_i = None
    try:
        year_to_i = int(year_to) if year_to not in (None, "") else None
    except ValueError:
        year_to_i = None
    genres_include = [x.strip() for x in (opts.get("genres_include") or []) if x and str(x).strip()]
    genres_exclude = [x.strip() for x in (opts.get("genres_exclude") or []) if x and str(x).strip()]
    cats_include = [x.strip() for x in (opts.get("cats_include") or []) if x and str(x).strip()]
    cats_exclude = [x.strip() for x in (opts.get("cats_exclude") or []) if x and str(x).strip()]
    cat_maps = None
    if genres_include or genres_exclude or cats_include or cats_exclude:
        try:
            ensure_progetto_catver(str(source))
            cat_maps = load_progetto_maps()
        except Exception as e:
            traceback.print_exc()
            if (PROGETTO_SUPPORT_DIR / "genre.ini").is_file() or (PROGETTO_SUPPORT_DIR / "catlist.ini").is_file():
                cat_maps = load_progetto_maps(force=True)
            else:
                raise RuntimeError(
                    "Filtres catégories demandés mais genre.ini/catlist.ini indisponibles. "
                    "Cliquez « Charger catlist » d'abord. Détail : " + str(e)
                )

    # --- Passe 1 : index romof / bios / device_ref ---
    bios_names: set = set(_KNOWN_BIOS_EXPORT)
    romof_map: Dict[str, str] = {}
    machine_names: set = set()
    root_attrib: Dict[str, str] = {}
    for _ev, el in etree.iterparse(str(source), events=("start",), tag=("mame", "datafile"), huge_tree=True):
        root_attrib = dict(el.attrib)
        break
    for _ev, el in etree.iterparse(str(source), events=("end",), tag="machine", huge_tree=True):
        name = el.get("name") or ""
        if not name:
            el.clear()
            continue
        machine_names.add(name)
        if (el.get("isbios") or "").lower() in ("yes", "1", "true"):
            bios_names.add(name)
        ro = el.get("romof") or ""
        if ro:
            romof_map[name] = ro
        el.clear()

    force_bios_sets = set(bios_filter) if bios_filter else set()

    def _passes_filters(f: Dict[str, Any], bios: str) -> bool:
        keep = True
        if f["isbios"]:
            if not include_bios and f["name"] not in force_bios_sets:
                keep = False
        elif f["isdevice"]:
            if not include_devices:
                keep = False
        elif f["ismechanical"] and not include_mechanical:
            keep = False
        else:
            if parents_only and f["cloneof"]:
                keep = False
            if not include_clones and f["cloneof"]:
                keep = False

        if keep and runnable_only and not f["runnable"] and not f["isbios"]:
            keep = False
        if keep and require_roms and not f["has_roms"] and not f["has_disks"] and not f["isbios"]:
            keep = False
        if keep and status_filter and f["status"] not in status_filter and not f["isbios"]:
            keep = False
        if keep and manufacturer and manufacturer not in (f["manufacturer"] or "").lower():
            keep = False
        if keep and description_q and description_q not in (f["description"] or "").lower():
            keep = False
        if keep and year_from_i is not None:
            try:
                y = int(f["year"][:4]) if f["year"] else None
            except ValueError:
                y = None
            if y is None or y < year_from_i:
                keep = False
        if keep and year_to_i is not None:
            try:
                y = int(f["year"][:4]) if f["year"] else None
            except ValueError:
                y = None
            if y is None or y > year_to_i:
                keep = False
        if keep and bios_filter:
            if f["isbios"]:
                if f["name"].lower() not in bios_filter:
                    keep = False
            elif (bios or "").lower() not in bios_filter:
                keep = False
        if keep and cat_maps is not None and not f["isbios"] and not f["isdevice"]:
            if not game_matches_category_filters(
                f["name"], cat_maps,
                genres_include, genres_exclude,
                cats_include, cats_exclude,
            ):
                keep = False
        return keep

    # --- Passe 2 : décider des machines gardées + BIOS requis ---
    kept_names: set = set()
    required_bios: set = set()
    required_devices: set = set()
    total = 0
    for _ev, el in etree.iterparse(str(source), events=("end",), tag="machine", huge_tree=True):
        total += 1
        f = _machine_flags(el)
        bios = _resolve_bios_name(f, bios_names, romof_map)
        if _passes_filters(f, bios):
            kept_names.add(f["name"])
            if bios:
                required_bios.add(bios)
            # device_ref name= attributes
            if include_devices:
                for dr in el.findall("device_ref"):
                    dn = dr.get("name") or ""
                    if dn and dn in machine_names:
                        required_devices.add(dn)
        el.clear()

    auto_included: set = set()
    if auto_bios:
        for b in required_bios:
            if b in machine_names and b not in kept_names:
                kept_names.add(b)
                auto_included.add(b)
    if include_devices:
        for d in required_devices:
            if d not in kept_names:
                kept_names.add(d)
                auto_included.add(d)
    # Forcer les BIOS explicitement listés dans bios_filter
    for b in force_bios_sets:
        if b in machine_names:
            kept_names.add(b)

    # --- Passe 3 : écriture ---
    def _esc_attr(v: str) -> str:
        return (
            str(v)
            .replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    root_tag = "mame"
    attr_s = "".join(f' {k}="{_esc_attr(v)}"' for k, v in root_attrib.items())
    tmp = dest.with_suffix(dest.suffix + ".part")
    kept = 0
    skipped = 0
    with open(tmp, "wb") as out:
        out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write(f"<{root_tag}{attr_s}>\n".encode("utf-8"))
        # Tags utiles au scanner non-merged uniquement (DAT allégé, bien plus rapide)
        _KEEP_CHILD = frozenset({
            "description", "year", "manufacturer", "rom", "disk", "driver",
        })
        for _ev, el in etree.iterparse(str(source), events=("end",), tag="machine", huge_tree=True):
            name = el.get("name") or ""
            if name in kept_names:
                # Retirer chips, device_ref, input, dipswitch, port, sample, display…
                for child in list(el):
                    tag = child.tag.split("}")[-1] if isinstance(child.tag, str) else child.tag
                    if tag not in _KEEP_CHILD:
                        el.remove(child)
                chunk = etree.tostring(el, encoding="utf-8", with_tail=False)
                out.write(chunk)
                out.write(b"\n")
                kept += 1
            else:
                skipped += 1
            el.clear()
        out.write(f"</{root_tag}>\n".encode("utf-8"))
    try:
        if dest.exists():
            dest.unlink()
    except OSError:
        pass
    try:
        tmp.replace(dest)
    except OSError:
        import shutil
        shutil.move(str(tmp), str(dest))

    # Vérification : le fichier doit être re-parseable en mode arcade
    verify_count = 0
    verify_mode = ""
    try:
        vh, _vm, vg, _vs, _vhm = parse_dat(dest)
        verify_count = len(vg)
        verify_mode = vh.get("dat_mode", "")
    except Exception as e:
        raise RuntimeError(
            f"Export écrit ({kept} machines) mais illisible par le scanner : {e}"
        )
    if verify_mode != "arcade":
        raise RuntimeError(
            f"Export rejeté : détecté comme '{verify_mode or 'standard'}' au lieu de arcade. "
            "Le scanner MAME non-merged ne pourra pas l'utiliser."
        )
    if verify_count == 0:
        raise RuntimeError("Export vide : 0 machine utilisable pour le scan non-merged")

    return {
        "ok": True,
        "path": str(dest.resolve()),
        "size": dest.stat().st_size,
        "total": total,
        "kept": kept,
        "skipped": skipped,
        "verify_count": verify_count,
        "dat_mode": verify_mode,
        "merge_mode": "non-merged",
        "auto_bios_included": sorted(auto_included),
        "required_bios": sorted(required_bios),
        "options": opts,
        "note": "non-merged: 1 machine = 1 zip; XML allégé (rom/disk/driver only)",
    }



@app.route("/api/mame/analyze", methods=["POST"])
def api_mame_analyze():
    body = request.get_json(silent=True) or {}
    path = Path((body.get("path") or "").strip())
    try:
        return jsonify(analyze_mame_xml(path))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/mame/export", methods=["POST"])
def api_mame_export():
    body = request.get_json(silent=True) or {}
    source = Path((body.get("source") or body.get("path") or "").strip())
    out_name = (body.get("output") or "").strip()
    opts = body.get("options") or body
    try:
        if not source.is_file():
            return jsonify({"error": f"Source introuvable : {source}"})
        out_dir = DEFAULT_DAT_DIR / "mame"
        out_dir.mkdir(parents=True, exist_ok=True)
        if not out_name:
            # nom auto
            parts = ["MAME"]
            if opts.get("bios_filter"):
                parts.append("+".join(opts["bios_filter"][:3]))
            if opts.get("parents_only"):
                parts.append("parents")
            out_name = "_".join(parts) + "_filtered.xml"
        if not out_name.lower().endswith((".xml", ".dat")):
            out_name += ".xml"
        dest = out_dir / out_name
        result = export_filtered_mame_xml(source, dest, opts)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/mame/frontends")
def api_mame_frontends():
    try:
        force = request.args.get("refresh") in ("1", "true", "yes")
        fmap = build_frontend_mame_map(force=force)
        return jsonify({"ok": True, "map": fmap, "count": len(fmap)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})




@app.route("/api/fbneo/versions")
def api_fbneo_versions():
    """Historique builds FBNeo + correspondances RetroBat / Batocera / Recalbox."""
    try:
        force = request.args.get("refresh") in ("1", "true", "yes")
        limit = 40
        try:
            limit = max(5, min(int(request.args.get("limit") or 40), 80))
        except ValueError:
            pass
        items = fetch_fbneo_versions(limit=limit, force=force)
        return jsonify({
            "ok": True,
            "items": items,
            "count": len(items),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/fbneo/download", methods=["POST"])
def api_fbneo_download():
    """Télécharge les DAT FBNeo pour une version/build (latest par défaut)."""
    body = request.get_json(silent=True) or {}
    version_id = (body.get("version_id") or body.get("tag") or body.get("id") or "latest").strip()
    try:
        result = download_fbneo_dat_version(version_id)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/mame/versions")
def api_mame_versions():
    try:
        items = fetch_mame_versions(80)
        return jsonify({
            "ok": True,
            "items": items,
            "count": len(items),
            "oldrel": "https://www.mamedev.org/oldrel.html",
        })
    except Exception as e:
        traceback.print_exc()
        # Dernier recours : liste minimale
        fallback = []
        for n in range(MAME_LATEST_KNOWN, MAME_LATEST_KNOWN - 10, -1):
            tag = f"mame{n:04d}"
            fallback.append({
                "tag": tag,
                "name": f"MAME 0.{n}",
                "version": f"0.{n}",
                "published": "",
                "lx_url": f"https://github.com/mamedev/mame/releases/download/{tag}/{tag}lx.zip",
                "lx_name": f"{tag}lx.zip",
                "lx_size": 0,
                "bin_url": "",
                "bin_name": "",
            })
        return jsonify({
            "ok": True,
            "items": fallback,
            "count": len(fallback),
            "warning": str(e),
            "oldrel": "https://www.mamedev.org/oldrel.html",
        })


@app.route("/api/mame/generate", methods=["POST"])
def api_mame_generate():
    """
    Génère un listxml :
    - body.tag / body.version → télécharge *lx.zip officiel (export MAME)
    - body.mame_exe → exécute mame -listxml en local
    """
    body = request.get_json(silent=True) or {}
    try:
        mame_exe = (body.get("mame_exe") or "").strip()
        if mame_exe:
            result = generate_mame_listxml_from_exe(Path(mame_exe))
        else:
            tag = (body.get("tag") or body.get("version") or "").strip()
            if not tag:
                return jsonify({"error": "Indiquez tag/version MAME ou chemin mame.exe"})
            result = generate_mame_listxml_from_lx(tag)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})




# ---------- API Profils Collection ----------

@app.route("/api/profiles", methods=["GET"])
def api_profiles_list():
    try:
        return jsonify({"ok": True, "profiles": list_profiles()})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/profiles/<profile_id>", methods=["GET"])
def api_profiles_get(profile_id: str):
    try:
        profile = load_profile(profile_id)
        return jsonify({"ok": True, "profile": profile, "summary": profile_summary(profile)})
    except FileNotFoundError:
        return jsonify({"error": "Profil introuvable"}), 404
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/profiles", methods=["POST"])
def api_profiles_create():
    """Crée un profil. Body: {name, type, root, detect?: true}"""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip() or "Mon RetroBat"
    root = (body.get("root") or "").strip()
    ptype = (body.get("type") or "retrobat").strip().lower()
    do_detect = body.get("detect", True)
    if not root:
        return jsonify({"error": "Racine installation manquante"})
    try:
        systems = []
        es_path = ""
        source = ""
        if do_detect:
            det = detect_systems_for_root(root, ptype)
            systems = det["systems"]
            es_path = det.get("es_systems") or ""
            source = det.get("source") or ""
        profile = {
            "name": name,
            "type": ptype,
            "root": root,
            "es_systems": es_path,
            "detect_source": source,
            "systems": systems,
        }
        saved = save_profile(profile)
        return jsonify({"ok": True, "profile": saved, "summary": profile_summary(saved)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/profiles/<profile_id>", methods=["PUT"])
def api_profiles_update(profile_id: str):
    body = request.get_json(silent=True) or {}
    try:
        profile = load_profile(profile_id)
        for key in ("name", "type", "root", "es_systems", "systems"):
            if key in body:
                profile[key] = body[key]
        saved = save_profile(profile)
        return jsonify({"ok": True, "profile": saved, "summary": profile_summary(saved)})
    except FileNotFoundError:
        return jsonify({"error": "Profil introuvable"}), 404
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/profiles/<profile_id>", methods=["DELETE"])
def api_profiles_delete(profile_id: str):
    try:
        delete_profile(profile_id)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"error": "Profil introuvable"}), 404
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/profiles/detect", methods=["POST"])
def api_profiles_detect():
    body = request.get_json(silent=True) or {}
    root = (body.get("root") or "").strip()
    ptype = (body.get("type") or "retrobat").strip()
    if not root:
        return jsonify({"error": "root manquant"})
    try:
        det = detect_systems_for_root(root, ptype)
        return jsonify({"ok": True, **det})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/profiles/suggest_dat", methods=["POST"])
def api_profiles_suggest_dat():
    body = request.get_json(silent=True) or {}
    sid = (body.get("system") or body.get("id") or "").strip()
    if not sid:
        return jsonify({"error": "system manquant"})
    sug = suggest_dat_for_system(sid)
    return jsonify({"ok": True, "suggestion": sug})




@app.route("/api/profiles/<profile_id>/set_dat", methods=["POST"])
def api_profiles_set_dat(profile_id: str):
    """Associe un DAT à un système du profil. Body: { system_id, dat }"""
    body = request.get_json(silent=True) or {}
    system_id = (body.get("system_id") or body.get("system") or "").strip().lower()
    dat = (body.get("dat") or body.get("path") or "").strip()
    if not system_id:
        return jsonify({"error": "system_id manquant"})
    if not dat:
        return jsonify({"error": "Chemin DAT manquant"})
    dpath = Path(dat)
    if not dpath.is_file():
        return jsonify({"error": f"DAT introuvable : {dat}"})
    try:
        profile = load_profile(profile_id)
    except FileNotFoundError:
        return jsonify({"error": "Profil introuvable"}), 404
    systems = profile.get("systems") or []
    found = False
    for s in systems:
        if (s.get("id") or "").lower() == system_id:
            s["dat"] = str(dpath)
            s["dat_name"] = dpath.name
            s["dat_status"] = "ok"
            s["dat_manual"] = True  # choisi à la main
            # invalider le dernier scan (DAT changé)
            s["last_counts"] = None
            s["last_scan"] = None
            found = True
            break
    if not found:
        return jsonify({"error": f"Système inconnu : {system_id}"})
    profile["systems"] = systems
    saved = save_profile(profile)
    return jsonify({"ok": True, "profile": saved, "summary": profile_summary(saved), "system_id": system_id})


@app.route("/api/profiles/<profile_id>/reset_dats", methods=["POST"])
def api_profiles_reset_dats(profile_id: str):
    """
    Réassigne les DAT par défaut (suggestions automatiques) pour tous les systèmes.
    Écrase les choix manuels.
    """
    try:
        profile = load_profile(profile_id)
    except FileNotFoundError:
        return jsonify({"error": "Profil introuvable"}), 404
    systems = profile.get("systems") or []
    changed = 0
    cleared = 0
    for s in systems:
        sid = s.get("id") or ""
        old = s.get("dat") or ""
        sug = suggest_dat_for_system(sid)
        if sug.get("path"):
            s["dat"] = sug["path"]
            s["dat_name"] = sug.get("name") or Path(sug["path"]).name
            s["dat_status"] = "ok"
            s["dat_manual"] = False
            if s["dat"] != old:
                changed += 1
                s["last_counts"] = None
                s["last_scan"] = None
        else:
            preset = SYSTEM_DAT_PRESETS.get(sid.lower())
            if preset is not None and not (preset.get("patterns")):
                s["dat"] = ""
                s["dat_name"] = ""
                s["dat_status"] = "skip"
                s["enabled"] = False
                s["dat_manual"] = False
            else:
                s["dat"] = ""
                s["dat_name"] = ""
                s["dat_status"] = "missing"
                s["dat_manual"] = False
                if old:
                    cleared += 1
                    s["last_counts"] = None
                    s["last_scan"] = None
    profile["systems"] = systems
    saved = save_profile(profile)
    return jsonify({
        "ok": True,
        "profile": saved,
        "summary": profile_summary(saved),
        "changed": changed,
        "cleared": cleared,
    })


@app.route("/api/profiles/<profile_id>/scan_system", methods=["POST"])
@_exclusive()
def api_profiles_scan_system(profile_id: str):
    """
    Charge le DAT du système, scanne son dossier, met à jour last_counts dans le profil.
    Body: { system_id, max_depth? }
    Réutilise le moteur de scan existant (STATE).
    """
    body = request.get_json(silent=True) or {}
    system_id = (body.get("system_id") or body.get("system") or "").strip().lower()
    try:
        max_depth = int(body.get("max_depth") or 0)
    except (TypeError, ValueError):
        max_depth = 0
    max_depth = max(0, min(max_depth, 2))

    try:
        profile = load_profile(profile_id)
    except FileNotFoundError:
        return jsonify({"error": "Profil introuvable"}), 404

    systems = profile.get("systems") or []
    target = None
    target_idx = -1
    for i, s in enumerate(systems):
        if (s.get("id") or "").lower() == system_id:
            target = s
            target_idx = i
            break
    if not target:
        return jsonify({"error": f"Système inconnu : {system_id}"})

    dat_path = (target.get("dat") or "").strip()
    roms_path = (target.get("path") or "").strip()
    if not dat_path:
        return jsonify({"error": f"Aucun DAT associé à {system_id}"})
    if not roms_path:
        return jsonify({"error": f"Aucun dossier ROMs pour {system_id}"})
    dpath = Path(dat_path)
    rpath = Path(roms_path)
    if not dpath.is_file():
        return jsonify({"error": f"DAT introuvable : {dat_path}"})
    if not rpath.is_dir():
        return jsonify({"error": f"Dossier ROMs introuvable : {roms_path}"})

    try:
        # Anciens résultats invalidés d'emblée (même si le scan échoue ensuite)
        STATE["results"] = []
        _new_scan_id()
        header, rom_map, games_list, size_set, hash_mode = parse_dat(dpath)
        STATE["header"] = header
        STATE["rom_map"] = rom_map
        STATE["games_list"] = games_list
        STATE["size_set"] = size_set
        STATE["hash_mode"] = hash_mode
        STATE["dat_mode"] = header.get("dat_mode", "standard")
        STATE["dat_path"] = str(dpath)
        results = scan_roms(
            rpath, rom_map, games_list, size_set or set(),
            hash_mode or "crc", header.get("dat_mode", "standard"),
            max_depth=max_depth,
            deep=bool(body.get("deep")),
            quick_id=body.get("quick_id", True) is not False,
        )
        STATE["results"] = results
        STATE["roms_path"] = str(rpath)
        counts = _counts(results)
        systems[target_idx]["last_counts"] = counts
        systems[target_idx]["last_scan"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        profile["systems"] = systems
        save_profile(profile)

        public = _public_results(results)
        return jsonify({
            "ok": True,
            "scan_id": _new_scan_id(),
            "system_id": system_id,
            "results": public,
            "counts": counts,
            "dat_mode": header.get("dat_mode", "standard"),
            "hash_mode": hash_mode,
            "dat_path": str(dpath),
            "roms_path": str(rpath),
            "dat_name": header.get("name") or dpath.name,
            "profile": profile,
            "summary": profile_summary(profile),
        })
    except ScanCancelled as e:
        STATE["results"] = []  # le DAT du système est chargé : les anciens résultats ne lui correspondent plus
        _new_scan_id()
        return jsonify({"error": str(e), "cancelled": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})



@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Arrêt propre du serveur (bouton Quitter) + fenêtre application."""
    def _stop():
        time.sleep(0.35)
        _close_ui_window()
        os._exit(0)
    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"ok": True, "message": "Arrêt…"})


# ---------------------------------------------------------------------------
# Fenêtre application (mode --app= Chromium, comme Gamelist Media Editor)
# ---------------------------------------------------------------------------

_UI_PROC = None


def _chromium_candidates():
    """Chemins connus Edge / Chrome / Brave (Windows + Linux)."""
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pfx86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    local = os.environ.get("LOCALAPPDATA") or ""
    return [
        os.path.join(pfx86, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(local, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pf, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.join(local, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
        "/usr/bin/brave-browser",
        "/usr/bin/brave",
    ]


def _is_chromium_exe(path):
    name = os.path.basename(path or "").lower()
    return name in {
        "msedge.exe", "chrome.exe", "brave.exe", "chromium.exe",
        "google-chrome", "google-chrome-stable", "chromium",
        "chromium-browser", "microsoft-edge", "brave-browser", "brave",
    }


def _windows_http_progid():
    """Handler HTTP par défaut (ChromeHTML, MSEdgeHTM, FirefoxURL…)."""
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
        ) as key:
            progid, _ = winreg.QueryValueEx(key, "ProgId")
        return (progid or "").strip()
    except Exception:
        return ""


def _exe_from_progid(progid):
    if not progid or os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{progid}\shell\open\command") as key:
            cmd, _ = winreg.QueryValueEx(key, None)
        cmd = (cmd or "").strip()
        if cmd.startswith('"'):
            end = cmd.find('"', 1)
            if end > 1:
                return cmd[1:end]
        return cmd.split()[0] if cmd else None
    except Exception:
        return None


def _find_app_mode_browser():
    """
    Navigateur par défaut s'il est Chrome / Edge / Brave → --app=
    Sinon Edge, puis Chrome, puis Brave.
    Sinon None (Firefox seul → navigateur normal).
    """
    progid = _windows_http_progid().lower()
    default_exe = _exe_from_progid(_windows_http_progid())
    if default_exe and os.path.isfile(default_exe) and _is_chromium_exe(default_exe):
        return default_exe
    if "chrome" in progid or "edge" in progid or "brave" in progid:
        for path in _chromium_candidates():
            if not path or not os.path.isfile(path):
                continue
            base = os.path.basename(path).lower()
            if "chrome" in progid and "chrome" in base:
                return path
            if "edge" in progid and "msedge" in base:
                return path
            if "brave" in progid and "brave" in base:
                return path
    for path in _chromium_candidates():
        if path and os.path.isfile(path):
            return path
    return None


def _close_ui_window():
    """Ferme la fenêtre --app= si on l'a lancée."""
    global _UI_PROC
    proc = _UI_PROC
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
    except Exception:
        pass
    _UI_PROC = None


def _watch_ui_process():
    """Fermer la fenêtre application → arrêter le serveur."""
    time.sleep(3)
    proc = _UI_PROC
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.wait()
    except Exception:
        return
    os._exit(0)


def _open_browser(url):
    """
    Fenêtre Chromium dédiée (pas d'onglets / barre d'adresse) si possible.
    Profil isolé dans app-window/ à côté de l'app.
    Fallback : navigateur système (barres visibles).
    """
    global _UI_PROC
    exe = _find_app_mode_browser()
    if exe:
        profile = str(SCRIPT_DIR / "app-window")
        try:
            os.makedirs(profile, exist_ok=True)
        except OSError:
            profile = None
        cmd = [
            exe,
            f"--app={url}",
            "--new-window",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--window-size=1400,900",
        ]
        if profile:
            cmd.append(f"--user-data-dir={profile}")
        try:
            _UI_PROC = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            threading.Thread(target=_watch_ui_process, daemon=True).start()
            print(f"  Fenêtre application : {exe}")
            return
        except Exception as e:
            print(f"  [!] Fenêtre --app= impossible ({e}) — navigateur normal")
    try:
        import webbrowser
        webbrowser.open(url)
        print("  Navigateur standard (barres visibles)")
    except Exception:
        pass


def main():
    import argparse

    if getattr(sys, "frozen", False):
        try:
            import multiprocessing
            multiprocessing.freeze_support()
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="RomSet Verifier")
    parser.add_argument("--open", action="store_true", help="Ouvre la fenêtre application au démarrage")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-app", action="store_true", help="Forcer le navigateur normal (avec barres)")
    args = parser.parse_args()
    # Double-clic sur l'exe → ouvrir la fenêtre automatiquement
    if getattr(sys, "frozen", False):
        args.open = True

    ensure_folders()
    url = f"http://127.0.0.1:{args.port}/"
    print("=" * 50)
    print(f"  {APP_NAME} {APP_VERSION}")
    print(f"  {url}")
    print("=" * 50)
    print(f"  dat/  → {DEFAULT_DAT_DIR}")
    print(f"  roms/ → {DEFAULT_ROMS_DIR}")
    print("  Fermer la fenêtre, Ctrl+C ou ⏻ Quitter pour arrêter.")
    print()

    if args.open:
        def _open():
            time.sleep(1.0)
            if args.no_app:
                try:
                    import webbrowser
                    webbrowser.open(url)
                except Exception:
                    pass
            else:
                _open_browser(url)
        threading.Thread(target=_open, daemon=True).start()

    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
