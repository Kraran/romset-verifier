import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rom_verifier as rv  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_hash_cache(tmp_path, monkeypatch):
    """Chaque test a son propre cache de CRC (jamais celui de l'application)."""
    monkeypatch.setattr(rv, "HASH_CACHE", rv._HashCache(tmp_path / "hash_cache.json"))
