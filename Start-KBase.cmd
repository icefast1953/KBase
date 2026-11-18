@echo off
setlocal
cd /d "%~dp0"
set "UV_CACHE_DIR=%CD%\.uv-cache"
set "UV_PYTHON_INSTALL_DIR=%CD%\.python"
set "KBASE_PORT=8765"
if /i "%~1"=="deepseek" (
    set "KBASE_CHAT_PROVIDER=deepseek"
    set "KBASE_PORT=8766"
)
echo Open http://127.0.0.1:%KBASE_PORT% after the server starts.
where uv >nul 2>&1
if not errorlevel 1 (
    uv run --managed-python --python 3.12 --locked uvicorn kbase.app:create_app --factory --host 127.0.0.1 --port %KBASE_PORT%
) else if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m uvicorn kbase.app:create_app --factory --host 127.0.0.1 --port %KBASE_PORT%
) else (
    echo Install uv and add it to PATH to start KBase.
    pause
    exit /b 1
)
if errorlevel 1 pause
