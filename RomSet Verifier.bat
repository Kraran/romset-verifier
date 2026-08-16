@echo off
chcp 65001 >nul
cd /d "%~dp0"
title RomSet Verifier 1.0.0-beta
set VENV_PY=%~dp0venv\Scripts\python.exe
if not exist "%VENV_PY%" (
  echo [ERREUR] venv introuvable. Relance l'installateur.
  pause
  exit /b 1
)
"%VENV_PY%" -c "import flask,lxml" 2>nul
if errorlevel 1 (
  echo Reparation des dependances...
  "%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
)
echo.
echo  Demarrage de RomSet Verifier...
echo  URL : http://127.0.0.1:8080
echo  Fermez cette fenetre pour arreter.
echo.
"%VENV_PY%" "%~dp0rom_verifier.py" --open
if errorlevel 1 pause
