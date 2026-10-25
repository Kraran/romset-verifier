"""
Tests : Redump depuis redump.org (source officielle), import d'un pack téléchargé à la main
(No-Intro DAT-o-MATIC), date du miroir. Aucun accès réseau.

    pytest tests/
"""
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rom_verifier as rv  # noqa: E402

DOWNLOADS_HTML = """<table>
<tr><td>Sony PlayStation</td><td><a href="/cues/psx/">Cuesheets</a></td><td><a href="/datfile/psx/">Datfile</a></td>
<td></td><td></td><td><a href="/datfile/psx-bios/">BIOS Datfile</a></td></tr>
<tr><td>Sony PlayStation 2</td><td></td><td><a href="/datfile/ps2/">Datfile</a></td></tr>
<tr><td>Sega Titan Video</td><td></td><td></td></tr>
</table>"""
PSX_NEW = "Sony - PlayStation - Datfile (10914) (2026-06-15 11-55-46).dat"
PSX_OLD = "Sony - PlayStation - Datfile (10800) (2025-01-01 10-00-00).dat"


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    dat, cache, prof = tmp_path / "dat", tmp_path / "cache", tmp_path / "profiles"
    for d in (dat, cache, prof):
        d.mkdir()
    monkeypatch.setattr(rv, "DEFAULT_DAT_DIR", dat)
    monkeypatch.setattr(rv, "CACHE_DIR", cache)
    monkeypatch.setattr(rv, "PROFILES_DIR", prof)
    rv._REDUMP_CATALOG_CACHE.update({"t": 0.0, "items": []})
    return dat, cache, prof


def zip_bytes(name: str, body: str = "<datafile/>") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, body)
    return buf.getvalue()


class Resp(io.BytesIO):
    def __init__(self, data: bytes, filename: str):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data)),
                        "Content-Disposition": f'attachment; filename="{filename}"'}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_official_catalog_parsing(dirs, monkeypatch):
    monkeypatch.setattr(rv, "_http_get", lambda url, timeout=30: DOWNLOADS_HTML.encode())
    items = rv.fetch_redump_official_catalog()
    assert [(i["name"], i["code"]) for i in items] == [
        ("Sony PlayStation", "psx"), ("Sony PlayStation (BIOS)", "psx-bios"), ("Sony PlayStation 2", "ps2")]


def test_official_download_replaces_old_version_and_profiles(dirs, monkeypatch):
    dat, _, prof = dirs
    target = dat / "redump"
    target.mkdir()
    (target / PSX_OLD).write_text("ancien")
    pf = prof / "p.json"
    pf.write_text(json.dumps({"id": "p", "systems": [{"id": "psx", "dat": str(target / PSX_OLD)}]}))
    seen = []

    def fake_urlopen(req, timeout=0):
        seen.append(req.full_url)
        return Resp(zip_bytes(PSX_NEW), PSX_NEW.replace(".dat", ".zip"))
    monkeypatch.setattr(rv.urllib.request, "urlopen", fake_urlopen)
    r = rv.download_redump_official("psx")
    assert seen == ["http://redump.org/datfile/psx/"]
    assert r["file"] == PSX_NEW and r["superseded"] == 1
    assert [p.name for p in target.iterdir()] == [PSX_NEW]
    assert json.loads(pf.read_text())["systems"][0]["dat"] == str(target / PSX_NEW)


def test_official_download_plain_dat_and_bad_code(dirs, monkeypatch):
    dat, _, _ = dirs
    bios = "Sony - PlayStation - BIOS Datfile (24) (2016-10-21).dat"
    monkeypatch.setattr(rv.urllib.request, "urlopen", lambda req, timeout=0: Resp(b"<datafile/>", bios))
    assert rv.download_redump_official("psx-bios")["file"] == bios
    with pytest.raises(ValueError):
        rv.download_redump_official("../../etc")


def test_local_status_and_bios_not_suggested_for_system(dirs):
    dat, _, _ = dirs
    target = dat / "redump"
    target.mkdir()
    (target / PSX_NEW).write_text("x")
    (target / "Sony - PlayStation - BIOS Datfile (24) (2026-12-31).dat").write_text("x")  # plus « récent »
    st = rv._redump_local_status(target, {"name": "Sony PlayStation", "code": "psx"})
    assert st == ("installed", PSX_NEW)
    assert rv._redump_local_status(target, {"name": "Sony PlayStation 2", "code": "ps2"}) == ("", "")
    assert rv.suggest_dat_for_system("psx")["name"] == PSX_NEW


