@echo off

set "PASTA=C:\Users\Tiago Souto\Desktop\telegram-extractor"
set "PYTHON=C:\Users\Tiago Souto\AppData\Local\Python\pythoncore-3.14-64\python.exe"
set "PIDFILE=%PASTA%\signal_bridge.pid"

for /f "tokens=1 delims=:" %%H in ("%time%") do set HORA=%%H
set /a HORA=1%HORA%-100

REM Janela permitida: 19:30 ate 09:10
REM Se hora >= 19 OU hora < 9, pode iniciar
REM Entre 09:00 e 18:59, nao inicia

if %HORA% GEQ 19 goto iniciar
if %HORA% LSS 9 goto iniciar

REM Se for exatamente 09h, so permite ate 09:10
if %HORA% EQU 9 (
    for /f "tokens=2 delims=:" %%M in ("%time%") do set MIN=%%M
    set /a MIN=1%MIN%-100
    if %MIN% LEQ 10 goto iniciar
)

exit /b 0

:iniciar
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