"""
Tests : mame -listxml (R4), cache des packs DAT (R5), versions FBNeo (R6).
Aucun accès réseau : les téléchargements sont simulés.

    pytest tests/
"""
import os
import stat
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rom_verifier as rv  # noqa: E402


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    dat, cache = tmp_path / "dat", tmp_path / "cache"
    dat.mkdir()
    cache.mkdir()
    monkeypatch.setattr(rv, "DEFAULT_DAT_DIR", dat)
    monkeypatch.setattr(rv, "CACHE_DIR", cache)
    return dat, cache


def mkpack(path: Path, names) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, f"<datafile><!-- {n} --></datafile>")


# --- R4 : mame -listxml ----------------------------------------------------------

def fake_mame(tmp_path: Path, body: str) -> Path:
    exe = tmp_path / "mame"
    exe.write_text(f"#!{sys.executable}\nimport sys\n{body}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


@pytest.mark.skipif(os.name == "nt", reason="faux exécutable POSIX")
def test_listxml_with_noisy_stderr_does_not_hang(tmp_path, dirs):
    exe = fake_mame(tmp_path, """
if "-help" in sys.argv:
    print("MAME v0.289"); sys.exit(0)
for i in range(20000):  # ~1 Mo d'avertissements : remplissait le tube stderr
    sys.stderr.write("warning: missing sample %d\\n" % i)
sys.stdout.write('<?xml version="1.0"?><mame build="0.289">' + "<machine name='x'/>" * 500 + "</mame>")
""")
    result = {}
    t = threading.Thread(target=lambda: result.update(rv.generate_mame_listxml_from_exe(exe)), daemon=True)
    t.start()
    t.join(30)
    assert not t.is_alive(), "mame -listxml bloqué"
    assert result["ok"] and Path(result["path"]).stat().st_size > 1000


@pytest.mark.skipif(os.name == "nt", reason="faux exécutable POSIX")
def test_listxml_failure_keeps_existing_dat(tmp_path, dirs):
    dat, _ = dirs
    exe = fake_mame(tmp_path, """
if "-help" in sys.argv:
    print("MAME v0.289"); sys.exit(0)
sys.stderr.write("fatal error"); sys.exit(1)
""")
    existing = dat / "mame" / "MAME 0.289.xml"
    existing.parent.mkdir(parents=True)
    existing.write_text("ancien DAT complet")
    with pytest.raises(RuntimeError, match="fatal error"):
        rv.generate_mame_listxml_from_exe(exe)
    assert existing.read_text() == "ancien DAT complet"
    assert not list(existing.parent.glob("*.part"))


# --- R5 : cache des packs -----------------------------------------------------------

def test_stale_cache_is_refreshed_when_dat_missing(dirs, monkeypatch):
    _, cache = dirs
    mkpack(cache / "nointro.zip", ["Nintendo - Game Boy (20250101).dat"])
    calls = []

    def fake_download(url, dest, timeout=600):
        calls.append(url)
        mkpack(dest, ["Nintendo - Game Boy (20261020).dat"])

    monkeypatch.setattr(rv, "_http_download", fake_download)
    r = rv.extract_one_from_pack("nointro", "Nintendo - Game Boy (20261020).dat")
    assert r["file"] == "Nintendo - Game Boy (20261020).dat" and r["refreshed"] is False
    assert len(calls) == 1
    assert Path(r["path"]).is_file()


def test_old_cache_is_redownloaded(dirs, monkeypatch):
    _, cache = dirs
    old = cache / "nointro.zip"
    mkpack(old, ["Sega - Mega Drive (20250101).dat"])
    past = time.time() - 3 * 24 * 3600
    os.utime(old, (past, past))
    calls = []
    monkeypatch.setattr(rv, "_http_download",
                        lambda url, dest, timeout=600: (calls.append(url), mkpack(dest, ["Sega - Mega Drive (20250101).dat"])))
    r = rv.extract_one_from_pack("nointro", "Sega - Mega Drive (20250101).dat")
    assert calls and r["refreshed"] is True


def test_no_fuzzy_match_on_similar_name(dirs, monkeypatch):
    _, cache = dirs
    names = ["Nintendo - Game Boy Advance (20261020).dat", "Nintendo - Game Boy Color (20261020).dat"]
    mkpack(cache / "nointro.zip", names)
    monkeypatch.setattr(rv, "_http_download", lambda url, dest, timeout=600: mkpack(dest, names))
    with pytest.raises(FileNotFoundError):
        rv.extract_one_from_pack("nointro", "Nintendo - Game Boy")


def test_fresh_cache_is_reused(dirs, monkeypatch):
    _, cache = dirs
    mkpack(cache / "redump.zip", ["Sony - PlayStation (20261020).dat"])
    monkeypatch.setattr(rv, "_http_download", lambda *a, **k: pytest.fail("téléchargement inutile"))
    r = rv.extract_one_from_pack("redump", "Sony - PlayStation (20261020).dat")
    assert Path(r["path"]).is_file()


# --- R6 : versions FBNeo -------------------------------------------------------------

def fake_fbneo_archive(url, dest, timeout=600):
    tag = url.rstrip("/").split("/")[-1].replace(".zip", "")
    with zipfile.ZipFile(dest, "w") as z:
        z.writestr(f"FBNeo-{tag}/dats/FinalBurn Neo (ClrMame Pro XML, Arcade only).dat", f"<datafile>{tag}</datafile>")


def test_older_fbneo_version_does_not_overwrite_latest(dirs, monkeypatch):
    dat, _ = dirs
    monkeypatch.setattr(rv, "_http_download", fake_fbneo_archive)
    latest = rv.download_fbneo_dat_version("latest")
    old = rv.download_fbneo_dat_version("tag:v1.0.0.2")
    name = "FinalBurn Neo (ClrMame Pro XML, Arcade only).dat"
    assert (dat / "fbneo" / name).read_text() == "<datafile>master</datafile>"
    assert (dat / "fbneo" / "versions" / "v1.0.0.2" / name).read_text() == "<datafile>v1.0.0.2</datafile>"
    assert latest["dir"] != old["dir"]


def test_suggestion_ignores_archived_versions(dirs, monkeypatch):
    dat, _ = dirs
    monkeypatch.setattr(rv, "_http_download", fake_fbneo_archive)
    rv.download_fbneo_dat_version("tag:v1.0.0.2")
    assert rv.suggest_dat_for_system("fbneo") == {}
    rv.download_fbneo_dat_version("latest")
    sug = rv.suggest_dat_for_system("fbneo")
    assert Path(sug["path"]).parent == dat / "fbneo"
