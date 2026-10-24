"""
Tests : mise à jour des .cue / .m3u au renommage (M4) et archives .7z / .rar (M6).

    pytest tests/
"""
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rom_verifier as rv  # noqa: E402

T1 = b"1" * 2352 * 4
T2 = b"2" * 2352 * 2
R = b"R" * 4096
X = b"X" * 4096


def crc(b: bytes) -> str:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08x}"


@pytest.fixture
def client():
    rv.STATE.update({"results": [], "games_list": [], "roms_path": "", "scan_id": ""})
    return rv.app.test_client()


def load(client, dat: Path) -> None:
    assert client.post("/api/load_dat", json={"path": str(dat)}).get_json().get("ok")


def scan(client, folder: Path) -> list:
    r = client.post("/api/scan", json={"path": str(folder)}).get_json()
    assert r.get("ok"), r
    return r["results"]


def act(client, url: str, body: dict = None) -> dict:
    body = dict(body or {})
    body.setdefault("scan_id", rv.STATE.get("scan_id", ""))
    return client.post(url, json=body).get_json()


# --- M4 : feuilles de pistes ------------------------------------------------------

@pytest.fixture
def redump_dat(tmp_path):
    dat = tmp_path / "redump.dat"
    dat.write_text(f"""<?xml version="1.0"?><datafile><header><name>PSX</name></header>
<game name="Game (Europe)">
<rom name="Game (Europe) (Track 1).bin" size="{len(T1)}" crc="{crc(T1)}"/>
<rom name="Game (Europe) (Track 2).bin" size="{len(T2)}" crc="{crc(T2)}"/>
</game></datafile>""")
    return dat


def write_cue(path: Path, t1: str, t2: str, newline: str = "\r\n") -> Path:
    lines = [
        f'FILE "{t1}" BINARY', "  TRACK 01 MODE2/2352", "    INDEX 01 00:00:00",
        f'FILE "{t2}" BINARY', "  TRACK 02 AUDIO", "    INDEX 01 00:00:00",
    ]
    path.write_bytes(newline.join(lines).encode("utf-8") + newline.encode())
    return path


