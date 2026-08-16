@echo off
chcp 65001 >nul
cd /d "%~dp0"
title RomSet Verifier

echo.
echo  ========================================
echo   RomSet Verifier 1.0.0-beta
echo  ========================================
echo.

REM ---- Python ----
set PY=
where python >nul 2>&1
if %errorlevel%==0 set PY=python
if not defined PY (
  where py >nul 2>&1
  if %errorlevel%==0 set PY=py -3
)
if not defined PY (
  echo  [ERREUR] Python introuvable.
  echo  Installez Python 3 depuis https://www.python.org/downloads/
  echo  Cochez "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

echo  [OK] Python trouve
%PY% -c "import flask,lxml" 2>nul
if errorlevel 1 (
  echo  Installation de Flask et lxml...
  %PY% -m pip install --user flask lxml
  if errorlevel 1 (
    echo  [ERREUR] Echec installation flask/lxml
    pause
    exit /b 1
  )
)
echo  [OK] Flask + lxml
echo.

REM ---- Node / Electron (optionnel) ----
where node >nul 2>&1
if errorlevel 1 goto BROWSER
where npm >nul 2>&1
if errorlevel 1 goto BROWSER

echo  [OK] Node.js trouve - mode Electron
if not exist "node_modules\electron" (
  echo  Installation d'Electron (premiere fois)...
  call npm install
  if errorlevel 1 (
    echo  [!] npm install a echoue - mode navigateur
    goto BROWSER
  )
)
echo.
echo  Demarrage de l'application native...
echo.
call npx electron .
goto FIN

:BROWSER
echo  [!] Node.js absent - mode navigateur
echo.
echo  Pour Electron plus tard, installez Node.js LTS :
echo    https://nodejs.org/
echo  (cochez Add to PATH, puis relancez ce fichier)
echo.
echo  Demarrage du serveur local...
echo  URL : http://127.0.0.1:8080
echo  Fermez cette fenetre pour arreter.
echo.
%PY% rom_verifier.py --open
if errorlevel 1 (
  echo.
  echo  [ERREUR] Le serveur s'est arrete avec une erreur.
  pause
  exit /b 1
)
echo.
echo  Serveur arrete.
exit /b 0

:FIN
echo.
pause
