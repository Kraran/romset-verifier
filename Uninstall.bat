@echo off
chcp 65001 >nul
setlocal
set "APPDIR=%~dp0"
set "SELF=%~f0"
set "SELFNAME=%~nx0"
echo.
echo  Desinstallation de RomSet Verifier
echo  Dossier : %APPDIR%
echo.
echo  Attention : ce dossier peut contenir VOS fichiers :
echo    roms\      ROMs placees dans le dossier par defaut (et sa corbeille .rsv_trash)
echo    dat\       DAT telecharges ou generes
echo    profiles\  profils de collection
echo.
choice /C ON /M "Desinstaller RomSet Verifier"
if errorlevel 2 exit /b 0

choice /C ON /M "Conserver roms\, dat\ et profiles\ (recommande)"
if errorlevel 2 (set "KEEP=0") else (set "KEEP=1")

if "%KEEP%"=="0" (
  echo.
  echo  TOUT sera supprime, y compris roms\, dat\ et profiles\.
  choice /C ON /M "Confirmer la suppression complete"
  if errorlevel 2 set "KEEP=1"
)

REM Raccourcis
del /f /q "%USERPROFILE%\Desktop\RomSet Verifier.lnk" 2>nul
del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\RomSet Verifier\RomSet Verifier.lnk" 2>nul
rmdir /s /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\RomSet Verifier" 2>nul

REM Sortir du dossier avant de le vider
cd /d "%TEMP%"

if "%KEEP%"=="1" (
  REM Dossiers de l'application et caches, sauf les donnees utilisateur
  for /d %%D in ("%APPDIR%*") do (
    if /i not "%%~nxD"=="roms" if /i not "%%~nxD"=="dat" if /i not "%%~nxD"=="profiles" rmdir /s /q "%%D"
  )
  REM Fichiers de l'application, sauf ce script qui est supprime en dernier
  for %%F in ("%APPDIR%*") do (
    if /i not "%%~nxF"=="%SELFNAME%" del /f /q "%%F"
  )
  echo.
  echo  Termine. Vos fichiers sont conserves dans :
  echo    "%APPDIR%"
  echo.
  pause
  REM Supprime ce script une fois termine
  (goto) 2>nul & del /f /q "%SELF%"
)

echo.
echo  Termine.
pause
REM Supprime tout le dossier, y compris ce script, une fois termine
(goto) 2>nul & rmdir /s /q "%APPDIR%"
