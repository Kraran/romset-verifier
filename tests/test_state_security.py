"""
Tests de non-régression : verrou, résultats périmés et API locale (rapport R1, S1).

    pytest tests/
"""
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rom_verifier as rv  # noqa: E402

R = b"R" * 4096


def crc(b: bytes) -> str:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08x}"


@pytest.fixture
def client():
    rv.STATE.update({"results": [], "games_list": [], "roms_path": "", "scan_id": ""})
    return rv.app.test_client()


@pytest.fixture
def scanned(tmp_path, client):
    """Un DAT chargé + un scan avec une ROM valide et un fichier inconnu."""
    dat = tmp_path / "nes.dat"
    dat.write_text(f"""<?xml version="1.0"?><datafile>
<game name="Game (USA)"><rom name="Game (USA).nes" size="4096" crc="{crc(R)}"/></game></datafile>""")
    assert client.post("/api/load_dat", json={"path": str(dat)}).get_json()["ok"]
    roms = tmp_path / "roms"
    roms.mkdir()
    (roms / "Game (USA).nes").write_bytes(R)
    (roms / "unknown.nes").write_bytes(b"U" * 4096)
    data = client.post("/api/scan", json={"path": str(roms)}).get_json()
    assert data["ok"] and data["scan_id"]
    return roms, data


def idx_of(results, name):
    return next(i for i, x in enumerate(results) if x["found"] == name)


# --- R1 : verrou global --------------------------------------------------------

def test_action_refused_while_busy(client, scanned):
    _roms, data = scanned
    i = idx_of(data["results"], "unknown.nes")
    assert rv.STATE_LOCK.acquire(blocking=False)
    try:
        r = client.post("/api/scan", json={"path": str(_roms)}).get_json()
        assert r.get("busy")
        r = client.post("/api/load_dat", json={"path": "x"}).get_json()
        assert r.get("busy")
    finally:
        rv.STATE_LOCK.release()
    # libéré : l'action passe
    r = client.post("/api/delete", json={"index": i, "scan_id": data["scan_id"]}).get_json()
    assert r.get("ok"), r


# --- R1 : résultats périmés ------------------------------------------------------

def test_stale_scan_id_is_refused(client, scanned):
    roms, data = scanned
    i = idx_of(data["results"], "unknown.nes")
    old_id = data["scan_id"]
    client.post("/api/scan", json={"path": str(roms)})  # nouveau scan, nouvel id
    r = client.post("/api/delete", json={"index": i, "scan_id": old_id}).get_json()
    assert r.get("stale")
    assert (roms / "unknown.nes").exists()


def test_missing_scan_id_is_refused(client, scanned):
    roms, data = scanned
    i = idx_of(data["results"], "unknown.nes")
    for url, body in (
        ("/api/delete", {"index": i}),
        ("/api/delete_selected", {"indices": [i]}),
        ("/api/delete_bads", {}),
        ("/api/rename", {"index": i}),
        ("/api/repair", {"indices": [i]}),
        ("/api/fix_all", {}),
    ):
        assert client.post(url, json=body).get_json().get("stale"), url
    assert (roms / "unknown.nes").exists()


def test_load_dat_invalidates_results(tmp_path, client, scanned):
    roms, data = scanned
    i = idx_of(data["results"], "unknown.nes")
    dat2 = tmp_path / "other.dat"
    dat2.write_text('<?xml version="1.0"?><datafile><game name="X"><rom name="x.bin" size="1" crc="00000000"/></game></datafile>')
    new = client.post("/api/load_dat", json={"path": str(dat2)}).get_json()
    assert new["scan_id"] != data["scan_id"]
    r = client.post("/api/delete", json={"index": i, "scan_id": data["scan_id"]}).get_json()
    assert r.get("stale")


def test_row_path_mismatch_is_refused(client, scanned):
    roms, data = scanned
    i = idx_of(data["results"], "unknown.nes")
    r = client.post("/api/delete", json={
        "index": i, "scan_id": data["scan_id"], "path": str(roms / "Game (USA).nes"),
    }).get_json()
    assert r.get("stale")
    assert (roms / "unknown.nes").exists() and (roms / "Game (USA).nes").exists()
    r = client.post("/api/delete_selected", json={
        "indices": [i], "paths": [str(roms / "autre.nes")], "scan_id": data["scan_id"],
    }).get_json()
    assert r["deleted"] == 0 and (roms / "unknown.nes").exists()


def test_matching_path_is_accepted(client, scanned):
    roms, data = scanned
    i = idx_of(data["results"], "unknown.nes")
    r = client.post("/api/delete", json={
        "index": i, "scan_id": data["scan_id"], "path": data["results"][i]["path"],
    }).get_json()
    assert r.get("ok"), r
    assert not (roms / "unknown.nes").exists()


# --- S1 : API réservée à la machine locale ---------------------------------------

@pytest.mark.parametrize("host", ["evil.example", "evil.example:8080", "192.168.1.10:8080"])
def test_foreign_host_refused(client, host):
    r = client.get("/api/i18n", headers={"Host": host})
    assert r.status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8080", "localhost:8080", "[::1]:8080", "localhost"])
def test_local_host_accepted(client, host):
    assert client.get("/api/i18n", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://192.168.1.10:8080"])
def test_foreign_origin_refused_on_write(client, scanned, origin):
    roms, data = scanned
    i = idx_of(data["results"], "unknown.nes")
    r = client.post("/api/delete", json={"index": i, "scan_id": data["scan_id"]},
                    headers={"Origin": origin})
    assert r.status_code == 403
    assert (roms / "unknown.nes").exists()


def test_cross_site_fetch_refused(client):
    r = client.post("/api/shutdown", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_local_origin_accepted_on_write(client, scanned):
    _roms, data = scanned
    i = idx_of(data["results"], "unknown.nes")
    r = client.post("/api/delete", json={"index": i, "scan_id": data["scan_id"]},
                    headers={"Origin": "http://127.0.0.1:8080", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200 and r.get_json().get("ok")


def test_shutdown_get_not_allowed(client):
    assert client.get("/api/shutdown").status_code == 405
