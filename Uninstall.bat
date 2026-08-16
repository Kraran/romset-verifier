@echo off
chcp 65001 >nul
echo Desinstallation de RomSet Verifier
echo Dossier : %~dp0
echo.
choice /C ON /M "Supprimer ce dossier et les raccourcis"
if errorlevel 2 exit /b 0
del /f /q "%USERPROFILE%\Desktop\RomSet Verifier.lnk" 2>nul
del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\RomSet Verifier\RomSet Verifier.lnk" 2>nul
rmdir /s /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\RomSet Verifier" 2>nul
cd /d "%TEMP%"
rmdir /s /q "%~dp0"
echo Termine.
pause
