@echo off

set "PASTA=C:\Users\Tiago Souto\Desktop\telegram-extractor"
set "PYTHON=C:\Users\Tiago Souto\AppData\Local\Python\pythoncore-3.14-64\python.exe"
set "PIDFILE=%PASTA%\signal_bridge.pid"

cd /d "%PASTA%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
"$pidFile='%PIDFILE%'; ^
if (Test-Path $pidFile) { ^
    $oldPid = Get-Content $pidFile -ErrorAction SilentlyContinue; ^
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) { ^
        exit 0 ^
    }; ^
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue ^
}; ^
$p = Start-Process -FilePath '%PYTHON%' -ArgumentList '-m','uvicorn','main:app','--host','127.0.0.1','--port','8000' -WorkingDirectory '%PASTA%' -WindowStyle Hidden -PassThru; ^
Set-Content -Path $pidFile -Value $p.Id"