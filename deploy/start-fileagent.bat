@echo off
rem FileAgent launcher (Windows). ASCII-only on purpose: .bat files are read with the
rem OEM code page, so Chinese comments here would show up as mojibake.
rem Docs: AGENTS.md section 2 / 2.1
rem
rem   start-fileagent.bat            backend :8000 + frontend :5241
rem   start-fileagent.bat https      also start Caddy on :8443 (needs caddy on PATH)

setlocal
set "ROOT=%~dp0.."

start "FileAgent backend" /D "%ROOT%\server" cmd /k "uv run python run_backend.py"
start "FileAgent frontend" /D "%ROOT%\client" cmd /k "npm run dev"

if /I "%1"=="https" (
	where caddy >nul 2>nul
	if errorlevel 1 (
		echo [warn] caddy not found on PATH, skip HTTPS entry. Install: winget install CaddyServer.Caddy
	) else (
		start "FileAgent caddy" /D "%ROOT%" cmd /k "caddy run --config deploy\Caddyfile --adapter caddyfile"
	)
)

echo.
echo Local      : http://localhost:5241
echo LAN visitors: http://<this-machine-ip>:5241   (firewall rule for 5241 must exist)
echo HTTPS       : https://<this-machine-ip>:8443  (only when started with the "https" argument)
endlocal
