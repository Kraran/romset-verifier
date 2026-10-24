"""
Tests : avancement du scan (GET /api/scan/progress), annulation, lectures de gros fichiers limitées.

    pytest tests/
"""
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rom_verifier as rv  # noqa: E402

SIZE = 256 * 1024


def crc(b: bytes) -> str:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08x}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(rv, "HASH_CACHE_MIN_SIZE", 1024)
    monkeypatch.setattr(rv, "QUICK_MATCH_MIN_SIZE", SIZE)
    monkeypatch.setattr(rv, "BIG_READ_MIN_SIZE", SIZE)
    monkeypatch.setattr(rv, "_READ_BUF", 16 * 1024)
    monkeypatch.setattr(rv, "BIG_READ_BUF", 16 * 1024)
    rv.STATE.update({"results": [], "games_list": [], "roms_path": "", "scan_id": ""})
    return rv.app.test_client()


@pytest.fixture
def setup(tmp_path, client):
    """6 « ISO » : 2 au bon nom (nom + taille), 4 mal nommés (lecture complète)."""
    datas = [bytes([65 + i]) * SIZE for i in range(6)]
    games = "".join(
        f'<game name="G{i} (USA)"><rom name="G{i} (USA).iso" size="{SIZE}" crc="{crc(d)}"/></game>'
        for i, d in enumerate(datas))
    dat = tmp_path / "x.dat"
    dat.write_text(f'<?xml version="1.0"?><datafile><header><name>X</name></header>{games}</datafile>')
    assert client.post("/api/load_dat", json={"path": str(dat)}).get_json()["ok"]
    roms = tmp_path / "roms"
    roms.mkdir()
    for i, d in enumerate(datas):
        (roms / (f"G{i} (USA).iso" if i < 2 else f"wrong{i}.iso")).write_bytes(d)
    return roms


def slow_reads(monkeypatch, delay=0.002):
    real = rv.SCAN_PROGRESS.file_bytes
    peak = [0]

    def slow(path, n):
        real(path, n)
        with rv.SCAN_PROGRESS.lock:
            peak[0] = max(peak[0], len(rv.SCAN_PROGRESS.active))
        time.sleep(delay)
    monkeypatch.setattr(rv.SCAN_PROGRESS, "file_bytes", slow)
    return peak


def counts(client):
    return client.get("/api/scan/progress").get_json()["counts"]


def test_progress_counts_after_scan(client, setup):
    # mode rapide : 2 identifiés par nom + taille, 4 non identifiés (même taille, autre nom) jamais lus
    r = client.post("/api/scan", json={"path": str(setup)}).get_json()
    assert r["ok"]
    assert client.get("/api/scan/progress").get_json()["running"] is False
    assert counts(client) == {"quick": 2, "cached": 0, "hashed": 0, "skipped": 4}
    # contrôle bit-exact : tout est lu…
    client.post("/api/scan", json={"path": str(setup), "quick_id": False})
    assert counts(client) == {"quick": 0, "cached": 0, "hashed": 6, "skipped": 0}
    # … puis repris du cache, en mode rapide comme en bit-exact
    client.post("/api/scan", json={"path": str(setup), "quick_id": False})
    assert counts(client) == {"quick": 0, "cached": 6, "hashed": 0, "skipped": 0}
    client.post("/api/scan", json={"path": str(setup)})
    assert counts(client) == {"quick": 0, "cached": 6, "hashed": 0, "skipped": 0}


def test_progress_visible_during_scan_and_big_reads_limited(client, setup, monkeypatch):
    peak = slow_reads(monkeypatch)
    out = {}
    th = threading.Thread(target=lambda: out.update(
        client.post("/api/scan", json={"path": str(setup), "quick_id": False}).get_json()))
    th.start()
    seen = None
    for _ in range(500):
        p = client.get("/api/scan/progress").get_json()
        if p["running"] and p["phase"] == "hash" and p["bytes_done"] > 0 and p["active"]:
            seen = p
            break
        time.sleep(0.005)
    th.join(30)
    assert out.get("ok"), out
    assert seen, "avancement jamais observé pendant la lecture"
    assert seen["bytes_total"] == 6 * SIZE and seen["total"] == 6
    assert seen["active"][0]["size"] == SIZE
    assert peak[0] <= rv.BIG_READ_PARALLEL


def test_cancel_stops_scan(client, setup, monkeypatch):
    real_file_bytes = rv.SCAN_PROGRESS.file_bytes
    slow_reads(monkeypatch, delay=0.01)
    out = {}
    th = threading.Thread(target=lambda: out.update(
        client.post("/api/scan", json={"path": str(setup), "quick_id": False}).get_json()))
    th.start()
    for _ in range(500):
        p = client.get("/api/scan/progress").get_json()
        if p["phase"] == "hash" and p["bytes_done"] > 0:
            break
        time.sleep(0.005)
    assert client.post("/api/scan/cancel").get_json()["running"] is True
    th.join(30)
    assert out.get("cancelled") is True and "error" in out
    assert rv.STATE["results"] == []
    # le scan suivant n'est pas « annulé » d'office
    monkeypatch.setattr(rv.SCAN_PROGRESS, "file_bytes", real_file_bytes)
    r = client.post("/api/scan", json={"path": str(setup)}).get_json()
    assert r.get("ok"), r


def test_cancel_without_scan_is_harmless(client):
    assert client.post("/api/scan/cancel").get_json()["running"] is False
    rv.SCAN_PROGRESS.check()  # ne lève pas
