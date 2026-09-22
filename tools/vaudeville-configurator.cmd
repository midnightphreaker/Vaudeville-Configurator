@echo off
rem Vaudeville Configurator - Windows launcher.
rem Keep this file next to vaudeville_configurator.py, or put its folder on PATH.
setlocal
set "APP=%~dp0vaudeville_configurator.py"
if not exist "%APP%" set "APP=%~dp0..\vaudeville_configurator.py"
if not exist "%APP%" (
  echo vaudeville_configurator.py not found next to %~f0 >&2
  exit /b 2
)
where py >nul 2>nul
if %errorlevel%==0 ( py -3 "%APP%" %* ) else ( python "%APP%" %* )
endlocal
