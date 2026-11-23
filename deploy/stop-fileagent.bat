@echo off
rem Stop FileAgent: whatever is listening on 8000 / 5241 / 8443, plus caddy.exe.
rem It matches by port, not by window title, so it still works if you started the
rem services by hand instead of via start-fileagent.bat.
rem The Postgres container is left running on purpose - stopping the database is a
rem separate decision (docker stop fileagent-postgres).

powershell -NoProfile -Command "foreach ($p in 8000,5241,8443) { $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue; if ($c) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue; Write-Host ('stopped port ' + $p + ' pid ' + $c.OwningProcess) } else { Write-Host ('port ' + $p + ' not listening') } }; $k = Get-Process caddy -ErrorAction SilentlyContinue; if ($k) { $k | Stop-Process -Force; Write-Host 'stopped caddy' } else { Write-Host 'caddy not running' }"

rem start-fileagent.bat opens cmd /k windows that stay put after the service dies;
rem close them here so "stop" really means "everything is gone".
taskkill /FI "WINDOWTITLE eq FileAgent*" /F >nul 2>nul

pause
