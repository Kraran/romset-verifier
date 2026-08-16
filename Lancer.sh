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

if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  echo "  [OK] Node.js trouvé — mode Electron natif"
  if [ ! -d node_modules/electron ]; then
    echo "  Installation d'Electron (première fois)…"
    npm install
  fi
  echo ""
  echo "  Démarrage de l'application native…"
  echo ""
  exec npx electron .
fi

echo "  [!] Node.js / npm non trouvés — mode navigateur"
echo ""
echo "  Pour Electron plus tard : installez Node.js LTS"
echo "    https://nodejs.org/"
echo ""
echo "  Démarrage du serveur local (http://127.0.0.1:8080)"
echo "  Ctrl+C pour arrêter."
echo ""
exec python3 rom_verifier.py --open
