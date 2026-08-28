@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Build RomSet Verifier
echo ============================================================
echo  Build RomSet Verifier Windows .exe (PyInstaller)
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [ERREUR] Python introuvable dans le PATH.
  echo Installez Python 3 et cochez "Add python.exe to PATH".
  pause
  exit /b 1
)

echo [1/3] Installation des dependances de build...
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt pyinstaller
if errorlevel 1 (
  echo [ERREUR] pip install a echoue.
  pause
  exit /b 1
)

echo [2/3] Nettoyage des anciens builds...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [3/3] PyInstaller...
python -m PyInstaller --noconfirm RomSetVerifier.spec
if errorlevel 1 (
  echo [ERREUR] PyInstaller a echoue.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  OK - Executable :
echo    %cd%\dist\RomSetVerifier.exe
echo.
echo  Double-clic sur l'exe : fenetre application + serveur local.
echo  dat/, roms/, profiles/ et app-window/ sont crees a cote de l'exe.
echo ============================================================
if exist dist explorer dist
pause
