@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Mise a jour GitHub RomSet Verifier...
git status
git add -A
git status
git commit -m "About King Kraran, favicon, icons, tools, i18n"
git push origin main
if errorlevel 1 (
  echo.
  echo Si demande de login: connecte-toi a GitHub dans le navigateur.
  pause
)
pause
