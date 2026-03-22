# ⚡ Trading Hub – Lokální Bot Manager

Kompletní aplikace pro správu, spouštění a backtesting trading botů.

---

## 🚀 Spuštění

### Windows
Poklikej na `START.bat`

### Mac / Linux
```bash
chmod +x start.sh
./start.sh
```

### Manuálně
```bash
pip install flask pandas numpy
python app.py
```

Otevři prohlížeč: **http://localhost:5000**

---

## 📁 Struktura

```
trading_hub/
├── app.py              # Flask server (hlavní soubor)
├── database.py         # SQLite databáze
├── backtest.py         # Backtest engine
├── hub.py              # Helper pro boty (log_trade)
├── requirements.txt
├── START.bat           # Windows spuštění
├── start.sh            # Mac/Linux spuštění
├── templates/
│   └── index.html      # Celé UI
├── bots/               # Příklady botů
│   └── example_mnq/
│       └── strategy.py
├── bots_runtime/       # Dočasné soubory spuštěných botů
└── data/
    └── trading_hub.db  # Databáze (vytvoří se automaticky)
```

---

## 🤖 Jak napsat bota

### 1. Backtest kompatibilita
Definuj funkci `on_bar()`:

```python
def on_bar(df, i, params):
    """
    df     – DataFrame se svíčkami (open, high, low, close, volume)
    i      – index aktuální svíčky
    params – slovník parametrů z konfigurace bota
    
    Vrať signál nebo None:
    """
    # ... tvoje logika ...
    return {
        "type":  "LONG",   # nebo "SHORT"
        "entry": 19250.0,
        "sl":    19238.0,
        "tp1":   19275.0,
    }
```

### 2. Zápis obchodu do Trade Logu
```python
from hub import log_trade

log_trade(
    bot_id     = "muj_bot",
    bot_name   = "Můj Bot",
    instrument = "MNQ",
    direction  = "BUY",
    contracts  = 2,
    entry      = 19250.0,
    exit_price = 19275.0,
    sl         = 19238.0,
    tp         = 19275.0,
    pnl        = 94.0,
    entry_time = "2025-03-18 09:47:32",
    exit_time  = "2025-03-18 10:23:15",
)
```

### 3. Parametry k doplnění přes UI
Proměnné označené `'DOPLNIT'` jsou automaticky detekovány:

```python
API_KEY    = 'DOPLNIT'
ACCOUNT_ID = 'DOPLNIT'
CONTRACT   = 'DOPLNIT'
```

---

## 📊 Backtest CSV formát

Stáhni historická data z NinjaTrader nebo Tradovate jako CSV:

```
datetime,open,high,low,close,volume
2025-01-02 09:30:00,19100,19120,19090,19115,1500
2025-01-02 09:35:00,19115,19135,19105,19130,1200
...
```

---

## ⚠️ Disclaimer
Aplikace slouží jako nástroj pro správu botů. Vždy testuj na demo účtu
před nasazením na ostrý účet. Trading nese riziko ztráty.