def test_rename_tracks_updates_cue(tmp_path, client, redump_dat):
    load(client, redump_dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    (roms / "game1.bin").write_bytes(T1)
    (roms / "game2.bin").write_bytes(T2)
    cue = write_cue(roms / "Game.cue", "game1.bin", "game2.bin")
    res = scan(client, roms)
    for i, x in enumerate(res):
        if x["status"] == "rename":
            r = act(client, "/api/rename", {"index": i})
            assert r.get("ok"), r
            assert "Game.cue mis à jour" in r["message"]
    text = cue.read_bytes().decode()
    assert 'FILE "Game (Europe) (Track 1).bin" BINARY' in text
    assert 'FILE "Game (Europe) (Track 2).bin" BINARY' in text
    assert "\r\n" in text and "game1.bin" not in text  # fins de ligne conservées
    # l'original est sauvegardé
    backups = list((roms / rv.TRASH_DIR_NAME).rglob("Game.cue"))
    assert backups and b"game1.bin" in backups[0].read_bytes()


def test_only_whole_references_are_replaced(tmp_path):
    folder = tmp_path
    cue = folder / "a.cue"
    cue.write_text('FILE "track.bin" BINARY\nREM mytrack.bin track.bin.bak\nFILE "sub/track.bin" BINARY\n')
    notes = rv._update_playlist_refs(folder, "track.bin", "New Name.bin")
    text = cue.read_text()
    assert 'FILE "New Name.bin" BINARY' in text
    assert "mytrack.bin" in text and "track.bin.bak" in text
    assert 'FILE "sub/New Name.bin" BINARY' in text
    assert notes == ["a.cue mis à jour (2 référence(s))"]


def test_cue_matching_dat_is_untouched(tmp_path):
    cue = write_cue(tmp_path / "Game.cue", "Game (Europe) (Track 1).bin", "Game (Europe) (Track 2).bin")
    before = cue.read_bytes()
    assert rv._update_playlist_refs(tmp_path, "other.bin", "Game (Europe) (Track 1).bin") == []
    assert cue.read_bytes() == before


def test_m3u_and_latin1_cue(tmp_path):
    (tmp_path / "Jeu.m3u").write_text("Jeu (Disc 1).cue\nJeu (Disc 2).cue\n")
    cue = tmp_path / "x.cue"
    cue.write_bytes('FILE "été.bin" BINARY\r\n'.encode("cp1252"))
    rv._update_playlist_refs(tmp_path, "Jeu (Disc 1).cue", "Jeu (France) (Disc 1).cue")
    assert (tmp_path / "Jeu.m3u").read_text().splitlines()[0] == "Jeu (France) (Disc 1).cue"
    rv._update_playlist_refs(tmp_path, "été.bin", "Été (France).bin")
    assert cue.read_bytes() == 'FILE "Été (France).bin" BINARY\r\n'.encode("cp1252")


# --- M6 : archives .7z / .rar -------------------------------------------------------

try:
    import py7zr
except ImportError:  # les tests .cue ci-dessus tournent quand même
    py7zr = None
needs_py7zr = pytest.mark.skipif(py7zr is None, reason="py7zr non installé")


@pytest.fixture
def nes_dat(tmp_path):
    dat = tmp_path / "nes.dat"
    dat.write_text(f"""<?xml version="1.0"?><datafile><header><name>NES</name></header>
<game name="Game (USA)"><rom name="Game (USA).nes" size="4096" crc="{crc(R)}"/></game>
</datafile>""")
    return dat


def mk7z(path: Path, members: dict) -> Path:
    with py7zr.SevenZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(data, name)
    return path


@needs_py7zr
def test_7z_good(tmp_path, client, nes_dat):
    load(client, nes_dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    mk7z(roms / "Game (USA).7z", {"Game (USA).nes": R})
    res = scan(client, roms)
    assert [(x["status"], x["found"]) for x in res] == [("good", "Game (USA).7z → Game (USA).nes")]


@needs_py7zr
def test_7z_misnamed_archive_is_renamed(tmp_path, client, nes_dat):
    load(client, nes_dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    mk7z(roms / "game.7z", {"Game (USA).nes": R})
    res = scan(client, roms)
    assert res[0]["status"] == "rename"
    r = act(client, "/api/rename", {"index": 0})
    assert r.get("ok"), r
    assert (roms / "Game (USA).7z").exists()
    assert scan(client, roms)[0]["status"] == "good"


@needs_py7zr
def test_7z_member_rename_refused_clearly(tmp_path, client, nes_dat):
    load(client, nes_dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    mk7z(roms / "Game (USA).7z", {"wrong.nes": R})
    res = scan(client, roms)
    assert res[0]["status"] == "rename"
    r = act(client, "/api/rename", {"index": 0})
    assert "lecture seule" in r["error"]
    assert (roms / "Game (USA).7z").exists()


@needs_py7zr
def test_7z_delete_member_refused_when_others(tmp_path, client, nes_dat):
    load(client, nes_dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    z = mk7z(roms / "Game (USA).7z", {"Game (USA).nes": R, "unknown.nes": X})
    res = scan(client, roms)
    i = next(i for i, x in enumerate(res) if x["found"].endswith("unknown.nes"))
    assert res[i]["status"] == "bad"
    r = act(client, "/api/delete", {"index": i})
    assert "lecture seule" in r["error"]
    assert z.exists()


def test_rar_reported_not_ignored(tmp_path, client, nes_dat):
    load(client, nes_dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    (roms / "Game (USA).rar").write_bytes(b"Rar!\x1a\x07\x00")
    res = scan(client, roms)
    rar = next(x for x in res if x["found"] == "Game (USA).rar")
    assert rar["status"] == "error" and "RAR" in rar["message"]


@needs_py7zr
def test_7z_without_py7zr_is_reported(tmp_path, client, nes_dat, monkeypatch):
    load(client, nes_dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    mk7z(roms / "Game (USA).7z", {"Game (USA).nes": R})
    monkeypatch.setattr(rv, "py7zr", None)
    res = scan(client, roms)
    err = next(x for x in res if x["status"] == "error")
    assert "py7zr" in err["message"]
