@echo off

set "PASTA=C:\Users\Tiago Souto\Desktop\telegram-extractor"
set "PIDFILE=%PASTA%\signal_bridge.pid"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"$pidFile='%PIDFILE%'; ^
if (Test-Path $pidFile) { ^
    $processId = Get-Content $pidFile -ErrorAction SilentlyContinue; ^
    if ($processId) { ^
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue ^
    }; ^
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue ^
}"