#!/usr/bin/env python3
"""
RomSet Verifier — vérification de romset à partir d'un DAT/XML
Interface locale moderne (liste + détail), support loose + ZIP.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sys
import subprocess
import traceback
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_local_site = Path.home() / ".local" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
if _local_site.exists():
    sys.path.insert(0, str(_local_site))

from flask import Flask, jsonify, render_template_string, request
from lxml import etree

SCRIPT_DIR = Path(__file__).resolve().parent

APP_VERSION = "1.0.0-beta"
APP_NAME = "RomSet Verifier"
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

UA = "RomSetVerifier/1.3 (+https://github.com/local)"

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


def _sha1_path(filepath: Path) -> tuple:
    with open(filepath, "rb", buffering=_READ_BUF) as f:
        return _sha1_fileobj(f)



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
    with open(filepath, "rb", buffering=_READ_BUF) as f:
        return _crc32_fileobj(f)


def compute_hashes_stream(fileobj) -> Dict[str, Any]:
    """API compat : CRC32 + taille."""
    crc, size = _crc32_fileobj(fileobj)
    return {"size": size, "crc": f"{crc:08x}", "md5": "", "sha1": ""}


def compute_hashes(filepath: Path) -> Dict[str, Any]:
    crc, size = _crc32_path(filepath)
    return {"size": size, "crc": f"{crc:08x}", "md5": "", "sha1": ""}


def _probe_dat_kind(dat_path: Path) -> str:
    """
    Identifie le type de DAT sans tout charger.
    Retourne: softwarelist | arcade | standard
    - softwarelist : racine <softwarelist>
    - arcade : listxml MAME (<mame>) ou machines avec ROMs CRC (set arcade)
    - standard : No-Intro / Redump / MAMERedump (disks SHA1, roms CRC, etc.)
    """
    saw_softwarelist = False
    saw_mame_root = False
    saw_datafile = False
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
            if tag == "datafile":
                saw_datafile = True
            elif tag == "machine":
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


def match_rom(hashes: Dict, rom_map: Dict, hash_mode: str = "crc") -> Tuple[Optional[Dict], Optional[str]]:
    if hash_mode == "sha1":
        sha1 = (hashes.get("sha1") or "").lower()
        if sha1 and ("sha1", sha1) in rom_map:
            return rom_map[("sha1", sha1)], "sha1"
        return None, None
    crc_i = hashes.get("crc_int")
    if crc_i is None:
        try:
            crc_i = int(hashes["crc"], 16)
        except (KeyError, ValueError, TypeError):
            return None, None
    key = ("crc", crc_i, hashes["size"])
    if key in rom_map:
        return rom_map[key], "crc"
    # fallback sha1 si présent
    sha1 = (hashes.get("sha1") or "").lower()
    if sha1 and ("sha1", sha1) in rom_map:
        return rom_map[("sha1", sha1)], "sha1"
    return None, None


def _err_row(found, path, msg, is_zip=False, member=""):
    return {
        "status": "error", "game": "", "expected": "", "found": found, "path": path,
        "size": 0, "crc": "", "md5": "", "sha1": "", "is_zip": is_zip,
        "zip_member": member, "message": msg,
    }

def _unique_paths(paths) -> List[Path]:
    """Déduplique les chemins (critique sur Windows, FS insensible à la casse)."""
    seen = set()
    out: List[Path] = []
    for p in paths:
        try:
            key = str(p.resolve()).lower()
        except Exception:
            key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


# Extensions reconnues (loose)
_LOOSE_EXT = frozenset({
    ".bin", ".a78", ".rom", ".nes", ".sfc", ".smc", ".gb", ".gbc", ".gba",
    ".n64", ".z64", ".v64", ".nds", ".3ds", ".cia", ".psp", ".pce", ".sgx",
    ".ngp", ".ngc", ".ws", ".wsc", ".col", ".gg", ".sms", ".md", ".gen",
    ".smd", ".32x", ".j64", ".jag", ".lnx", ".a26", ".a52", ".int", ".vec",
    ".iso", ".img", ".chd", ".cue", ".nrg", ".mdf", ".cdi", ".gcm",
    ".wbfs", ".rvz", ".wia", ".cso", ".iso", ".toast",
})


def _hash_loose_task(args: Tuple) -> Dict:
    """Worker : hash un fichier loose selon hash_mode."""
    path_str, size_set, hash_mode = args
    fpath = Path(path_str)
    try:
        size = fpath.stat().st_size
    except OSError as e:
        return {
            "path": path_str, "found": fpath.name, "is_zip": False,
            "zip_member": "", "hashes": None, "error": str(e),
        }

    # CHD : SHA1 header instantané (MAME / MAMERedump)
    if fpath.suffix.lower() == ".chd":
        sha1 = read_chd_sha1(fpath)
        if sha1:
            return {
                "path": path_str, "found": fpath.name, "is_zip": False,
                "zip_member": "", "hashes": {
                    "size": size, "crc": "", "crc_int": -1,
                    "md5": "", "sha1": sha1,
                }, "error": None,
            }
        # fallback hash fichier si header illisible
        if hash_mode == "sha1":
            try:
                sha1, size2 = _sha1_path(fpath)
                return {
                    "path": path_str, "found": fpath.name, "is_zip": False,
                    "zip_member": "", "hashes": {
                        "size": size2, "crc": "", "crc_int": -1,
                        "md5": "", "sha1": sha1,
                    }, "error": None,
                }
            except Exception as e:
                return {
                    "path": path_str, "found": fpath.name, "is_zip": False,
                    "zip_member": "", "hashes": None, "error": str(e),
                }

    if hash_mode == "sha1":
        try:
            sha1, size2 = _sha1_path(fpath)
            return {
                "path": path_str, "found": fpath.name, "is_zip": False,
                "zip_member": "", "hashes": {
                    "size": size2, "crc": "", "crc_int": -1,
                    "md5": "", "sha1": sha1,
                }, "error": None,
            }
        except Exception as e:
            return {
                "path": path_str, "found": fpath.name, "is_zip": False,
                "zip_member": "", "hashes": None, "error": str(e),
            }

    # Mode CRC : filtre taille
    if size_set and size not in size_set:
        return {
            "path": path_str, "found": fpath.name, "is_zip": False,
            "zip_member": "", "hashes": {
                "size": size, "crc": "", "crc_int": -1, "md5": "", "sha1": "",
            }, "error": None, "size_mismatch": True,
        }
    try:
        crc, size2 = _crc32_path(fpath)
        return {
            "path": path_str, "found": fpath.name, "is_zip": False,
            "zip_member": "", "hashes": {
                "size": size2, "crc": f"{crc:08x}", "crc_int": crc, "md5": "", "sha1": "",
            }, "error": None,
        }
    except Exception as e:
        return {
            "path": path_str, "found": fpath.name, "is_zip": False,
            "zip_member": "", "hashes": None, "error": str(e),
        }


def _hash_zip_task(args: Tuple) -> List[Dict]:
    """Worker : ZIP — CRC ou SHA1 selon mode."""
    path_str, size_set, hash_mode = args
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
                # CRC catalogue central — pas de décompression
                crc = info.CRC & 0xFFFFFFFF
                out.append({
                    "path": path_str, "found": label, "is_zip": True,
                    "zip_member": info.filename, "hashes": {
                        "size": info.file_size, "crc": f"{crc:08x}", "crc_int": crc,
                        "md5": "", "sha1": "",
                    }, "error": None,
                })
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


def _collect_files(
    rom_dir: Path,
    size_set: set,
    hash_mode: str = "crc",
    max_depth: int = 0,
    skip_zips: bool = False,
) -> List[Dict]:
    loose: List[str] = []
    zips: List[str] = []
    seen = set()
    # .chd toujours inclus
    loose_ext = set(_LOOSE_EXT) | {".chd"}

    for fpath in _iter_rom_paths(rom_dir, max_depth=max_depth):
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
        tasks = [(p, size_set, hash_mode) for p in loose]
        # chunksize adaptatif : petits lots si peu de fichiers
        chunk = 8 if len(loose) > 64 else max(1, len(loose) // max(workers, 1) or 1)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for result in ex.map(_hash_loose_task, tasks, chunksize=chunk):
                inventory.append(result)

    if zips:
        tasks = [(p, size_set, hash_mode) for p in zips]
        chunk = 4 if len(zips) > 32 else 1
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for batch in ex.map(_hash_zip_task, tasks, chunksize=chunk):
                inventory.extend(batch)

    return inventory



def _zip_catalog(zpath: Path) -> Dict[str, Any]:
    """
    Lecture unique du catalogue central d'un ZIP (sans décompresser).
    Retourne path, name, crc_idx, name_idx, members, error.
    """
    path_str = str(zpath)
    out: Dict[str, Any] = {
        "path": path_str,
        "name": Path(path_str).name,
        "crc_idx": {},
        "name_idx": {},
        "members": [],
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


def _zip_catalog_task(path_str: str) -> Dict[str, Any]:
    return _zip_catalog(Path(path_str))


def _zip_crc_index(zpath: Path) -> Dict[int, List[Tuple[str, int]]]:
    """Compat : index CRC seul."""
    return _zip_catalog(zpath).get("crc_idx") or {}



def _scan_one_mame_machine(args: Tuple) -> Dict:
    """Vérifie un machine.zip en mode non-merged (jamais de ROMs lues depuis le parent).
    Produit aussi `components[]` : statut de chaque ROM/CHD attendu.
    args: (machine, zpath_str|None, catalog|None) — catalog évite une 2e ouverture.
    """
    if len(args) >= 3:
        machine, zpath_str, catalog = args[0], args[1], args[2]
    else:
        machine, zpath_str = args[0], args[1]
        catalog = None
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

    if not zpath_str:
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
            "expected": name + (".zip" if required or not disks else ""),
            "message": "CHD/zip absent" if (not required and disks) else "ZIP absent",
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

    for r in roms:
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

        hits = crc_idx.get(crc_i) or []
        hit = None
        rname_l = rname.lower()

        def _pick(prefer_unused=True):
            # 1) nom exact + taille
            for fn, sz in hits:
                if prefer_unused and fn in matched_members:
                    continue
                if Path(fn).name.lower() == rname_l and (not size_e or sz == size_e):
                    return (fn, sz)
            # 2) nom exact
            for fn, sz in hits:
                if prefer_unused and fn in matched_members:
                    continue
                if Path(fn).name.lower() == rname_l:
                    return (fn, sz)
            # 3) taille
            if size_e:
                for fn, sz in hits:
                    if prefer_unused and fn in matched_members:
                        continue
                    if sz == size_e:
                        return (fn, sz)
            # 4) premier libre
            for fn, sz in hits:
                if prefer_unused and fn in matched_members:
                    continue
                return (fn, sz)
            return None

        hit = _pick(True)
        shared = False  # entrée DAT en double (même fichier, même CRC) — fréquent FBNeo

        if hit is None:
            # CRC présent mais membre déjà consommé : partage OK si nom+CRC correspondent
            shared_hit = _pick(False)
            if shared_hit is not None:
                sfn, ssz = shared_hit
                if Path(sfn).name.lower() == rname_l:
                    hit = shared_hit
                    shared = True

        if hit is None:
            # CRC absent du ZIP : nom présent avec un autre CRC ?
            by_name = name_idx.get(rname_l)
            if by_name:
                fn, sz, found_crc = by_name
                # Même CRC que demandé + déjà matché → doublon DAT, pas un bad_crc
                if found_crc == crc_i:
                    hit = (fn, sz)
                    shared = True
                else:
                    # Vrai mauvais CRC seulement si ce membre n'a pas déjà été
                    # validé pour UNE autre entrée qui attendait CE crc... 
                    # Sinon c'est bien un conflit de contenu sous ce nom.
                    components.append(_comp(
                        "rom", rname, "bad_crc",
                        found=fn, crc_expected=crc_s,
                        crc_found=f"{found_crc:08x}",
                        size_expected=size_e, size_found=sz,
                        message="CRC incorrect",
                    ))
                    bad_roms.append(rname)
                    continue
            else:
                components.append(_comp(
                    "rom", rname, "missing",
                    crc_expected=crc_s, size_expected=size_e,
                    message="absente du ZIP",
                ))
                missing_roms.append(rname)
                continue

        fn, sz = hit
        if not shared:
            matched_members.add(fn)
        found_count += 1
        base_fn = Path(fn).name
        if base_fn.lower() != rname_l and not shared:
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

    # CHD
    chd_missing = []
    for d in disks:
        dname = d.get("name") or ""
        sha_e = (d.get("sha1") or "").lower()
        dstatus = (d.get("status") or "good").lower()
        optional = bool(d.get("optional"))
        if dstatus == "nodump" or not sha_e:
            components.append(_comp(
                "chd", _chd_filename(dname), "nodump" if dstatus == "nodump" else "optional",
                sha1_expected=sha_e, message="nodump" if dstatus == "nodump" else "sans SHA1",
            ))
            continue
        chd_fn = _chd_filename(dname)
        candidates = [
            zpath.parent / chd_fn,
            zpath.parent / name / chd_fn,
            zpath.parent / _chd_filename(name),
        ]
        found_chd = next((c for c in candidates if c.is_file()), None)
        if not found_chd:
            components.append(_comp(
                "chd", _chd_filename(dname), "missing",
                sha1_expected=sha_e, message="CHD absent",
            ))
            chd_missing.append(dname)
            continue
        sha = read_chd_sha1(found_chd)
        if not sha:
            components.append(_comp(
                "chd", _chd_filename(dname), "bad_crc",
                found=str(found_chd), sha1_expected=sha_e,
                message="header CHD illisible",
            ))
            bad_roms.append(f"{_chd_filename(dname)} (header)")
        elif sha != sha_e:
            components.append(_comp(
                "chd", _chd_filename(dname), "bad_crc",
                found=str(found_chd), sha1_expected=sha_e, sha1_found=sha,
                message="SHA1 incorrect",
            ))
            bad_roms.append(f"{_chd_filename(dname)} (SHA1)")
        else:
            components.append(_comp(
                "chd", _chd_filename(dname), "ok",
                found=str(found_chd), sha1_expected=sha_e, sha1_found=sha,
                message="OK",
            ))

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
        "crc": f"{found_count}/{len(required)}" if required else "",
        "components": components,
    }



def build_rom_pool(rom_dir: Path, max_depth: int = 0) -> Dict[int, List[Dict[str, Any]]]:
    """
    Index global CRC32 → [{zip, member, size}] pour tous les ZIP du dossier.
    Sert à localiser une ROM manquante présente dans un autre set.
    """
    pool: Dict[int, List[Dict[str, Any]]] = {}
    for fpath in _iter_rom_paths(rom_dir, max_depth=max_depth):
        if fpath.suffix.lower() != ".zip":
            continue
        try:
            with zipfile.ZipFile(str(fpath), "r") as zf:
                for info in zf.infolist():
                    if info.is_dir() or info.file_size <= 0:
                        continue
                    crc = info.CRC & 0xFFFFFFFF
                    pool.setdefault(crc, []).append({
                        "zip": str(fpath),
                        "zip_name": fpath.name,
                        "member": info.filename,
                        "size": info.file_size,
                    })
        except Exception:
            continue
    return pool


def _annotate_available_roms(results: List[Dict], pool: Dict[int, List[Dict[str, Any]]]) -> None:
    """Marque les composantes missing dont le CRC existe dans un autre ZIP."""
    for row in results:
        own_zip = (row.get("path") or "").replace("\\", "/")
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


def scan_mame_nonmerged(rom_dir: Path, machines: List[Dict], max_depth: int = 0) -> List[Dict]:
    """Scan romset MAME non-merged : 1 ZIP par machine, CRC via catalogue ZIP.
    max_depth>0 autorise les zip dans des sous-dossiers (rare, ex. classement A-Z).
    Post-passe : ROMs manquantes trouvées dans d'autres ZIP → status 'available'.
    """
    # Index des zip (stem lower → path) — priorité à la racine (profondeur plus faible)
    zips: Dict[str, str] = {}
    zips_depth: Dict[str, int] = {}
    for fpath in _iter_rom_paths(rom_dir, max_depth=max_depth):
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
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for cat in ex.map(_zip_catalog_task, zip_paths, chunksize=4):
                catalog_by_path[cat["path"]] = cat

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
        tasks.append((m, zpath_str, cat))

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
            # Score de recouvrement CRC : exact (1.0) prioritaire, sinon meilleur ≥ 80 %
            best_path = None
            best_score = 0.0
            tied = False
            seen_p = set()
            for c in req:
                for fp, path in crc_to_orphans.get(c, []):
                    if path in seen_p or path.lower() in used_zip_paths:
                        continue
                    seen_p.add(path)
                    inter = len(need & fp)
                    if inter == 0:
                        continue
                    score = inter / float(len(need))
                    # bonus si empreinte exacte
                    if fp == need:
                        score = 1.0
                    if score > best_score + 1e-9:
                        best_score = score
                        best_path = path
                        tied = False
                    elif abs(score - best_score) < 1e-9 and path != best_path:
                        tied = True
            # Exiger un match unique et suffisamment complet
            if tied or not best_path or best_score < 0.8:
                continue
            zpath_str = best_path
            cat = catalog_by_path.get(zpath_str)
            new_row = _scan_one_mame_machine((machine, zpath_str, cat))
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


def scan_roms(
    rom_dir: Path,
    rom_map: Dict,
    games_list: List[Dict],
    size_set: Optional[set] = None,
    hash_mode: str = "crc",
    dat_mode: str = "standard",
    max_depth: int = 0,
) -> List[Dict]:
    max_depth = max(0, min(int(max_depth or 0), 2))
    if dat_mode == "arcade":
        return scan_mame_nonmerged(rom_dir, games_list, max_depth=max_depth)
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
        rom_dir, size_set, hash_mode, max_depth=max_depth, skip_zips=skip_zips,
    )

    by_crc: Dict[Tuple, Dict] = {}
    by_crc_only: Dict[int, Dict] = {}  # fallback si taille DAT ≠ fichier
    by_sha1: Dict[str, Dict] = {}
    used_ids: set = set()

    for it in inventory:
        if it.get("error") or not it.get("hashes"):
            continue
        if it.get("size_mismatch"):
            continue
        h = it["hashes"]
        crc_i = h.get("crc_int", -1)
        if crc_i is not None and crc_i >= 0:
            by_crc.setdefault((crc_i, h["size"]), it)
            # premier fichier pour ce CRC (évite d'écraser un match déjà optimal)
            by_crc_only.setdefault(crc_i, it)
        sha1 = (h.get("sha1") or "").lower()
        if sha1:
            by_sha1.setdefault(sha1, it)

    def _find_file(g: Dict) -> Optional[Dict]:
        # CHD / mode SHA1 pur
        if g.get("is_disk") or (hash_mode == "sha1" and (g.get("crc_int") is None or int(g.get("crc_int") or -1) < 0)):
            sha1 = (g.get("sha1") or "").lower()
            if sha1 and sha1 in by_sha1:
                return by_sha1[sha1]
            if g.get("is_disk"):
                return None
        # CRC (+ taille), puis CRC seul, puis SHA1
        crc_i = g.get("crc_int")
        if crc_i is None:
            try:
                crc_i = int(g["crc"], 16) if g.get("crc") else -1
            except ValueError:
                crc_i = -1
        if crc_i is not None and crc_i >= 0:
            hit = by_crc.get((crc_i, g.get("size") or 0))
            if hit:
                return hit
            hit = by_crc_only.get(crc_i)
            if hit:
                return hit
        sha1 = (g.get("sha1") or "").lower()
        if sha1 and sha1 in by_sha1:
            return by_sha1[sha1]
        return None

    for g in games_list:
        it = _find_file(g)
        if it is None:
            results.append({
                "status": "missing", "game": g["game"], "expected": g["rom_name"],
                "found": "", "path": "", "size": g["size"], "crc": g.get("crc", ""),
                "md5": g.get("md5", ""), "sha1": g.get("sha1", ""), "is_zip": False,
                "zip_member": "", "message": "",
                "description": g.get("description") or "",
                "cloneof": g.get("cloneof") or "",
            })
            continue

        used_ids.add(id(it))
        h = it["hashes"]
        if it["is_zip"]:
            expected_zip = g["game"] + ".zip"
            zname = Path(it["path"]).name
            member = it.get("zip_member") or ""
            # softlist : membre = dump_name MAME (ex. c1001_cubeup.bin)
            member_ok = (
                _names_match(member, g["rom_name"])
                or _names_match(member, g.get("dump_name") or "")
            )
            name_ok = _names_match(zname, expected_zip) and member_ok
        else:
            found_name = Path(it["found"]).name
            name_ok = (
                _names_match(found_name, g["rom_name"])
                or _names_match(found_name, g.get("dump_name") or "")
            )

        status = "good" if name_ok else "rename"
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
            "message": "",
            "description": g.get("description") or "",
            "cloneof": g.get("cloneof") or "",
        })

    for it in inventory:
        if id(it) in used_ids:
            continue
        if it.get("error"):
            results.append(_err_row(
                it["found"], it["path"], it["error"],
                is_zip=it["is_zip"], member=it.get("zip_member") or "",
            ))
            continue
        h = it.get("hashes") or {}
        if it.get("size_mismatch"):
            results.append({
                "status": "bad", "game": "", "expected": "", "found": it["found"],
                "path": it["path"], "size": h.get("size", 0), "crc": "",
                "md5": "", "sha1": "", "is_zip": it["is_zip"],
                "zip_member": it.get("zip_member") or "",
                "message": "Taille hors DAT (CRC non calcule)",
            })
            continue
        sha1 = (h.get("sha1") or "").lower()
        crc_i = h.get("crc_int", -1)
        if sha1 and sha1 in by_sha1 and id(by_sha1[sha1]) != id(it):
            msg = "Doublon (meme contenu)"
        elif crc_i >= 0 and (crc_i, h.get("size", 0)) in by_crc and id(by_crc[(crc_i, h.get("size", 0))]) != id(it):
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


def _rename_zip_member(zip_path: Path, old_member: str, new_member: str) -> None:
    """Renomme un membre dans un ZIP (réécriture atomique via fichier temporaire)."""
    _batch_rename_zip_members(zip_path, {old_member: new_member})


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
    entries: List[Tuple[str, bytes, Any]] = []  # new_name, data, src_info

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

        for item in zin.infolist():
            if item.is_dir() or item.file_size <= 0:
                continue
            data = zin.read(item.filename)
            filename = resolved.get(item.filename, item.filename)
            if filename != item.filename:
                applied.append(f"{item.filename} → {filename}")
            entries.append((filename, data, item))

    # Source fermée — écriture + remplacement
    tmp = zip_path.with_name(zip_path.name + ".rsv_tmp")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    try:
        with zipfile.ZipFile(str(tmp), "w") as zout:
            for filename, data, item in entries:
                info = zipfile.ZipInfo(filename=filename)
                info.compress_type = item.compress_type
                info.external_attr = item.external_attr
                info.date_time = item.date_time
                try:
                    info.create_system = item.create_system
                except Exception:
                    pass
                zout.writestr(info, data)
        try:
            os.replace(str(tmp), str(zip_path))
        except OSError:
            try:
                if zip_path.exists():
                    zip_path.unlink()
            except OSError as e:
                raise RuntimeError(f"Impossible de remplacer le ZIP (verrouillé ?) : {e}")
            os.replace(str(tmp), str(zip_path))
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
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
    if game and path_s:
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
    zip_name_wrong = False
    if row.get("is_zip") and game:
        zip_name_wrong = src.name.lower() != (game + ".zip").lower()
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
                zip_target_name = game + ".zip"
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

        row["path"] = str(dst)
        row["found"] = dst.name
        row["expected"] = expected or target_name
        row["status"] = "good"
        return True, f"{src.name} → {dst.name}"
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
        if r.get("optional") and crc_i < 0:
            continue
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
                            data = ez.read(c["source_member"])
                        zout.writestr(expected, data)
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
                try:
                    src.unlink()
                except OSError as e:
                    return False, f"Impossible d'écraser {src.name}: {e}"
            os.replace(str(tmp), str(src))
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
            member_bytes: Dict[str, bytes] = {}
            all_reals: List[str] = []

            with zipfile.ZipFile(str(src), "r") as zin:
                for info in zin.infolist():
                    if info.is_dir() or info.file_size <= 0:
                        continue
                    crc = info.CRC & 0xFFFFFFFF
                    by_crc.setdefault(crc, []).append((info.filename, info.file_size))
                    by_base[Path(info.filename).name.lower()] = info.filename
                    all_reals.append(info.filename)

                # plan: expected_basename → source key in member_bytes
                plan: Dict[str, str] = {}

                # 1) Match DAT CRC → fichier local
                #    Priorité : nom exact, puis membre pas encore utilisé (CRC dupliqués MAME)
                used_sources: set = set()
                for r in roms_req:
                    expected = Path(r.get("name") or "").name
                    if not expected or expected in plan:
                        continue
                    crc_i = int(r["crc_int"])
                    cands = by_crc.get(crc_i) or []
                    size_e = int(r.get("size") or 0)
                    exp_l = expected.lower()
                    hit = None
                    # a) nom exact non utilisé
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
                    # b) taille, non utilisé
                    if hit is None and size_e:
                        for real, sz in cands:
                            if real in used_sources:
                                continue
                            if sz == size_e:
                                hit = real
                                break
                    # c) premier non utilisé
                    if hit is None:
                        for real, sz in cands:
                            if real in used_sources:
                                continue
                            hit = real
                            break
                    # d) contenu dupliqué : réutiliser un source déjà lu (même CRC)
                    if hit is None and cands:
                        hit = cands[0][0]
                    if hit is not None:
                        plan[expected] = hit
                        used_sources.add(hit)
                        if hit not in member_bytes:
                            member_bytes[hit] = zin.read(hit)

                # 1b) ROMs encore absentes du plan mais CRC présent dans le ZIP
                #     → dupliquer le contenu (2 noms DAT, 1 CRC = 2 fichiers non-merged)
                for r in roms_req:
                    expected = Path(r.get("name") or "").name
                    if not expected or expected in plan:
                        continue
                    crc_i = int(r["crc_int"])
                    cands = by_crc.get(crc_i) or []
                    if not cands:
                        continue
                    # préférer un candidat déjà chargé
                    hit = None
                    for real, sz in cands:
                        if real in member_bytes:
                            hit = real
                            break
                    if hit is None:
                        hit = cands[0][0]
                    plan[expected] = hit
                    if hit not in member_bytes:
                        try:
                            member_bytes[hit] = zin.read(hit)
                        except Exception as e:
                            notes.append(f"Lecture dup {hit}: {e}")
                            plan.pop(expected, None)
                            continue
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
                                    member_bytes[key] = ez.read(c["source_member"])
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
                    if real not in member_bytes:
                        try:
                            member_bytes[real] = zin.read(real)
                        except Exception as e:
                            notes.append(f"Lecture {real}: {e}")
                            plan.pop(expected, None)

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
                    if real not in member_bytes:
                        try:
                            member_bytes[real] = zin.read(real)
                        except Exception as e:
                            notes.append(f"Lecture {real}: {e}")
                            plan.pop(expected, None)

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
            if len(all_reals) != len(used_reals):
                needs_rewrite = True
            # Toujours réécrire s'il reste des rename dans les components
            if any((c.get("status") or "") == "rename" for c in (row.get("components") or [])):
                needs_rewrite = True
            # Toujours réécrire si plan a plus d'entrées que de sources uniques (copies CRC)
            if len(plan) > len(used_reals):
                needs_rewrite = True

            _repair_log(f"  needs_rewrite={needs_rewrite} plan={len(plan)}")

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
                        for expected in sorted(plan.keys()):
                            key = plan[expected]
                            data = member_bytes.get(key)
                            if data is None:
                                continue
                            if not key.startswith("ext:") and Path(key).name != expected:
                                renamed += 1
                                notes.append(f"{Path(key).name} → {expected}")
                            info = zipfile.ZipInfo(filename=expected)
                            info.compress_type = zipfile.ZIP_DEFLATED
                            zout.writestr(info, data)
                            written += 1
                    if written == 0:
                        if tmp.exists():
                            tmp.unlink()
                        return False, "Aucune ROM écrite"
                    try:
                        os.replace(str(tmp), str(src))
                    except OSError:
                        try:
                            if src.exists():
                                src.unlink()
                        except OSError as e:
                            try:
                                if tmp.exists():
                                    tmp.unlink()
                            except OSError:
                                pass
                            msg = f"ZIP verrouillé (fermez MAME / l'explorateur) : {e}"
                            _repair_log(msg)
                            return False, msg
                        try:
                            os.replace(str(tmp), str(src))
                        except OSError as e:
                            return False, f"Remplacement ZIP échoué : {e}"
                    if renamed:
                        notes.insert(0, f"{renamed} ROM renommée(s)")
                    _repair_log(f"  wrote {written} roms renamed={renamed}")
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



def do_delete(row: Dict) -> Tuple[bool, str]:
    # Autorise la suppression de tout fichier présent (good, rename, bad, error…)
    if not row.get("path"):
        return False, "Pas de fichier à supprimer"
    src = Path(row["path"])
    if not src.exists():
        return False, "Déjà absent"
    try:
        src.unlink()
        row["status"] = "deleted"
        row["found"] = ""
        row["path"] = ""
        return True, "Supprimé"
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
            rel_l = str(p.relative_to(DEFAULT_DAT_DIR)).lower()
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



SCAN_LOCK = threading.Lock()

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
    "dat_path": "", "roms_path": str(DEFAULT_ROMS_DIR),
}

app = Flask(__name__)

@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


# HTML is loaded from a separate file for clarity
HTML_PATH = SCRIPT_DIR / "_ui.html"

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
    base = SCRIPT_DIR / "i18n"
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
    base = SCRIPT_DIR / "i18n"
    langs = []
    if base.is_dir():
        for p in sorted(base.glob("*.json")):
            langs.append(p.stem)
    return jsonify({"ok": True, "languages": langs or ["fr", "en"]})

@app.route("/api/load_dat", methods=["POST"])
def api_load_dat():
    path = Path(request.json.get("path", "").strip())
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
    body = request.json or {}
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
def api_scan():
    if not STATE.get("games_list"):
        return jsonify({"error": "Chargez d'abord un DAT"})
    body = request.json or {}
    path = Path((body.get("path") or "").strip())
    if not path.is_dir():
        return jsonify({"error": f"Dossier introuvable : {path}"})
    if not SCAN_LOCK.acquire(blocking=False):
        return jsonify({"error": "Un scan est déjà en cours"})
    try:
        return _api_scan_body(path, body)
    finally:
        SCAN_LOCK.release()


def _api_scan_body(path: Path, body: dict):
    # Nouveau scan : vider l'ancien résultat côté serveur immédiatement
    STATE["results"] = []
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
        results = scan_roms(
            path,
            STATE["rom_map"],
            STATE["games_list"],
            STATE.get("size_set") or set(),
            hash_mode,
            dat_mode,
            max_depth=max_depth,
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
            "results": public,
            "counts": _counts(results),
            "max_depth": effective_depth,
            "max_depth_auto": effective_depth != max_depth,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})

@app.route("/api/rename", methods=["POST"])
def api_rename():
    body = request.json or {}
    idx = body.get("index", -1)
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return jsonify({"error": "Index invalide"})
    results = STATE.get("results") or []
    if idx < 0 or idx >= len(results):
        return jsonify({"error": "Index invalide"})
    row = results[idx]
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
def api_repair():
    """Répare une ou plusieurs entrées (indices ou game/path). Reconstruction ZIP non-merged."""
    body = request.json or {}
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
        try:
            ok, msg = do_repair(row)
        except Exception as e:
            traceback.print_exc()
            ok, msg = False, str(e)
        if ok:
            done += 1
            row["repairable"] = _row_is_repairable(row)
            rows_out.append({"index": idx, "row": _public_row(row), "message": msg})
        else:
            failed.append({"index": idx, "error": msg, "game": row.get("game") or row.get("found")})
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
        with zipfile.ZipFile(str(zip_path), "r") as zin:
            # Précharge pour fermer la source avant remplacement (Windows / NAS)
            entries: List[Tuple[str, bytes, int]] = []
            for info in zin.infolist():
                if info.is_dir():
                    continue
                # garder le chemin interne (sous-dossiers éventuels)
                name = info.filename.replace("\\", "/")
                if name.endswith("/"):
                    continue
                data = zin.read(info.filename)
                entries.append((name, data, info.external_attr))
                members += 1

        with zipfile.ZipFile(
            str(tmp), "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level
        ) as zout:
            for name, data, ext_attr in entries:
                zi = zipfile.ZipInfo(filename=name)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = ext_attr
                zout.writestr(zi, data)

        try:
            os.replace(str(tmp), str(zip_path))
        except OSError:
            try:
                if zip_path.exists():
                    zip_path.unlink()
            except OSError as e:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
                return {
                    "ok": False,
                    "error": f"Fichier verrouillé : {e}",
                    "path": str(zip_path),
                    "name": zip_path.name,
                }
            os.replace(str(tmp), str(zip_path))

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
def api_delete():
    idx = request.json.get("index", -1)
    if idx < 0 or idx >= len(STATE["results"]):
        return jsonify({"error": "Index invalide"})
    row = STATE["results"][idx]
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
    body = request.json or {}
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
    body = request.json or {}

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
def api_fix_all():
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
def api_delete_selected():
    body = request.json or {}
    indices = body.get("indices") or []
    results = STATE.get("results") or []
    deleted = 0
    failed = []
    rows_out = []
    # Trier décroissant pour stabilité d'index (pas de retrait de liste)
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
def api_delete_bads():
    results = STATE.get("results") or []
    rows_out = []
    n = 0
    for i, r in enumerate(results):
        if r.get("status") not in ("bad", "error"):
            continue
        ok, msg = do_delete(r)
        if ok:
            n += 1
            rows_out.append({"index": i, "row": _public_row(r), "message": msg})
    return jsonify({
        "ok": True,
        "message": f"{n} fichier(s) supprimé(s)",
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


def extract_one_from_pack(pack_id: str, file_name: str) -> Dict[str, Any]:
    """Extrait un seul DAT depuis le zip en cache (télécharge le pack si absent)."""
    if pack_id not in DAT_PACKS:
        raise ValueError(f"Pack inconnu : {pack_id}")
    info = DAT_PACKS[pack_id]
    zip_path = CACHE_DIR / f"{pack_id}.zip"
    if not zip_path.is_file():
        _http_download(info["zip"], zip_path)

    target = DEFAULT_DAT_DIR / info["subdir"]
    target.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        # Cherche le fichier exact ou par suffixe
        match = None
        for name in zf.namelist():
            if Path(name).name == file_name or name == file_name:
                match = name
                break
        if match is None:
            # Fuzzy : même préfixe de nom de système
            for name in zf.namelist():
                if file_name.lower() in Path(name).name.lower():
                    match = name
                    break
        if match is None:
            raise FileNotFoundError(f"DAT introuvable dans le pack : {file_name}")

        base = Path(match).name
        dest = target / base
        with zf.open(match) as src, open(dest, "wb") as out:
            out.write(src.read())

    return {"ok": True, "path": str(dest), "file": base, "pack": pack_id}


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
    pack_id = (request.json or {}).get("pack", "nointro")
    try:
        result = download_and_extract_pack(pack_id)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/dats/download_one", methods=["POST"])
def api_dats_download_one():
    body = request.json or {}
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
def api_mame_enrich():
    """
    Enrichit les résultats du scan arcade avec les titres ADB (Nom complet).
    Body optionnel: { "games": ["1942", ...] } — sinon tous les résultats avec un game.
    """
    if not STATE.get("results"):
        return jsonify({"error": "Aucun résultat à enrichir — lancez un scan d'abord"})
    body = request.json or {}
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


def _parse_mame_ver(s: str) -> Optional[str]:
    m = re.search(r"(0\.\d{2,4})", s)
    if not m:
        m = re.search(r"\bv?(\d{3})\b", s)
        if m and 100 <= int(m.group(1)) <= 400:
            return f"0.{int(m.group(1))}"
        return None
    return m.group(1)


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


def download_fbneo_dat_version(version_id: str) -> Dict[str, Any]:
    """
    Télécharge le pack DAT FBNeo.
    - latest / vide → archive master (comportement pack fbneo)
    - build:YYYY-MM-DD → commit le plus proche sur dats/ puis archive
    - tag:xxx → archive de ce tag
    """
    version_id = (version_id or "latest").strip()
    target = DEFAULT_DAT_DIR / "fbneo"
    target.mkdir(parents=True, exist_ok=True)
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

    zip_path = cache / f"fbneo_{re.sub(r'[^A-Za-z0-9._-]+', '_', label)}.zip"
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

    # Base locale : 0.289 → 0.160 (listxml GitHub fiable sur cette plage)
    latest = 289
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

    # -listxml peut produire 100+ Mo : stream vers fichier
    with open(dest, "wb") as out:
        proc = subprocess.Popen(
            [str(mame_exe), "-listxml"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(mame_exe.parent),
        )
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
        proc.wait(timeout=600)
        if proc.returncode not in (0, None):
            err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", errors="replace")
            # certains builds renvoient code non-0 même avec XML OK
            if dest.stat().st_size < 1000:
                raise RuntimeError(f"mame -listxml a échoué (code {proc.returncode}) : {err[:500]}")

    if dest.stat().st_size < 1000:
        raise RuntimeError("XML généré trop petit — échec listxml")

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

PROGETTO_SUPPORT_DIR = Path(__file__).resolve().parent / "dat" / "support" / "CatVer"
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
    body = request.json or {}
    path = Path((body.get("path") or "").strip())
    try:
        return jsonify(analyze_mame_xml(path))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})


@app.route("/api/mame/export", methods=["POST"])
def api_mame_export():
    body = request.json or {}
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
    body = request.json or {}
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
        for n in range(289, 279, -1):
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
    body = request.json or {}
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
    body = request.json or {}
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
    body = request.json or {}
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
    body = request.json or {}
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
    body = request.json or {}
    sid = (body.get("system") or body.get("id") or "").strip()
    if not sid:
        return jsonify({"error": "system manquant"})
    sug = suggest_dat_for_system(sid)
    return jsonify({"ok": True, "suggestion": sug})




@app.route("/api/profiles/<profile_id>/set_dat", methods=["POST"])
def api_profiles_set_dat(profile_id: str):
    """Associe un DAT à un système du profil. Body: { system_id, dat }"""
    body = request.json or {}
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
def api_profiles_scan_system(profile_id: str):
    """
    Charge le DAT du système, scanne son dossier, met à jour last_counts dans le profil.
    Body: { system_id, max_depth? }
    Réutilise le moteur de scan existant (STATE).
    """
    body = request.json or {}
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

    if not SCAN_LOCK.acquire(blocking=False):
        return jsonify({"error": "Un scan est déjà en cours"})
    try:
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
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)})
    finally:
        try:
            SCAN_LOCK.release()
        except RuntimeError:
            pass



@app.route("/api/shutdown", methods=["POST", "GET"])
def api_shutdown():
    """Arrêt propre du serveur (bouton Quitter)."""
    def _stop():
        time.sleep(0.35)
        # Ferme le process Python → la fenêtre Lancer.bat se termine
        os._exit(0)
    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"ok": True, "message": "Arrêt…"})


def main():
    import argparse
    import threading
    import time
    import webbrowser

    parser = argparse.ArgumentParser(description="RomSet Verifier")
    parser.add_argument("--open", action="store_true", help="Ouvre le navigateur au demarrage")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    ensure_folders()
    print("=" * 50)
    print(f"  {APP_NAME} {APP_VERSION}")
    print(f"  http://127.0.0.1:{args.port}")
    print("=" * 50)
    print(f"  dat/  → {DEFAULT_DAT_DIR}")
    print(f"  roms/ → {DEFAULT_ROMS_DIR}")
    print()

    if args.open:
        def _open():
            time.sleep(1.2)
            webbrowser.open(f"http://127.0.0.1:{args.port}/")
        threading.Thread(target=_open, daemon=True).start()

    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
