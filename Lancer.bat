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
REM On teste chaque candidat en l'EXECUTANT : "where python" trouve aussi le faux
REM python.exe du Microsoft Store, qui ne fait qu'ouvrir le Store.
REM Pas de %%errorlevel%% dans un bloc ( ... ) : il y serait evalue trop tot.
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (python --version >nul 2>&1 && set "PY=python")
if not defined PY (python3 --version >nul 2>&1 && set "PY=python3")
if not defined PY (
  echo  [ERREUR] Python 3 introuvable.
  echo  Installez Python 3.10 ou plus recent depuis https://www.python.org/downloads/
  echo  et cochez "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
  echo  [ERREUR] Python 3.10 ou plus recent est requis.
  %PY% --version
  echo  Installez une version recente depuis https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

for /f "delims=" %%V in ('%PY% --version 2^>^&1') do echo  [OK] %%V ^(%PY%^)
%PY% -c "import flask,lxml" >nul 2>&1
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
REM Optionnel : lecture des archives .7z
%PY% -c "import py7zr" >nul 2>&1
if errorlevel 1 (
  echo  Installation de py7zr ^(lecture des .7z^)...
  %PY% -m pip install --user py7zr >nul 2>&1
  if errorlevel 1 echo  [!] py7zr non installe : les archives .7z ne seront pas lues.
)
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
