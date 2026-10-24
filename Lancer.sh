#!/usr/bin/env bash
# RomSet Verifier — lanceur Linux / macOS
set -e
cd "$(dirname "$0")"

echo ""
echo "  ========================================"
echo "   RomSet Verifier"
echo "  ========================================"
echo ""

# Pas d'ajout au PYTHONPATH : mélanger les site-packages de plusieurs versions
# de Python fait charger un lxml compilé pour une autre version.

if ! command -v python3 >/dev/null 2>&1; then
  echo "  [ERREUR] Python 3 introuvable"
  exit 1
fi
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"; then
  echo "  [ERREUR] Python 3.10 ou plus récent est requis ($(python3 --version 2>&1))"
  exit 1
fi
echo "  [OK] $(python3 --version 2>&1)"

if ! python3 -c "import flask, lxml" 2>/dev/null; then
  echo "  Installation de Flask et lxml…"
  # Même interpréteur que celui qui lancera l'application (pas « pip3 », qui peut viser un autre Python)
  if ! python3 -m pip install --user flask lxml; then
    echo ""
    echo "  [ERREUR] Installation impossible. Sur les distributions qui interdisent pip --user"
    echo "  (« externally-managed-environment »), utilisez un environnement virtuel :"
    echo "    python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt"
    echo "    python rom_verifier.py --open"
    exit 1
  fi
fi
echo "  [OK] Flask + lxml"
echo ""
echo "  Démarrage — fenêtre application si Chrome / Chromium / Edge / Brave."
echo "  Fermez la fenêtre, Ctrl+C ou ⏻ Quitter pour arrêter."
echo ""
exec python3 rom_verifier.py --open
