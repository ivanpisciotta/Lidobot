# Lidobot

A smart-money tracker for Hyperliquid perpetuals. It watches a list of whale wallets and sends a Telegram alert when one of them opens, changes or closes a position.

I built it to support my own trading and have been running and improving it since April 2026.

## What it does

- **Whale tracking (`bot.py`):** checks the wallets in `wallets.json` every 5 minutes through Hyperliquid's public API, finds new fills, sends alerts to Telegram and logs every alert to a CSV file
- **Market scanner (`scanner.py`):** watches open interest, funding and mark price for selected assets (BTC, ETH, SOL, PYTH) and flags unusual moves
- **Dashboard (`dashboard.py`):** a Flask web view with three tabs (Positions, Movements, Market) and a light/dark mode

## Stack

Python · asyncio · python-telegram-bot · Flask · pandas · Hyperliquid public API · PM2

## Setup

```bash
git clone https://github.com/ivanpisciotta/lidobot.git
cd lidobot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                    # add your Telegram bot token and chat ID
cp wallets.example.json wallets.json    # add the wallets you want to follow
```

`wallets.json` format:

```json
[
  { "wallet": "0x...", "alias": "some.whale" }
]
```

## Run

```bash
set -a; source .env; set +a
python bot.py         # whale alerts
python scanner.py     # market scanner
python dashboard.py   # dashboard at http://127.0.0.1:5000
```

In my own setup the three processes run under PM2.

## Security

- The Telegram credentials are read from environment variables and are never in the code.
- The wallet list, state files and alert history are kept out of the repo (see `.gitignore`).

## Disclaimer

A personal tool for watching what traders do on-chain. It is not financial advice.
