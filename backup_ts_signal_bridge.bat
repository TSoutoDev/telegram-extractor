@echo off
setlocal enabledelayedexpansion

set "PG_DUMP=C:\Program Files\PostgreSQL\18\bin\pg_dump.exe"
set "DB_NAME=ts_signal_bridge"
set "DB_USER=postgres"
set "DB_HOST=localhost"
set "DB_PORT=5432"
set "BACKUP_DIR=G:\Meu Drive\Backup\Postgres"

if not exist "%BACKUP_DIR%" (
    mkdir "%BACKUP_DIR%"
)

for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set "dt=%%I"

set "YYYY=!dt:~0,4!"
set "MM=!dt:~4,2!"
set "DD=!dt:~6,2!"
set "HH=!dt:~8,2!"
set "MIN=!dt:~10,2!"

set "FILE_NAME=%DB_NAME%_!YYYY!-!MM!-!DD!_!HH!-!MIN!.backup"
set "FULL_PATH=%BACKUP_DIR%\!FILE_NAME!"

echo.
echo Gerando backup...
echo Destino: !FULL_PATH!
echo.

"%PG_DUMP%" ^
  -h %DB_HOST% ^
  -p %DB_PORT% ^
  -U %DB_USER% ^
  -F c ^
  -d %DB_NAME% ^
  -f "!FULL_PATH!"

if errorlevel 1 (
    echo.
    echo ERRO: o backup nao foi concluido.
    echo Nenhum backup antigo sera apagado.
    exit /b 1
)

if not exist "!FULL_PATH!" (
    echo.
    echo ERRO: o arquivo de backup nao foi criado.
    echo Nenhum backup antigo sera apagado.
    exit /b 1
)

echo.
echo Backup criado com sucesso.
echo.

for /f "skip=3 delims=" %%F in ('dir "%BACKUP_DIR%\%DB_NAME%_*.backup" /b /o-d') do (
    echo Apagando backup antigo: %%F
    del "%BACKUP_DIR%\%%F"
)

echo.
echo Mantidos somente os 3 backups mais recentes.
echo.

endlocal