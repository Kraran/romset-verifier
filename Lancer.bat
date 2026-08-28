@echo off
chcp 65001 >nul
cd /d "%~dp0"
title RomSet Verifier

echo.
echo  ========================================
echo   RomSet Verifier 1.1.0-beta
echo  ========================================
echo.

REM ---- Python ----
set "PY="
where python >nul 2>&1
if %errorlevel%==0 set "PY=python"
if not defined PY (
  where py >nul 2>&1
  if %errorlevel%==0 set "PY=py -3"
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
echo  Demarrage...
echo  Fenetre application (sans barre d'adresse) si Edge / Chrome / Brave.
echo  Fermez la fenetre ou Quitter pour arreter le serveur.
echo.

%PY% rom_verifier.py --open
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
  echo.
  echo  [ERREUR] Le serveur s'est arrete avec une erreur.
  pause
  exit /b %ERR%
)
exit /b 0
