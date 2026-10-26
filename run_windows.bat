@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. First run setup_windows.bat.
  pause
  exit /b 1
)
rem Detect common Tesseract installer paths automatically for this launch.
if not defined TESSERACT_CMD if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" set "TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe"
if not defined TESSERACT_CMD if exist "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe" set "TESSERACT_CMD=C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"
if not defined TESSERACT_CMD if exist "%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe" set "TESSERACT_CMD=%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"
if defined TESSERACT_CMD echo Using Tesseract: %TESSERACT_CMD%
if not defined TESSERACT_CMD where tesseract.exe >nul 2>nul
if errorlevel 1 if not defined TESSERACT_CMD echo WARNING: Tesseract was not found. OCR scanning needs Tesseract installed; manual entry remains available.
rem Use port 5001 so an older CertTrack instance on port 5000 cannot shadow this version.
if not defined PORT set "PORT=5001"
echo Starting this CertTrack copy at http://127.0.0.1:%PORT%
".venv\Scripts\python.exe" app.py
pause
