"""
Tests : vérification approfondie (M3), rattachement des ZIP mal nommés (R2),
réparation sans charger les ROM en mémoire (R3).

    pytest tests/
"""
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rom_verifier as rv  # noqa: E402

A, B, C, D, E, X = (bytes([65 + i]) * 1024 for i in range(6))
R = b"R" * 4096


def crc(b: bytes) -> str:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08x}"


def mkzip(path: Path, members: dict, stored: bool = True) -> Path:
    mode = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(path, "w", mode) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def corrupt_member_data(zpath: Path, data: bytes) -> None:
    """Modifie un octet des données d'un membre STORED sans toucher au CRC de l'en-tête."""
    raw = bytearray(zpath.read_bytes())
    i = raw.find(data[:64])
    assert i >= 0
    raw[i + 100] ^= 0xFF
    zpath.write_bytes(bytes(raw))


@pytest.fixture
def client():
    rv.STATE.update({"results": [], "games_list": [], "roms_path": "", "scan_id": ""})
    return rv.app.test_client()


def load(client, dat: Path) -> None:
    assert client.post("/api/load_dat", json={"path": str(dat)}).get_json().get("ok")


def scan(client, folder: Path, **extra) -> list:
    r = client.post("/api/scan", json={"path": str(folder), **extra}).get_json()
    assert r.get("ok"), r
    return r["results"]


def row(results, game):
    return next(x for x in results if x["game"] == game)


# --- M3 : vérification approfondie --------------------------------------------------

