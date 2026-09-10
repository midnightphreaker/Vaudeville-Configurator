@echo off
rem Build the single-file Windows executable. Run ON Windows.
rem PyInstaller cannot cross-compile: the .exe must be built on Windows.
setlocal
pushd "%~dp0.."
if not exist .venv-build\Scripts\pyinstaller.exe (
  py -3 -m venv .venv-build 2>nul || python -m venv .venv-build
  .venv-build\Scripts\python -m pip install --upgrade pip
  .venv-build\Scripts\python -m pip install pyinstaller
)
.venv-build\Scripts\pyinstaller --onefile --clean --name VaudvilleConfigurator vaudville_configurator.py
echo ### built: %cd%\dist\VaudvilleConfigurator.exe
popd
endlocal
