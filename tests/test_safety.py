"""
Tests de non-régression : aucune action ne doit perdre de fichiers (C1 à C4).

    pip install pytest flask lxml
    pytest tests/
"""
import os
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rom_verifier as rv  # noqa: E402

A = b"A" * 1024
B = b"B" * 1024
R = b"R" * 4096


def crc(b: bytes) -> str:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08x}"


def mkzip(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def names(path: Path) -> list:
    with zipfile.ZipFile(path) as z:
        return sorted(z.namelist())


def trash_files(root: Path) -> list:
    t = root / rv.TRASH_DIR_NAME
    return sorted(p.name for p in t.rglob("*") if p.is_file()) if t.exists() else []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(rv, "_REPLACE_DELAY", 0)
    rv.STATE.update({"results": [], "games_list": [], "roms_path": ""})
    return rv.app.test_client()


def load(client, dat: Path) -> dict:
    r = client.post("/api/load_dat", json={"path": str(dat)}).get_json()
    assert r.get("ok"), r
    return r


def scan(client, folder: Path) -> list:
    r = client.post("/api/scan", json={"path": str(folder)}).get_json()
    assert r.get("ok"), r
    return r["results"]


def arcade_dat(tmp_path: Path) -> Path:
    dat = tmp_path / "mame.xml"
    dat.write_text(f"""<?xml version="1.0"?><mame build="0.289">
<machine name="g1" sourcefile="x.cpp"><description>G1</description>
<rom name="a.bin" size="1024" crc="{crc(A)}"/><rom name="b.bin" size="1024" crc="{crc(B)}"/></machine>
<machine name="g2" sourcefile="x.cpp"><rom name="a.bin" size="1024" crc="{crc(A)}"/></machine>
<machine name="g3" sourcefile="x.cpp"><rom name="b.bin" size="1024" crc="{crc(B)}"/></machine>
</mame>""")
    return dat


def standard_dat(tmp_path: Path) -> Path:
    dat = tmp_path / "nes.dat"
    dat.write_text(f"""<?xml version="1.0"?><datafile><header><name>NES</name></header>
<game name="Game (USA)"><rom name="Game (USA).nes" size="4096" crc="{crc(R)}"/></game></datafile>""")
    return dat


# --- C1 : remplacement sûr -------------------------------------------------

def _always_locked(*_a, **_k):
    raise OSError("verrouillé")


def test_rezip_locked_keeps_original(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "_REPLACE_DELAY", 0)
    z = mkzip(tmp_path / "x.zip", {"a.bin": A})
    monkeypatch.setattr(rv.os, "replace", _always_locked)
    res = rv.recompress_zip_file(z)
    monkeypatch.undo()
    assert not res["ok"]
    assert z.exists() and names(z) == ["a.bin"]
    assert not (tmp_path / "x.zip.rsv_rezip").exists()


def test_rename_locked_keeps_original(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "_REPLACE_DELAY", 0)
    z = mkzip(tmp_path / "x.zip", {"A.BIN": A})
    monkeypatch.setattr(rv.os, "replace", _always_locked)
    with pytest.raises(RuntimeError):
        rv._batch_rename_zip_members(z, {"A.BIN": "a.bin"})
    monkeypatch.undo()
    assert names(z) == ["A.BIN"]


def test_safe_replace_retries_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "_REPLACE_DELAY", 0)
    dst = tmp_path / "d.zip"
    dst.write_bytes(b"old")
    tmp = tmp_path / "d.zip.tmp"
    tmp.write_bytes(b"new")
    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("scan antivirus")
        return real(a, b)

    monkeypatch.setattr(rv.os, "replace", flaky)
    rv._safe_replace(tmp, dst)
    assert dst.read_bytes() == b"new" and calls["n"] == 3


def test_rezip_applies_compression_level(tmp_path):
    data = (bytes(range(256)) * 64 + b"xyz" * 5000) * 20
    z = mkzip(tmp_path / "x.zip", {"a.bin": data})
    rv.recompress_zip_file(z, compresslevel=1)
    size1 = z.stat().st_size
    rv.recompress_zip_file(z, compresslevel=9)
    with zipfile.ZipFile(z) as zf:
        assert zf.read("a.bin") == data
    assert z.stat().st_size <= size1


# --- C2 / C3 : suppression ---------------------------------------------------

