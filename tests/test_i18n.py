"""
Tests : cohérence des traductions (mêmes clés partout, mêmes {variables}, clés de l'UI définies)
et réponses JSON de l'API même en cas d'erreur.

    pytest tests/
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import rom_verifier as rv  # noqa: E402

LANGS = sorted(p.stem for p in (ROOT / "i18n").glob("*.json"))
EN = json.loads((ROOT / "i18n" / "en.json").read_text(encoding="utf-8"))


def load(lang):
    return json.loads((ROOT / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))


def placeholders(s: str):
    return sorted(re.findall(r"\{\w+\}", s))


@pytest.mark.parametrize("lang", LANGS)
def test_same_keys_and_placeholders(lang):
    d = load(lang)
    assert set(d) == set(EN), f"{lang}: clés manquantes {set(EN) - set(d)} / en trop {set(d) - set(EN)}"
    for k, v in d.items():
        assert placeholders(v) == placeholders(EN[k]), f"{lang}.{k}"


def test_every_ui_key_is_defined():
    html = (ROOT / "_ui.html").read_text(encoding="utf-8")
    used = set(re.findall(r"""\bt\(\s*['"]([a-zA-Z0-9_.]+)['"]""", html))
    used |= set(re.findall(r'data-i18n(?:-[a-z]+)?="([a-zA-Z0-9_.]+)"', html))
    assert sorted(k for k in used if k not in EN) == []


@pytest.mark.parametrize("url,kwargs,code", [
    ("/api/nexistepas", {}, 404),
    ("/api/shutdown", {}, 405),  # GET refusé
])
def test_api_errors_are_json(url, kwargs, code):
    r = rv.app.test_client().get(url, **kwargs)
    assert r.status_code == code
    assert "error" in r.get_json()


def test_non_json_body_does_not_crash():
    c = rv.app.test_client()
    for url in ("/api/scan", "/api/rename", "/api/delete", "/api/profiles/detect", "/api/dats/download_one"):
        r = c.post(url, data="pas du json", content_type="text/plain")
        assert r.status_code == 200 and "error" in r.get_json(), url
