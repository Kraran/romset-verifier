"""
Tests de non-régression : statuts de scan (rapport C5, M1, M2, M5).

    pytest tests/
"""
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rom_verifier as rv  # noqa: E402

A = b"A" * 1024
B = b"B" * 1024
O = b"O" * 512
R = b"R" * 4096
T = b"T" * 2048
SHA = "11" * 20


def act(client, url: str, body: dict = None):
    """Action sur les résultats du dernier scan (joint le scan_id, comme l'UI)."""
    body = dict(body or {})
    body.setdefault("scan_id", rv.STATE.get("scan_id", ""))
    return client.post(url, json=body)


def crc(b: bytes) -> str:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08x}"


def mkzip(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def chd(path: Path, sha1: str) -> Path:
    """En-tête CHD v5 minimal portant le SHA-1 combiné à l'offset 84."""
    hdr = bytearray(124)
    hdr[:8] = b"MComprHD"
    hdr[12:16] = (5).to_bytes(4, "big")
    hdr[84:104] = bytes.fromhex(sha1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(hdr))
    return path


@pytest.fixture
def client():
    rv.STATE.update({"results": [], "games_list": [], "roms_path": ""})
    return rv.app.test_client()


def load(client, dat: Path) -> None:
    r = client.post("/api/load_dat", json={"path": str(dat)}).get_json()
    assert r.get("ok"), r


def scan(client, folder: Path) -> list:
    r = client.post("/api/scan", json={"path": str(folder)}).get_json()
    assert r.get("ok"), r
    return r["results"]


def row(results: list, game: str) -> dict:
    return next(x for x in results if x["game"] == game)


# --- C5 : un fichier ne sert qu'une entrée DAT --------------------------------

@pytest.fixture
def same_crc_dat(tmp_path):
    dat = tmp_path / "r.dat"
    dat.write_text(f"""<?xml version="1.0"?><datafile><header><name>X</name></header>
<game name="Game (USA)"><rom name="Game (USA) (Track 2).bin" size="2048" crc="{crc(T)}"/></game>
<game name="Game (Europe)"><rom name="Game (Europe) (Track 2).bin" size="2048" crc="{crc(T)}"/></game>
</datafile>""")
    return dat


def test_shared_content_is_not_proposed_for_rename(tmp_path, client, same_crc_dat):
    load(client, same_crc_dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    (roms / "Game (Europe) (Track 2).bin").write_bytes(T)
    res = scan(client, roms)
    assert row(res, "Game (Europe)")["status"] == "good"
    usa = row(res, "Game (USA)")
    assert usa["status"] == "missing"
    assert "copie nécessaire" in usa["message"]
    act(client, "/api/fix_all")
    assert (roms / "Game (Europe) (Track 2).bin").exists()


def test_both_copies_present_are_both_good(tmp_path, client, same_crc_dat):
    load(client, same_crc_dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    (roms / "Game (Europe) (Track 2).bin").write_bytes(T)
    (roms / "Game (USA) (Track 2).bin").write_bytes(T)
    res = scan(client, roms)
    assert sorted(x["status"] for x in res) == ["good", "good"]


def test_exact_name_wins_over_scan_order(tmp_path, client, same_crc_dat):
    """Le fichier au bon nom revient à son jeu, l'autre copie au nom faux est à renommer."""
    load(client, same_crc_dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    (roms / "Game (Europe) (Track 2).bin").write_bytes(T)
    (roms / "copie.bin").write_bytes(T)
    res = scan(client, roms)
    assert row(res, "Game (Europe)")["found"] == "Game (Europe) (Track 2).bin"
    usa = row(res, "Game (USA)")
    assert usa["status"] == "rename" and usa["found"] == "copie.bin"


def test_extra_copy_is_reported_as_duplicate(tmp_path, client):
    dat = tmp_path / "nes.dat"
    dat.write_text(f"""<?xml version="1.0"?><datafile>
<game name="Game (USA)"><rom name="Game (USA).nes" size="4096" crc="{crc(R)}"/></game></datafile>""")
    load(client, dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    (roms / "Game (USA).nes").write_bytes(R)
    (roms / "copy.nes").write_bytes(R)
    res = scan(client, roms)
    assert row(res, "Game (USA)")["status"] == "good"
    dup = next(x for x in res if x["found"] == "copy.nes")
    assert dup["status"] == "bad" and dup["message"].startswith("Doublon")


# --- M1 : ROM / CHD optionnels -------------------------------------------------

def test_missing_optional_rom_keeps_set_good(tmp_path, client):
    dat = tmp_path / "mame.xml"
    dat.write_text(f"""<?xml version="1.0"?><mame build="0.289">
<machine name="g1" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/>
<rom name="b.bin" size="1024" crc="{crc(B)}"/>
<rom name="opt.bin" size="512" crc="{crc(O)}" optional="yes"/>
<disk name="extra" sha1="{SHA}" optional="yes"/></machine>
<machine name="g2" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/></machine>
<machine name="g3" sourcefile="x.cpp"><rom name="b.bin" size="1024" crc="{crc(B)}"/></machine>
</mame>""")
    load(client, dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    mkzip(roms / "g1.zip", {"a.bin": A, "b.bin": B})
    r = row(scan(client, roms), "g1")
    assert r["status"] == "good", r["message"]
    assert r["crc"] == "2/2"


def test_present_optional_rom_is_ok(tmp_path, client):
    dat = tmp_path / "mame.xml"
    dat.write_text(f"""<?xml version="1.0"?><mame build="0.289">
<machine name="g1" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/>
<rom name="opt.bin" size="512" crc="{crc(O)}" optional="yes"/></machine>
<machine name="g2" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/></machine>
<machine name="g3" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/></machine>
</mame>""")
    load(client, dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    mkzip(roms / "g1.zip", {"a.bin": A, "opt.bin": O})
    assert row(scan(client, roms), "g1")["status"] == "good"


# --- M2 : machines CHD seules ---------------------------------------------------

@pytest.fixture
def chd_dat(tmp_path):
    dat = tmp_path / "m.xml"
    dat.write_text(f"""<?xml version="1.0"?><mame build="0.289">
<machine name="cdgame" sourcefile="x.cpp"><disk name="cdgame" sha1="{SHA}"/></machine>
<machine name="g" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/></machine>
<machine name="h" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/></machine>
</mame>""")
    return dat


def test_chd_only_machine_found_without_zip(tmp_path, client, chd_dat):
    load(client, chd_dat)
    roms = tmp_path / "roms"
    chd(roms / "cdgame" / "cdgame.chd", SHA)
    r = row(scan(client, roms), "cdgame")
    assert r["status"] == "good", r["message"]
    assert not r.get("repairable")


def test_chd_only_machine_bad_sha1(tmp_path, client, chd_dat):
    load(client, chd_dat)
    roms = tmp_path / "roms"
    chd(roms / "cdgame" / "cdgame.chd", "22" * 20)
    assert row(scan(client, roms), "cdgame")["status"] == "bad"


def test_chd_only_machine_absent(tmp_path, client, chd_dat):
    load(client, chd_dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    assert row(scan(client, roms), "cdgame")["status"] == "missing"


# --- M5 : une ROM loose valide n'est pas « réparable » ----------------------------

def test_loose_good_rom_not_repairable(tmp_path, client):
    dat = tmp_path / "nes.dat"
    dat.write_text(f"""<?xml version="1.0"?><datafile>
<game name="Game (USA)"><rom name="Game (USA).nes" size="4096" crc="{crc(R)}"/></game></datafile>""")
    load(client, dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    (roms / "Game (USA).nes").write_bytes(R)
    r = scan(client, roms)[0]
    assert r["status"] == "good"
    assert not rv._row_is_repairable(r)