def test_deep_scan_detects_corrupt_zip_standard(tmp_path, client):
    dat = tmp_path / "nes.dat"
    dat.write_text(f"""<?xml version="1.0"?><datafile>
<game name="Game (USA)"><rom name="Game (USA).nes" size="4096" crc="{crc(R)}"/></game></datafile>""")
    load(client, dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    z = mkzip(roms / "Game (USA).zip", {"Game (USA).nes": R})
    corrupt_member_data(z, R)
    assert scan(client, roms)[0]["status"] == "good"  # rapide : en-tête seul
    res = scan(client, roms, deep=True)
    assert row(res, "Game (USA)")["status"] == "missing"
    bad = next(x for x in res if x["status"] == "bad")
    assert "corrompues" in bad["message"]


def test_deep_scan_ok_on_clean_zip(tmp_path, client):
    dat = tmp_path / "nes.dat"
    dat.write_text(f"""<?xml version="1.0"?><datafile>
<game name="Game (USA)"><rom name="Game (USA).nes" size="4096" crc="{crc(R)}"/></game></datafile>""")
    load(client, dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    mkzip(roms / "Game (USA).zip", {"Game (USA).nes": R}, stored=False)
    assert scan(client, roms, deep=True)[0]["status"] == "good"


def test_deep_scan_detects_corrupt_zip_arcade(tmp_path, client):
    dat = tmp_path / "mame.xml"
    dat.write_text(f"""<?xml version="1.0"?><mame build="0.289">
<machine name="g1" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/>
<rom name="b.bin" size="1024" crc="{crc(B)}"/></machine>
<machine name="g2" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/></machine>
<machine name="g3" sourcefile="x.cpp"><rom name="b.bin" size="1024" crc="{crc(B)}"/></machine>
</mame>""")
    load(client, dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    z = mkzip(roms / "g1.zip", {"a.bin": A, "b.bin": B})
    corrupt_member_data(z, A)
    assert row(scan(client, roms), "g1")["status"] == "good"
    res = scan(client, roms, deep=True)
    assert row(res, "g1")["status"] == "bad"
    detail = client.post("/api/result_detail", json={
        "index": next(i for i, x in enumerate(res) if x["game"] == "g1"),
    }).get_json()["row"]
    comp = next(c for c in detail["components"] if c["expected"] == "a.bin")
    assert comp["status"] == "bad_crc" and "corrompues" in comp["message"]
    # la ROM corrompue n'est pas proposée comme source pour g2
    assert not row(res, "g2").get("available_count")


# --- R2 : rattachement des ZIP mal nommés --------------------------------------------

def test_misnamed_clone_zip_goes_to_clone_not_parent(tmp_path, client):
    dat = tmp_path / "mame.xml"
    dat.write_text(f"""<?xml version="1.0"?><mame build="0.289">
<machine name="parent" sourcefile="x.cpp">
<rom name="a.bin" size="1024" crc="{crc(A)}"/><rom name="b.bin" size="1024" crc="{crc(B)}"/>
<rom name="c.bin" size="1024" crc="{crc(C)}"/><rom name="d.bin" size="1024" crc="{crc(D)}"/>
<rom name="e.bin" size="1024" crc="{crc(E)}"/></machine>
<machine name="clone" sourcefile="x.cpp" cloneof="parent" romof="parent">
<rom name="a.bin" size="1024" crc="{crc(A)}"/><rom name="b.bin" size="1024" crc="{crc(B)}"/>
<rom name="c.bin" size="1024" crc="{crc(C)}"/><rom name="d.bin" size="1024" crc="{crc(D)}"/>
<rom name="x.bin" size="1024" crc="{crc(X)}"/></machine>
<machine name="other" sourcefile="y.cpp"><rom name="r.bin" size="4096" crc="{crc(R)}"/></machine>
</mame>""")
    load(client, dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    # le ZIP du clone, mal nommé
    mkzip(roms / "clone (copie).zip", {"a.bin": A, "b.bin": B, "c.bin": C, "d.bin": D, "x.bin": X})
    res = scan(client, roms)
    assert row(res, "clone")["status"] == "rename"
    assert row(res, "clone")["path"].endswith("clone (copie).zip")
    assert row(res, "parent")["status"] == "missing"


def test_ambiguous_misnamed_zip_is_not_assigned(tmp_path, client):
    """Deux machines au contenu identique : impossible de choisir, on n'associe pas."""
    dat = tmp_path / "mame.xml"
    dat.write_text(f"""<?xml version="1.0"?><mame build="0.289">
<machine name="m1" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/>
<rom name="b.bin" size="1024" crc="{crc(B)}"/></machine>
<machine name="m2" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/>
<rom name="b.bin" size="1024" crc="{crc(B)}"/></machine>
<machine name="m3" sourcefile="x.cpp"><rom name="c.bin" size="1024" crc="{crc(C)}"/></machine>
</mame>""")
    load(client, dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    mkzip(roms / "unknown.zip", {"a.bin": A, "b.bin": B})
    res = scan(client, roms)
    assert row(res, "m1")["status"] == "missing" and row(res, "m2")["status"] == "missing"


# --- R3 : réparation sans charger les ROM en mémoire -----------------------------------

def test_repair_streams_members(tmp_path, client, monkeypatch):
    dat = tmp_path / "mame.xml"
    dat.write_text(f"""<?xml version="1.0"?><mame build="0.289">
<machine name="g1" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/>
<rom name="b.bin" size="1024" crc="{crc(B)}"/></machine>
<machine name="g2" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/></machine>
<machine name="g3" sourcefile="x.cpp"><rom name="b.bin" size="1024" crc="{crc(B)}"/></machine>
</mame>""")
    load(client, dat)
    roms = tmp_path / "roms"
    roms.mkdir()
    z = mkzip(roms / "g1.zip", {"wrong_a.bin": A, "b.bin": B, "extra.txt": b"x"})
    res = scan(client, roms)
    idx = next(i for i, x in enumerate(res) if x["game"] == "g1")

    def no_full_read(*_a, **_k):
        raise AssertionError("ZipFile.read() appelé : la ROM entière est chargée en mémoire")

    monkeypatch.setattr(zipfile.ZipFile, "read", no_full_read)
    r = client.post("/api/repair", json={"indices": [idx], "scan_id": rv.STATE["scan_id"]}).get_json()
    monkeypatch.undo()
    assert r["repaired"] == 1, r
    with zipfile.ZipFile(z) as zf:
        assert sorted(zf.namelist()) == ["a.bin", "b.bin", "extra.txt"]
        assert zf.read("a.bin") == A
