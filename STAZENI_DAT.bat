@echo off
title Tradovate Data Downloader
echo.
echo  ==========================================
echo   STAZENI HISTORICKYCH DAT Z TRADOVATE
echo  ==========================================
echo.
echo  Instaluji potrebne knihovny...
python -m pip install aiohttp websockets pandas -q
echo.
echo  Spoustim stazeni...
python download_data.py
echo.
pause
