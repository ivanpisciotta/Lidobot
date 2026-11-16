from flask import Flask, render_template, jsonify
import requests
import pandas as pd
import os
import json

app = Flask(__name__)

URL = "https://api.hyperliquid.xyz/info"
CSV_FILE = "alertas.csv"
SCANNER_FILE = "scanner_datos.json"

with open("wallets.json") as f:
    SMART_WALLETS = json.load(f)

def get_all_mids():
    try:
        return requests.post(URL, json={"type": "allMids"}, timeout=10).json()
    except:
        return {}


def get_positions(wallet):
    try:
        return requests.post(URL, json={"type": "clearinghouseState", "user": wallet}, timeout=10).json()
    except:
        return {}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/posiciones')
def get_posiciones():
    mids = get_all_mids()
    resultado = []
    for w in SMART_WALLETS:
        wallet = w["wallet"]
        alias = w["alias"]
        data = get_positions(wallet)
        positions = data.get("assetPositions", [])
        account = data.get("marginSummary", {})
        account_value = float(account.get("accountValue", 0))
        total_pnl = float(account.get("totalRawUsd", 0))
        wallet_positions = []
        for p in positions:
            pos = p.get("position", {})
            coin = pos.get("coin", "")
            szi = float(pos.get("szi", 0))
            entry_price = float(pos.get("entryPx", 0) or 0)
            unrealized_pnl = float(pos.get("unrealizedPnl", 0) or 0)
            leverage_val = float(pos.get("leverage", {}).get("value", 0) or 0)
            mid = float(mids.get(coin, 0) or 0)
            value_usd = abs(szi) * mid if mid > 0 else 0
            direction = "LONG" if szi > 0 else "SHORT"
            wallet_positions.append({
                "coin": coin,
                "direction": direction,
                "size": abs(szi),
                "entry_price": entry_price,
                "mark_price": mid,
                "value_usd": value_usd,
                "unrealized_pnl": unrealized_pnl,
                "leverage": leverage_val,
            })
        resultado.append({
            "alias": alias,
            "wallet": wallet,
            "account_value": account_value,
	    "total_pnl": total_pnl,
            "positions": wallet_positions,
        })
    return jsonify(resultado)


@app.route('/api/movimientos')
def get_movimientos():
    if not os.path.exists(CSV_FILE):
        return jsonify([])
    try:
        df = pd.read_csv(CSV_FILE)
        df = df.fillna("")
        df = df.sort_values('timestamp', ascending=False)
        return jsonify(df.to_dict(orient='records'))
    except:
        return jsonify([])


@app.route('/api/mercado')
def get_mercado():
    if not os.path.exists(SCANNER_FILE):
        return jsonify({})
    try:
        with open(SCANNER_FILE) as f:
            return jsonify(json.load(f))
    except:
        return jsonify({})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
