"""
Tests : mise à jour des DAT (téléchargement intègre, anciennes versions mises de côté,
profils réorientés, suggestion de la version la plus récente, état dans le catalogue).
Aucun accès réseau.

    pytest tests/
"""
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rom_verifier as rv  # noqa: E402

OLD = "Nintendo - Game Boy (20250101-010101).dat"
NEW = "Nintendo - Game Boy (20261020-084512).dat"
GBC = "Nintendo - Game Boy Color (20250101-010101).dat"


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    dat, cache, prof = tmp_path / "dat", tmp_path / "cache", tmp_path / "profiles"
    for d in (dat, cache, prof):
        d.mkdir()
    monkeypatch.setattr(rv, "DEFAULT_DAT_DIR", dat)
    monkeypatch.setattr(rv, "CACHE_DIR", cache)
    monkeypatch.setattr(rv, "PROFILES_DIR", prof)
    return dat, cache, prof


def mkpack(path: Path, names) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, f"<datafile><!-- {n} --></datafile>")


# --- Téléchargement intègre ----------------------------------------------------------

class FakeResp(io.BytesIO):
    def __init__(self, data: bytes, length=None):
        super().__init__(data)
        self.headers = {"Content-Length": str(length if length is not None else len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_truncated_download_keeps_existing_file(tmp_path, monkeypatch):
    dest = tmp_path / "pack.zip"
    mkpack(dest, ["ancien.dat"])
    before = dest.read_bytes()
    monkeypatch.setattr(rv.urllib.request, "urlopen", lambda *a, **k: FakeResp(b"PK\x03\x04abc", length=10_000))
    with pytest.raises(IOError, match="incomplet"):
        rv._http_download("https://x/pack.zip", dest)
    assert dest.read_bytes() == before
    assert not list(tmp_path.glob("*.part"))


def test_html_error_page_is_not_saved_as_zip(tmp_path, monkeypatch):
    dest = tmp_path / "pack.zip"
    monkeypatch.setattr(rv.urllib.request, "urlopen", lambda *a, **k: FakeResp(b"<html>rate limited</html>"))
    with pytest.raises(IOError, match="illisible"):
        rv._http_download("https://x/pack.zip", dest)
    assert not dest.exists()


def test_corrupt_cached_pack_is_redownloaded(dirs, monkeypatch):
    _, cache, _ = dirs
    (cache / "nointro.zip").write_bytes(b"pas un zip")  # récent mais abîmé
    calls = []
    monkeypatch.setattr(rv, "_http_download", lambda url, dest, timeout=600: (calls.append(url), mkpack(dest, [NEW])))
    r = rv.extract_one_from_pack("nointro", NEW)
    assert calls and Path(r["path"]).is_file()


# --- Anciennes versions + profils ---------------------------------------------------

def make_profile(prof: Path, dat_path: Path) -> Path:
    p = prof / "salon.json"
    p.write_text(json.dumps({"id": "salon", "name": "Salon", "systems": [
        {"id": "gb", "dat": str(dat_path), "dat_name": dat_path.name, "last_counts": {"good": 3}},
        {"id": "gbc", "dat": "", "dat_name": ""},
    ]}), encoding="utf-8")
    return p


def test_update_one_dat_sets_old_version_aside_and_retargets_profiles(dirs, monkeypatch):
    dat, cache, prof = dirs
    target = dat / "nointro"
    target.mkdir()
    (target / OLD).write_text("ancien")
    (target / GBC).write_text("autre système")
    pf = make_profile(prof, target / OLD)
    mkpack(cache / "nointro.zip", [NEW, GBC])
    monkeypatch.setattr(rv, "_http_download", lambda *a, **k: pytest.fail("cache frais"))

    r = rv.extract_one_from_pack("nointro", NEW)

    assert r["superseded"] == 1
    assert sorted(p.name for p in target.iterdir()) == sorted([NEW, GBC])  # GBC n'est pas « Game Boy »
    assert (cache / "superseded_dats" / OLD).read_text() == "ancien"  # mis de côté, pas supprimé
    sys_gb = json.loads(pf.read_text(encoding="utf-8"))["systems"][0]
    assert sys_gb["dat"] == str(target / NEW) and sys_gb["dat_name"] == NEW
    assert sys_gb["last_counts"] == {"good": 3}


def test_full_pack_update(dirs, monkeypatch):
    dat, cache, _ = dirs
    target = dat / "redump"
    target.mkdir()
    old = "Sony - PlayStation - Datfile (10800) (2025-01-01 10-00-00).dat"
    new = "Sony - PlayStation - Datfile (10852) (2026-10-20 08-45-12).dat"
    ps2 = "Sony - PlayStation 2 - Datfile (11000) (2026-10-20 08-45-12).dat"
    (target / old).write_text("ancien")
    monkeypatch.setattr(rv, "_http_download", lambda url, dest, timeout=600: mkpack(dest, [new, ps2]))
    r = rv.download_and_extract_pack("redump")
    assert r["extracted"] == 2 and r["superseded"] == 1
    assert sorted(p.name for p in target.iterdir()) == sorted([new, ps2])


def test_newer_local_dat_is_never_replaced_by_an_older_one(dirs, monkeypatch):
    dat, cache, _ = dirs
    target = dat / "nointro"
    target.mkdir()
    (target / NEW).write_text("récent")
    mkpack(cache / "nointro.zip", [OLD])
    rv.extract_one_from_pack("nointro", OLD)
    assert (target / NEW).read_text() == "récent"


def test_mameredump_counts_are_versions(dirs):
    dat, _, _ = dirs
    d = dat / "mameredump"
    d.mkdir()
    old, new = d / "3DO Interactive Multiplayer (668).dat", d / "3DO Interactive Multiplayer (670).dat"
    old.write_text("a")
    past = time.time() - 3600
    os.utime(old, (past, past))
    new.write_text("b")
    assert rv._supersede_old_dats([new]) == {str(old): str(new)}


@pytest.mark.parametrize("a,b,same", [
    (OLD, NEW, True),
    ("Sega - Mega Drive - Genesis (20261020-084512).dat", "Sega - Mega Drive - Genesis (Private) (20261020-084512).dat", False),
    ("Nintendo - Nintendo Entertainment System (Headered) (1).dat", "Nintendo - Nintendo Entertainment System (Headered) (2).dat", True),
    (OLD, GBC, False),
])
def test_dat_family(a, b, same):
    assert (rv._dat_family(a) == rv._dat_family(b)) is same


# --- Suggestion et catalogue ----------------------------------------------------------

def test_suggestion_prefers_newest_version(dirs):
    dat, _, _ = dirs
    d = dat / "nointro"
    d.mkdir()
    # l'ancien est écrit APRÈS (mtime plus récent) : c'est la date du nom qui décide
    (d / NEW).write_text("x")
    time.sleep(0.01)
    (d / OLD).write_text("x")
    sug = rv.suggest_dat_for_system("gb")
    assert sug and sug["name"] == NEW


def test_catalog_shows_local_status(dirs, monkeypatch):
    dat, _, _ = dirs
    target = dat / "nointro"
    target.mkdir()
    (target / OLD).write_text("x")
    (target / "Sega - 32X (20261020-084512).dat").write_text("x")
    xml = ("<clrmamepro>"
           f"<datfile><name>Nintendo - Game Boy</name><version>20261020-084512</version><file>{NEW}</file></datfile>"
           "<datfile><name>Sega - 32X</name><version>20261020-084512</version><file>Sega - 32X (20261020-084512).dat</file></datfile>"
           "<datfile><name>Atari - Lynx</name><version>1</version><file>Atari - Lynx (20261020-084512).dat</file></datfile>"
           "</clrmamepro>").encode()
    monkeypatch.setattr(rv, "_http_get", lambda url, timeout=60: xml)
    st = {it["name"]: it["status"] for it in rv.fetch_catalog("nointro")}
    assert st == {"Nintendo - Game Boy": "update", "Sega - 32X": "installed", "Atari - Lynx": ""}


# --- Choix du DAT d'un système ----------------------------------------------------------

NOINTRO = [
    "Nintendo - Game Boy (20261020-084512).dat",
    "Nintendo - Game Boy Color (20261021-084512).dat",
    "Nintendo - Game Boy Advance (20261022-084512).dat",
    "Source Code - Nintendo - Game Boy (20261023-084512).dat",
    "Nintendo - Nintendo Entertainment System (Headered) (20261020-084512).dat",
    "Nintendo - Super Nintendo Entertainment System (20261024-084512).dat",
    "Sega - Mega Drive - Genesis (20261020-084512).dat",
    "Sega - Mega Drive - Genesis (Private) (20261025-084512).dat",
]
REDUMP = [
    "Sony - PlayStation - Datfile (10852) (2026-10-20 08-45-12).dat",
    "Sony - PlayStation 2 - Datfile (11000) (2026-10-21 08-45-12).dat",
    "Sony - PlayStation Portable - Datfile (1900) (2026-10-22 08-45-12).dat",
]


@pytest.mark.parametrize("system,expected", [
    ("gb", "Nintendo - Game Boy (20261020-084512).dat"),
    ("gbc", "Nintendo - Game Boy Color (20261021-084512).dat"),
    ("gba", "Nintendo - Game Boy Advance (20261022-084512).dat"),
    ("nes", "Nintendo - Nintendo Entertainment System (Headered) (20261020-084512).dat"),
    ("snes", "Nintendo - Super Nintendo Entertainment System (20261024-084512).dat"),
    ("megadrive", "Sega - Mega Drive - Genesis (20261020-084512).dat"),
    ("psx", "Sony - PlayStation - Datfile (10852) (2026-10-20 08-45-12).dat"),
    ("ps2", "Sony - PlayStation 2 - Datfile (11000) (2026-10-21 08-45-12).dat"),
    ("psp", "Sony - PlayStation Portable - Datfile (1900) (2026-10-22 08-45-12).dat"),
])
def test_system_gets_its_own_dat_not_a_sibling(dirs, system, expected):
    dat, _, _ = dirs
    for sub, names in (("nointro", NOINTRO), ("redump", REDUMP)):
        (dat / sub).mkdir(exist_ok=True)
        for n in names:
            (dat / sub / n).write_text("x")
    assert rv.suggest_dat_for_system(system)["name"] == expected


def test_catalog_status_when_local_is_newer(dirs, monkeypatch):
    dat, _, _ = dirs
    target = dat / "nointro"
    target.mkdir()
    (target / NEW).write_text("x")  # plus récent que le catalogue
    xml = f"<clrmamepro><datfile><name>Nintendo - Game Boy</name><version>1</version><file>{OLD}</file></datfile></clrmamepro>".encode()
    monkeypatch.setattr(rv, "_http_get", lambda url, timeout=60: xml)
    assert rv.fetch_catalog("nointro")[0]["status"] == "installed"
