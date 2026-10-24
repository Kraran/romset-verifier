"""
Tests : cache des CRC des fichiers hors archive (réutilisés tant que taille + date ne changent pas).

    pytest tests/
"""
import os
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rom_verifier as rv  # noqa: E402

MIN = 1024     # seuil du cache abaissé pour les tests
QUICK = 64 * 1024


def crc(b: bytes) -> str:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08x}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(rv, "HASH_CACHE_MIN_SIZE", MIN)
    monkeypatch.setattr(rv, "QUICK_MATCH_MIN_SIZE", QUICK)
    rv.STATE.update({"results": [], "games_list": [], "roms_path": "", "scan_id": ""})
    return rv.app.test_client()


@pytest.fixture
def setup(tmp_path, client):
    rom = b"R" * (MIN * 4)       # caché, mais sous le seuil nom + taille
    iso = b"I" * (QUICK * 2)     # éligible à l'identification nom + taille
    dat = tmp_path / "set.dat"
    dat.write_text(f"""<?xml version="1.0"?><datafile><header><name>Test</name></header>
<game name="Rom (USA)"><rom name="Rom (USA).bin" size="{len(rom)}" crc="{crc(rom)}"/></game>
<game name="Iso (USA)"><rom name="Iso (USA).iso" size="{len(iso)}" crc="{crc(iso)}"/></game>
</datafile>""")
    assert client.post("/api/load_dat", json={"path": str(dat)}).get_json()["ok"]
    roms = tmp_path / "roms"
    roms.mkdir()
    return roms, rom, iso


def scan(client, roms, **extra):
    r = client.post("/api/scan", json={"path": str(roms), **extra}).get_json()
    assert r.get("ok"), r
    return r


def row(res, game):
    return next(x for x in res["results"] if x["game"] == game)


def count_reads(monkeypatch):
    calls = []
    real = rv._crc32_path

    def spy(p):
        calls.append(Path(p).name)
        return real(p)
    monkeypatch.setattr(rv, "_crc32_path", spy)
    return calls


def test_second_scan_reuses_cached_crc(client, setup, monkeypatch):
    roms, rom, _ = setup
    (roms / "Rom (USA).bin").write_bytes(rom)
    reads = count_reads(monkeypatch)
    r1 = scan(client, roms)
    assert reads == ["Rom (USA).bin"] and r1["hash_cache_hits"] == 0
    r2 = scan(client, roms)
    assert reads == ["Rom (USA).bin"]  # pas relu
    assert r2["hash_cache_hits"] == 1 and row(r2, "Rom (USA)")["status"] == "good"


def test_cache_persists_on_disk(client, setup, monkeypatch, tmp_path):
    roms, rom, _ = setup
    (roms / "Rom (USA).bin").write_bytes(rom)
    scan(client, roms)
    assert rv.HASH_CACHE.file.is_file()
    # nouvelle instance (= redémarrage de l'application) : relit le fichier
    monkeypatch.setattr(rv, "HASH_CACHE", rv._HashCache(rv.HASH_CACHE.file))
    reads = count_reads(monkeypatch)
    assert scan(client, roms)["hash_cache_hits"] == 1 and reads == []


def test_modified_file_is_rehashed(client, setup, monkeypatch):
    roms, rom, _ = setup
    f = roms / "Rom (USA).bin"
    f.write_bytes(rom)
    scan(client, roms)
    bad = bytearray(rom)
    bad[0] ^= 0xFF
    st = f.stat()
    f.write_bytes(bytes(bad))
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    reads = count_reads(monkeypatch)
    res = scan(client, roms)
    assert reads == ["Rom (USA).bin"] and res["hash_cache_hits"] == 0
    assert row(res, "Rom (USA)")["status"] == "missing"


def test_deep_check_ignores_and_refreshes_cache(client, setup, monkeypatch):
    roms, rom, _ = setup
    (roms / "Rom (USA).bin").write_bytes(rom)
    scan(client, roms)
    reads = count_reads(monkeypatch)
    assert scan(client, roms, deep=True)["hash_cache_hits"] == 0
    assert reads == ["Rom (USA).bin"]


def test_cached_crc_beats_name_and_size(client, setup):
    """Un ISO corrompu repéré par la vérification approfondie reste signalé aux scans rapides suivants."""
    roms, _, iso = setup
    bad = bytearray(iso)
    bad[10] ^= 0xFF
    (roms / "Iso (USA).iso").write_bytes(bytes(bad))
    assert row(scan(client, roms), "Iso (USA)")["quick"] is True  # rapide : nom + taille
    assert row(scan(client, roms, deep=True), "Iso (USA)")["status"] == "missing"
    res = scan(client, roms)  # rapide à nouveau : le vrai CRC (mauvais) vient du cache
    assert res["hash_cache_hits"] == 1
    assert row(res, "Iso (USA)")["status"] == "missing"
    assert any(x["status"] == "bad" and x["found"] == "Iso (USA).iso" for x in res["results"])


def test_rename_keeps_cache_entry(client, setup, monkeypatch):
    roms, rom, _ = setup
    (roms / "rom.bin").write_bytes(rom)
    res = scan(client, roms)
    assert row(res, "Rom (USA)")["status"] == "rename"
    i = next(k for k, x in enumerate(rv.STATE["results"]) if x["game"] == "Rom (USA)")
    out = client.post("/api/rename", json={"index": i, "scan_id": rv.STATE["scan_id"]}).get_json()
    assert out.get("ok"), out
    assert (roms / "Rom (USA).bin").is_file()
    reads = count_reads(monkeypatch)
    assert scan(client, roms)["hash_cache_hits"] == 1 and reads == []


def test_clear_cache(client, setup, monkeypatch):
    roms, rom, _ = setup
    (roms / "Rom (USA).bin").write_bytes(rom)
    scan(client, roms)
    out = client.post("/api/hash_cache/clear", json={}).get_json()
    assert out["ok"] and out["cleared"] == 1
    reads = count_reads(monkeypatch)
    assert scan(client, roms)["hash_cache_hits"] == 0 and reads == ["Rom (USA).bin"]


def test_small_files_not_cached(client, setup, monkeypatch):
    monkeypatch.setattr(rv, "HASH_CACHE_MIN_SIZE", 10 ** 9)
    roms, rom, _ = setup
    (roms / "Rom (USA).bin").write_bytes(rom)
    scan(client, roms)
    assert scan(client, roms)["hash_cache_hits"] == 0