def test_delete_bads_keeps_good_rom_in_same_zip(tmp_path, client):
    load(client, standard_dat(tmp_path))
    roms = tmp_path / "roms"
    roms.mkdir()
    z = mkzip(roms / "Game (USA).zip", {"Game (USA).nes": R, "info.txt": b"x" * 10})
    scan(client, roms)
    r = client.post("/api/delete_bads").get_json()
    assert r["deleted"] == 1
    assert names(z) == ["Game (USA).nes"]
    assert "info.txt" in trash_files(roms)
    # le rescan reste Good
    assert [x["status"] for x in scan(client, roms)] == ["good"]


def test_delete_bads_skips_orphan_bios_zip(tmp_path, client):
    load(client, arcade_dat(tmp_path))
    roms = tmp_path / "roms"
    roms.mkdir()
    mkzip(roms / "g1.zip", {"a.bin": A, "b.bin": B})
    mkzip(roms / "neogeo.zip", {"x.bin": b"Z" * 100})
    res = scan(client, roms)
    assert any(x["found"] == "neogeo.zip" and x["mtype"] == "extra" for x in res)
    client.post("/api/delete_bads")
    assert (roms / "neogeo.zip").exists()


def test_delete_bads_skips_error_rows(tmp_path, client):
    load(client, standard_dat(tmp_path))
    roms = tmp_path / "roms"
    roms.mkdir()
    (roms / "broken.zip").write_bytes(b"pas un zip")
    res = scan(client, roms)
    assert any(x["status"] == "error" for x in res)
    client.post("/api/delete_bads")
    assert (roms / "broken.zip").exists()


def test_delete_goes_to_trash(tmp_path, client):
    load(client, standard_dat(tmp_path))
    roms = tmp_path / "roms"
    roms.mkdir()
    (roms / "unknown.nes").write_bytes(b"U" * 4096)
    res = scan(client, roms)
    idx = next(i for i, x in enumerate(res) if x["found"] == "unknown.nes")
    r = client.post("/api/delete", json={"index": idx}).get_json()
    assert r.get("ok"), r
    assert not (roms / "unknown.nes").exists()
    assert trash_files(roms) == ["unknown.nes"]
    # la corbeille n'est pas rescannée
    assert all(".rsv_trash" not in x.get("path", "") for x in scan(client, roms))


# --- C4 : réparation ------------------------------------------------------------

def test_repair_keeps_members_not_in_dat(tmp_path, client):
    load(client, arcade_dat(tmp_path))
    roms = tmp_path / "roms"
    roms.mkdir()
    z = mkzip(roms / "g1.zip", {
        "wrong_a.bin": A, "b.bin": B, "device_rom.bin": b"D" * 256, "readme.txt": b"hi",
    })
    res = scan(client, roms)
    idx = next(i for i, x in enumerate(res) if x["game"] == "g1")
    r = client.post("/api/repair", json={"indices": [idx]}).get_json()
    assert r["repaired"] == 1, r
    assert names(z) == ["a.bin", "b.bin", "device_rom.bin", "readme.txt"]
    with zipfile.ZipFile(z) as zf:
        assert zf.read("device_rom.bin") == b"D" * 256


def test_repair_backs_up_dropped_bad_member(tmp_path, client):
    load(client, arcade_dat(tmp_path))
    roms = tmp_path / "roms"
    roms.mkdir()
    # a.bin a un mauvais contenu, la bonne ROM est sous un autre nom
    z = mkzip(roms / "g1.zip", {"a.bin": b"X" * 1024, "good_a.bin": A, "b.bin": B})
    res = scan(client, roms)
    idx = next(i for i, x in enumerate(res) if x["game"] == "g1")
    client.post("/api/repair", json={"indices": [idx]})
    with zipfile.ZipFile(z) as zf:
        assert zf.read("a.bin") == A
    backups = list((roms / rv.TRASH_DIR_NAME).rglob("g1*.zip"))
    assert backups, "le ZIP original doit être sauvegardé"
    with zipfile.ZipFile(backups[0]) as zf:
        assert zf.read("a.bin") == b"X" * 1024


def test_repair_locked_keeps_original(tmp_path, client, monkeypatch):
    load(client, arcade_dat(tmp_path))
    roms = tmp_path / "roms"
    roms.mkdir()
    z = mkzip(roms / "g1.zip", {"wrong_a.bin": A, "b.bin": B})
    res = scan(client, roms)
    idx = next(i for i, x in enumerate(res) if x["game"] == "g1")
    monkeypatch.setattr(rv.os, "replace", _always_locked)
    r = client.post("/api/repair", json={"indices": [idx]}).get_json()
    monkeypatch.undo()
    assert r["repaired"] == 0
    assert names(z) == ["b.bin", "wrong_a.bin"]
    assert not (roms / "g1.zip.rsv_repair").exists()
