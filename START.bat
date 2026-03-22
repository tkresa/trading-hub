@echo off
chcp 65001 > nul
title Trading Hub
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo.
echo  ⚡ Trading Hub
echo  Spoustim aplikaci...
echo.

:: Zkontroluj Python
python --version > nul 2>&1
if errorlevel 1 (
    echo  CHYBA: Python nenalezen. Nainstaluj Python 3.10+
    pause
    exit /b 1
)

:: Nainstaluj zavislosti pokud chybi
if not exist ".deps_ok" (
    echo  Instaluji zavislosti...
    python -m pip install -r requirements.txt -q --break-system-packages 2>nul
    python -m pip install -r requirements.txt -q 2>nul
    echo. > .deps_ok
)

:: Spust aplikaci
echo  Oteviram http://localhost:5000
start "" "http://localhost:5000"
python app.py

:: Pokud Python skonci s chybou
if errorlevel 1 (
    echo.
    echo  CHYBA pri spusteni! Zkontroluj chybovou hlasku vyse.
    pause
)
