#!/bin/bash
echo ""
echo " =========================================="
echo "  TRADING HUB - spouštím..."
echo " =========================================="
echo ""

# Zkontroluj Python
if ! command -v python3 &> /dev/null; then
    echo " [CHYBA] Python3 není nainstalován!"
    exit 1
fi

# Instalace závislostí
echo " Kontroluji závislosti..."
pip3 install -q flask pandas numpy

echo ""
echo " Otevírám prohlížeč..."
sleep 2
open http://localhost:5000 2>/dev/null || xdg-open http://localhost:5000 2>/dev/null

echo " Spouštím server..."
python3 app.py
