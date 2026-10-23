@echo off
setlocal
where uv >nul 2>nul
if errorlevel 1 (
  echo uv was not found.
  echo Install uv first: https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)
uv sync
if errorlevel 1 (
  echo.
  echo Dependency setup failed. Check the error above.
  pause
  exit /b 1
)
if not exist .env copy .env.example .env >nul
echo.
echo Environment ready.
echo   Start the server:      uv run taskprinter serve
echo   Try without printer:   uv run taskprinter serve --mock
echo   Then open http://localhost:8080
pause
