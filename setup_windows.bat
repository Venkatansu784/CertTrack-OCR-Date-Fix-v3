@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher "py" was not found. Install Python 3.10+ and enable Add Python to PATH.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating the project virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 goto :error
)
echo Installing dependencies into this project's virtual environment...
".venv\Scripts\python.exe" -m ensurepip --upgrade
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :error
echo.
echo Python dependencies installed successfully.
echo Tesseract OCR must also be installed separately.
echo Verify it with: tesseract --version
echo Then run run_windows.bat to launch CertTrack.
pause
exit /b 0
:error
echo Setup failed. If an earlier setup was cancelled, close this window, delete the .venv folder in this project directory, and run setup_windows.bat again.
pause
exit /b 1
