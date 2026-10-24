@echo off

setlocal
cd /d "%~dp0"
set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
	echo Creating virtual environment...
	python -m venv .venv
	if errorlevel 1 (
		echo Failed to create the virtual environment.
		pause
		exit /b 1
	)

	call ".venv\Scripts\activate.bat"
	echo Installing project dependencies...
	"%VENV_PYTHON%" -m pip install -e ".[dev]"
	if errorlevel 1 (
		echo Failed to install project dependencies.
		pause
		exit /b 1
	)
) else (
	call ".venv\Scripts\activate.bat"
)

rem --dev turns on auto-reload (an extra watcher process, ~20 MB); production runs without it.
set "RELOAD="
if /i "%~1"=="--dev" set "RELOAD=--reload"

echo Starting OctoFlux...
"%VENV_PYTHON%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log %RELOAD%

pause