def test_catalog_api_falls_back_to_mirror(dirs, monkeypatch):
    def fake_get(url, timeout=60):
        if "redump.org" in url:
            raise OSError("hors ligne")
        return b"<clrmamepro><datfile><name>Sony - PlayStation</name><version>1</version><file>a.dat</file></datfile></clrmamepro>"
    monkeypatch.setattr(rv, "_http_get", fake_get)
    r = rv.app.test_client().get("/api/dats/catalog?pack=redump").get_json()
    assert r["ok"] and r["source"] == "mirror" and "injoignable" in r["note"]


def test_mirror_latest_date(dirs, monkeypatch):
    xml = (b"<clrmamepro><datfile><name>A</name><version>20260707-013717</version><file>A (20260707-013717).dat</file></datfile>"
           b"<datfile><name>B</name><version>20250101-000000</version><file>B (20250101-000000).dat</file></datfile></clrmamepro>")
    monkeypatch.setattr(rv, "_http_get", lambda url, timeout=60: xml)
    r = rv.app.test_client().get("/api/dats/catalog?pack=nointro").get_json()
    assert r["source"] == "mirror" and r["mirror_latest"] == "2026-07-07"


def test_import_daily_pack(dirs, tmp_path):
    dat, _, _ = dirs
    target = dat / "nointro"
    target.mkdir()
    (target / "Nintendo - Game Boy (20260707-013717).dat").write_text("miroir")
    pack = tmp_path / "No-Intro Love Pack (PC XML) (2026-10-25).zip"
    with zipfile.ZipFile(pack, "w") as z:
        z.writestr("No-Intro/Nintendo - Game Boy (20261024-101010).dat", "<datafile/>")
        z.writestr("No-Intro/Nintendo - Game Boy Color (20261024-101010).dat", "<datafile/>")
        z.writestr("Source Code/Source Code - Nintendo - Game Boy (20261020-000000).dat", "<datafile/>")
        z.writestr("readme.txt", "x")
        z.writestr("../evil.dat", "x")
    r = rv.app.test_client().post("/api/dats/import", json={"path": str(pack), "pack": "nointro"}).get_json()
    assert r["ok"] and r["extracted"] == 3 and r["superseded"] == 1
    assert sorted(p.name for p in target.iterdir()) == [
        "Nintendo - Game Boy (20261024-101010).dat",
        "Nintendo - Game Boy Color (20261024-101010).dat",
        "Source Code - Nintendo - Game Boy (20261020-000000).dat",
    ]


def test_import_single_dat_and_errors(dirs, tmp_path):
    dat, _, _ = dirs
    one = tmp_path / "Sega - 32X (20261024-101010).dat"
    one.write_text("<datafile/>")
    assert rv.import_dat_pack(one, "nointro")["extracted"] == 1
    assert (dat / "nointro" / one.name).is_file()
    bad = tmp_path / "x.txt"
    bad.write_text("x")
    with pytest.raises(ValueError):
        rv.import_dat_pack(bad, "nointro")


# --- Type de DAT -------------------------------------------------------------------

def _dat(tmp_path, name, header, games):
    p = tmp_path / name
    p.write_text(f'<?xml version="1.0"?><datafile><header>{header}</header>{games}</datafile>')
    return p


def cd_games(n=5):
    return "".join(
        f'<game name="G{i}"><rom name="G{i}.cue" size="100" crc="0000000{i}"/>'
        f'<rom name="G{i} (Track 1).bin" size="1000" crc="1000000{i}"/>'
        f'<rom name="G{i} (Track 2).bin" size="1000" crc="2000000{i}"/></game>' for i in range(n))


def test_redump_cue_bin_dat_is_not_arcade(tmp_path):
    p = _dat(tmp_path, "psx.dat", "<name>Sony - PlayStation</name><author>redump.org</author>", cd_games())
    assert rv._probe_dat_kind(p) == "standard"
    # sans en-tête Redump : les pistes .cue/.bin suffisent
    p2 = _dat(tmp_path, "x.dat", "<name>X</name>", cd_games())
    assert rv._probe_dat_kind(p2) == "standard"


def test_fbneo_multi_rom_dat_stays_arcade(tmp_path):
    games = "".join(
        f'<game name="g{i}"><rom name="g{i}.p1" size="10" crc="0000000{i}"/>'
        f'<rom name="g{i}.s1" size="10" crc="1000000{i}"/><rom name="g{i}.c1" size="10" crc="2000000{i}"/></game>'
        for i in range(5))
    p = _dat(tmp_path, "FinalBurn Neo (ClrMame Pro XML, Arcade only).dat", "<name>FinalBurn Neo</name><author>FinalBurn Neo</author>", games)
    assert rv._probe_dat_kind(p) == "arcade"
