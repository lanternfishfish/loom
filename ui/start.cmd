@echo off
rem Loom Web - double-click to open the panel.
rem Extra arguments are passed through, e.g.   start.cmd --port 9000
rem Delete this file if you prefer the terminal.

setlocal
cd /d "%~dp0.."

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo [loom] Python not found in PATH.
  echo [loom] Install Python 3.9 or newer, then run this again.
  pause
  exit /b 1
)

if not exist "work" (
  echo [loom] This folder has no work\ yet.
  echo [loom] The page will offer a "New" button to create one.
)

echo [loom] starting the panel - close this window to stop it.
echo.
%PY% "ui\loom_web.py" --workdir "work" --open %*

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [loom] exited with code %RC%
  pause
)
endlocal
