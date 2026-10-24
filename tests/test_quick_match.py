"""
Tests : identification rapide des gros fichiers par nom + taille (sans lire leur contenu).

    pytest tests/
"""
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rom_verifier as rv  # noqa: E402

BIG = 4096  # seuil abaissé pour les tests


def crc(b: bytes) -> str:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08x}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(rv, "QUICK_MATCH_MIN_SIZE", BIG)
    rv.STATE.update({"results": [], "games_list": [], "roms_path": "", "scan_id": ""})
    return rv.app.test_client()


@pytest.fixture
def setup(tmp_path, client):
    iso = b"I" * (BIG * 2)
    small = b"s" * 1024
    dat = tmp_path / "x360.dat"
    dat.write_text(f"""<?xml version="1.0"?><datafile><header><name>Microsoft - Xbox 360</name></header>
<game name="Halo 3 (USA)"><rom name="Halo 3 (USA).iso" size="{len(iso)}" crc="{crc(iso)}"/></game>
<game name="Small (USA)"><rom name="Small (USA).bin" size="{len(small)}" crc="{crc(small)}"/></game>
</datafile>""")
    assert client.post("/api/load_dat", json={"path": str(dat)}).get_json()["ok"]
    roms = tmp_path / "roms"
    roms.mkdir()
    return roms, iso, small


def scan(client, roms, **extra):
    r = client.post("/api/scan", json={"path": str(roms), **extra}).get_json()
    assert r.get("ok"), r
    return r["results"]


def forbid_reads(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("le contenu d'un gros fichier a été lu")
    monkeypatch.setattr(rv, "_crc32_path", boom)
    monkeypatch.setattr(rv, "_sha1_path", boom)


def test_large_file_matched_by_name_and_size_without_reading(client, setup, monkeypatch):
    roms, iso, _ = setup
    (roms / "Halo 3 (USA).iso").write_bytes(iso)
    forbid_reads(monkeypatch)
    r = scan(client, roms)[0]
    assert r["status"] == "good" and r["quick"] is True
    assert "CRC non calculé" in r["message"]


def test_name_match_is_case_insensitive(client, setup, monkeypatch):
    roms, iso, _ = setup
    (roms / "HALO 3 (usa).ISO").write_bytes(iso)
    forbid_reads(monkeypatch)
    assert scan(client, roms)[0]["quick"] is True


def test_deep_check_computes_full_crc(client, setup):
    roms, iso, _ = setup
    bad = bytearray(iso)
    bad[100] ^= 0xFF  # même nom, même taille, contenu différent
    (roms / "Halo 3 (USA).iso").write_bytes(bytes(bad))
    assert scan(client, roms)[0]["status"] == "good"  # rapide : non détecté (par conception)
    res = scan(client, roms, deep=True)
    assert next(x for x in res if x["game"] == "Halo 3 (USA)")["status"] == "missing"
    assert any(x["status"] == "bad" and x["found"] == "Halo 3 (USA).iso" for x in res)


def test_wrong_name_falls_back_to_crc(client, setup):
    roms, iso, _ = setup
    (roms / "halo3.iso").write_bytes(iso)
    r = scan(client, roms)[0]
    assert r["status"] == "rename" and not r["quick"]


def test_wrong_size_is_not_quick_matched(client, setup):
    roms, iso, _ = setup
    (roms / "Halo 3 (USA).iso").write_bytes(iso + b"x")
    res = scan(client, roms)
    assert next(x for x in res if x["game"] == "Halo 3 (USA)")["status"] == "missing"


def test_small_files_always_get_a_crc(client, setup):
    roms, _, small = setup
    (roms / "Small (USA).bin").write_bytes(b"t" * len(small))  # bon nom + taille, mauvais contenu
    res = scan(client, roms)
    assert next(x for x in res if x["game"] == "Small (USA)")["status"] == "missing"
