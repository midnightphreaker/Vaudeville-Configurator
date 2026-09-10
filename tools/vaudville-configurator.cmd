@echo off
rem Vaudville Configurator - Windows launcher.
rem Keep this file next to vaudville_configurator.py, or put its folder on PATH.
setlocal
set "APP=%~dp0vaudville_configurator.py"
if not exist "%APP%" set "APP=%~dp0..\vaudville_configurator.py"
if not exist "%APP%" (
  echo vaudville_configurator.py not found next to %~f0 >&2
  exit /b 2
)
where py >nul 2>nul
if %errorlevel%==0 ( py -3 "%APP%" %* ) else ( python "%APP%" %* )
endlocal
