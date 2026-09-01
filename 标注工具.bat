@echo off
rem Anonymizer annotator launcher (lives inside the anonymizer folder; works anywhere).
rem Double-click: starts the UI, then pick an image dir via the open-dir button.
rem Drop an image folder onto this file: opens that folder directly.
cd /d "%~dp0"
if "%~1"=="" (
    python ui.py
) else (
    python ui.py "%~1"
)
if errorlevel 1 (
    echo.
    echo UI exited with an error. Check the traceback above.
    pause
)
