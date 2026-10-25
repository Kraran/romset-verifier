"""
Tests : ressources de l'interface (polices embarquées, icônes du sprite).

    pytest tests/
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import rom_verifier as rv  # noqa: E402

HTML = (ROOT / "_ui.html").read_text(encoding="utf-8")


def test_every_icon_used_is_in_the_sprite():
    defined = set(re.findall(r'<symbol id="i-([a-z0-9-]+)"', HTML))
    used = set(re.findall(r"#i-([a-z0-9-]+)", HTML)) | set(re.findall(r"ico\('([a-z0-9-]+)'\)", HTML))
    used |= set(re.findall(r"(?:ok|warn|err|info|miss): '([a-z0-9-]+)'", HTML))  # DETAIL_ICON
    assert sorted(used - defined) == []


def test_fonts_are_served_offline():
    c = rv.app.test_client()
    names = set(re.findall(r"/fonts/([a-z0-9-]+\.woff2)", HTML))
    assert names, "aucune police référencée"
    for n in names:
        r = c.get("/fonts/" + n)
        assert r.status_code == 200 and r.mimetype == "font/woff2", n
        assert "immutable" in r.headers.get("Cache-Control", "")


def test_font_route_rejects_other_files():
    c = rv.app.test_client()
    for bad in ("OFL-IBMPlexSans.txt", "..%2From_verifier.py", "x.woff2"):
        assert c.get("/fonts/" + bad).status_code == 404, bad
