@echo off
rem Build the single-file Windows executable. Run ON Windows.
rem PyInstaller cannot cross-compile: the .exe must be built on Windows.
rem
rem The pure-python data modules listed in EXTRA_MODULES are passed as explicit
rem --hidden-import flags so the bundle is correct however
rem vaudeville_configurator.py imports them (top level, inside a
rem "try: ... except ImportError:" fallback, or lazily inside a function).
rem The generated .spec is not a sync peer; tools/build_package.sh and
rem .forgejo/workflows/release.yml carry the same list.
setlocal EnableDelayedExpansion
pushd "%~dp0.."
set EXTRA_MODULES=vaudeville_cast vaudeville_help
if not exist .venv-build\Scripts\pyinstaller.exe (
  py -3 -m venv .venv-build 2>nul || python -m venv .venv-build
  .venv-build\Scripts\python -m pip install --upgrade pip
  .venv-build\Scripts\python -m pip install pyinstaller
)
set "HIDDEN="
for %%m in (%EXTRA_MODULES%) do set "HIDDEN=!HIDDEN! --hidden-import %%m"
.venv-build\Scripts\pyinstaller --onefile --clean --name VaudevilleConfigurator !HIDDEN! vaudeville_configurator.py
if errorlevel 1 (
  echo ### ERROR: pyinstaller failed
  popd
  endlocal
  exit /b 1
)
rem Prove the data modules really landed inside the .exe.
set "BUNDLE_LIST=%TEMP%\vaudeville-bundle-list.txt"
.venv-build\Scripts\python -m PyInstaller.utils.cliutils.archive_viewer -l -r -b dist\VaudevilleConfigurator.exe > "!BUNDLE_LIST!" 2>nul
for %%m in (%EXTRA_MODULES%) do (
  if exist %%m.py (
    findstr /r /c:"^ *%%m$" "!BUNDLE_LIST!" >nul 2>&1
    if errorlevel 1 (
      echo ### ERROR: %%m is missing from dist\VaudevilleConfigurator.exe
      popd
      endlocal
      exit /b 1
    )
    echo ### bundle contains %%m
  ) else (
    echo ### note: %%m.py not in this checkout; skipping its bundle assertion
  )
)
echo ### built: %cd%\dist\VaudevilleConfigurator.exe
popd
endlocal
