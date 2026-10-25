"""
Tests : case « ISO nom + taille » — gros fichiers identifiés sans lecture (nom exact,
nom assoupli, début de nom, taille unique), jamais lus en mode rapide même non identifiés.

    pytest tests/
"""
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rom_verifier as rv  # noqa: E402

BIG = 4096
GAMES = [
    "Halo 3 (USA)",
    "Halo 3 - ODST (USA, Europe)",
    "Tom Clancy's Splinter Cell - Double Agent (Europe) (En,Fr,De,Es,It)",
    "Game Collection Vol 1 (USA)",
    "Game Collection Vol 2 (USA)",
]


def crc(b: bytes) -> str:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08x}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(rv, "QUICK_MATCH_MIN_SIZE", BIG)
    monkeypatch.setattr(rv, "BIG_READ_MIN_SIZE", BIG)
    rv.STATE.update({"results": [], "games_list": [], "roms_path": "", "scan_id": ""})
    return rv.app.test_client()


@pytest.fixture
def setup(tmp_path, client):
    """Comme sur Xbox 360 : toutes les images ont la même taille (le filtre taille ne sert à rien)."""
    datas = {g: bytes([65 + i]) * (BIG * 2) for i, g in enumerate(GAMES)}
    datas["Unique (Japan)"] = b"U" * (BIG * 3)  # seule entrée de cette taille
    games = "".join(
        f'<game name="{g.replace(chr(39), "&apos;")}"><rom name="{g.replace(chr(39), "&apos;")}.iso" '
        f'size="{len(d)}" crc="{crc(d)}"/></game>'
        for g, d in datas.items())
    dat = tmp_path / "x360.dat"
    dat.write_text(f'<?xml version="1.0"?><datafile><header><name>Microsoft - Xbox 360</name></header>{games}</datafile>',
                   encoding="utf-8")
    assert client.post("/api/load_dat", json={"path": str(dat)}).get_json()["ok"]
    roms = tmp_path / "roms"
    roms.mkdir()
    return roms, datas


@pytest.fixture
def no_reads(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("un gros fichier a été lu en mode rapide")
    monkeypatch.setattr(rv, "_crc32_path", boom)
    monkeypatch.setattr(rv, "_sha1_path", boom)


def scan(client, roms, **extra):
    r = client.post("/api/scan", json={"path": str(roms), **extra}).get_json()
    assert r.get("ok"), r
    return r["results"]


def by_found(res, name):
    return next(x for x in res if x["found"] == name)


def test_relaxed_name(client, setup, no_reads):
    roms, d = setup
    (roms / "halo 3 – odst (usa, europe).iso").write_bytes(d["Halo 3 - ODST (USA, Europe)"])
    r = by_found(scan(client, roms), "halo 3 – odst (usa, europe).iso")
    assert r["game"] == "Halo 3 - ODST (USA, Europe)" and r["status"] == "rename" and r["quick"]
    assert "approchant" in r["message"]


def test_truncated_name_prefix(client, setup, no_reads):
    roms, d = setup
    g = "Tom Clancy's Splinter Cell - Double Agent (Europe) (En,Fr,De,Es,It)"
    (roms / "Tom Clancys Splinter Cell - Double Agent (Euro.iso").write_bytes(d[g])
    r = by_found(scan(client, roms), "Tom Clancys Splinter Cell - Double Agent (Euro.iso")
    assert r["game"] == g and r["status"] == "rename" and "début de nom" in r["message"]


def test_unique_size(client, setup, no_reads):
    roms, d = setup
    (roms / "whatever.iso").write_bytes(d["Unique (Japan)"])
    r = by_found(scan(client, roms), "whatever.iso")
    assert r["game"] == "Unique (Japan)" and "taille unique" in r["message"]


def test_ambiguous_names_stay_unidentified_and_unread(client, setup, no_reads):
    roms, d = setup
    (roms / "Game Collection Vol.iso").write_bytes(d["Game Collection Vol 1 (USA)"])  # préfixe de 2 entrées
    (roms / "Halo.iso").write_bytes(d["Halo 3 (USA)"])  # trop court, même taille que 4 autres
    res = scan(client, roms)
    for name in ("Game Collection Vol.iso", "Halo.iso"):
        r = by_found(res, name)
        assert r["status"] == "bad" and "non identifié" in r["message"] and r["crc"] == ""
    # jamais lus → exclus de « Supprimer tous les Bad »
    rows = [x for x in rv.STATE["results"] if x.get("unidentified")]
    assert len(rows) == 2 and not any(rv._bulk_deletable(x) for x in rows)


def test_quick_id_off_reads_and_identifies_by_crc(client, setup):
    roms, d = setup
    (roms / "Halo.iso").write_bytes(d["Halo 3 (USA)"])
    r = by_found(scan(client, roms, quick_id=False), "Halo.iso")
    assert r["game"] == "Halo 3 (USA)" and r["status"] == "rename" and not r["quick"]


def test_deep_reads_too(client, setup):
    roms, d = setup
    (roms / "Halo 3 (USA).iso").write_bytes(b"X" * (BIG * 2))  # bon nom, contenu faux
    assert by_found(scan(client, roms), "Halo 3 (USA).iso")["status"] == "good"
    assert by_found(scan(client, roms, deep=True), "Halo 3 (USA).iso")["status"] == "bad"


@pytest.mark.parametrize("a,b", [
    ("Tom Clancy's Splinter Cell", "tom clancys splinter cell"),
    ("Halo 3 – ODST.iso", "halo 3 odst"),
    ("Pokémon_Stadium", "pokemon stadium"),
])
def test_relax_name(a, b):
    assert rv._relax_name(a) == b
