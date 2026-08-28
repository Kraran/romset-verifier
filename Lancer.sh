#!/usr/bin/env bash
# RomSet Verifier — lanceur Linux / macOS
set -e
cd "$(dirname "$0")"

echo ""
echo "  ========================================"
echo "   RomSet Verifier"
echo "  ========================================"
echo ""

export PYTHONPATH="${HOME}/.local/lib/python3.12/site-packages:${HOME}/.local/lib/python3.11/site-packages:${HOME}/.local/lib/python3.10/site-packages:${PYTHONPATH:-}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "  [ERREUR] Python 3 introuvable"
  exit 1
fi
echo "  [OK] Python trouvé"

if ! python3 -c "import flask, lxml" 2>/dev/null; then
  echo "  Installation de Flask et lxml…"
  pip3 install --user flask lxml
fi
echo "  [OK] Flask + lxml"
echo ""
echo "  Démarrage — fenêtre application si Chrome / Chromium / Edge / Brave."
echo "  Fermez la fenêtre, Ctrl+C ou ⏻ Quitter pour arrêter."
echo ""
exec python3 rom_verifier.py --open
