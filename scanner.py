import requests
import asyncio
import time
import logging
import json
import os
from datetime import datetime, timezone
from telegram import Bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

URL = "https://api.hyperliquid.xyz/info"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SCAN_INTERVAL = 60 * 5  # 5 minutos

TOKENS = ["BTC", "ETH", "SOL", "PYTH"]

# Umbrales para considerar señal
OI_CAMBIO_MIN = 0.015       # OI debe cambiar al menos 1.5%
VOLUMEN_MULTIPLICADOR = 2.0  # Volumen debe ser 2x el promedio reciente
CVD_MIN = 0.1               # CVD debe tener al menos 10% de sesgo

STATE_FILE = "scanner_estado.json"

# ── Estado persistente ────────────────────────────────────────────────────────

def cargar_estado():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}


def guardar_estado(estado):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(estado, f)
    except Exception as e:
        logging.error(f"Error guardando estado: {e}")


# ── API ───────────────────────────────────────────────────────────────────────

def api_post(payload):
    try:
        r = requests.post(URL, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        logging.error(f"API error: {e}")
        return None


def get_market_data(tokens):
    """Obtiene OI, funding y mark price para todos los tokens de una vez."""
    data = api_post({"type": "metaAndAssetCtxs"})
    if not data:
        return {}

    assets = data[0]["universe"]
    contexts = data[1]
    result = {}

    for i, asset in enumerate(assets):
        if asset["name"] in tokens:
            ctx = contexts[i]
            result[asset["name"]] = {
                "oi": float(ctx.get("openInterest", 0)),
                "funding": float(ctx.get("funding", 0)),
                "mark_price": float(ctx.get("markPx", 0)),
                "volume_24h": float(ctx.get("dayNtlVlm", 0)),
            }
    return result


def get_cvd(coin, limit=200):
    """CVD y volumen reciente de los últimos trades."""
    data = api_post({"type": "recentTrades", "coin": coin})
    if not data:
        return 0, 0

    cvd = 0
    vol_total = 0
    for trade in data[:limit]:
        size = float(trade.get("sz", 0))
        px = float(trade.get("px", 0))
        usd = size * px
        vol_total += usd
        if trade.get("side") == "B":
            cvd += usd
        elif trade.get("side") == "A":
            cvd -= usd

    return cvd, vol_total


def calcular_sesgo_cvd(cvd, vol_total):
    """Retorna sesgo del CVD como porcentaje (-1 a 1)."""
    if vol_total == 0:
        return 0
    return cvd / vol_total


# ── Análisis de señal ─────────────────────────────────────────────────────────

def analizar_token(coin, datos_actuales, datos_anteriores):
    """
    Compara datos actuales vs anteriores y determina si hay señal.
    Retorna (hay_señal, tipo, detalles) 
    """
    if coin not in datos_anteriores:
        return False, None, {}

    actual = datos_actuales[coin]
    anterior = datos_anteriores[coin]

    # OI — cambio porcentual
    oi_anterior = anterior.get("oi", 0)
    oi_actual = actual["oi"]
    if oi_anterior > 0:
        oi_cambio = (oi_actual - oi_anterior) / oi_anterior
    else:
        oi_cambio = 0

    # CVD y volumen reciente
    cvd, vol_reciente = get_cvd(coin)
    sesgo_cvd = calcular_sesgo_cvd(cvd, vol_reciente)

    # Volumen — comparar vol reciente vs promedio por minuto del día
    vol_24h = actual["volume_24h"]
    vol_promedio_5min = (vol_24h / 288) if vol_24h > 0 else 0  # 288 periodos de 5min en 24h
    vol_multiplicador = (vol_reciente / vol_promedio_5min) if vol_promedio_5min > 0 else 0

    # Funding
    funding = actual["funding"]
    funding_anterior = anterior.get("funding", 0)
    funding_cambio = funding - funding_anterior

    detalles = {
        "oi_actual": oi_actual,
        "oi_cambio": oi_cambio,
        "mark_price": actual["mark_price"],
        "funding": funding,
        "funding_cambio": funding_cambio,
        "sesgo_cvd": sesgo_cvd,
        "vol_reciente": vol_reciente,
        "vol_multiplicador": vol_multiplicador,
    }

    # Determinar señal
    oi_sube = oi_cambio >= OI_CAMBIO_MIN
    oi_baja = oi_cambio <= -OI_CAMBIO_MIN
    vol_anormal = vol_multiplicador >= VOLUMEN_MULTIPLICADOR
    cvd_alcista = sesgo_cvd >= CVD_MIN
    cvd_bajista = sesgo_cvd <= -CVD_MIN

    # PRESION ALCISTA — OI sube + CVD alcista + volumen anormal
    if oi_sube and cvd_alcista and vol_anormal:
        return True, "PRESION ALCISTA", detalles

    # PRESION BAJISTA — OI sube + CVD bajista + volumen anormal
    if oi_sube and cvd_bajista and vol_anormal:
        return True, "PRESION BAJISTA", detalles

    # SALIDA MASIVA — OI cae fuerte con volumen anormal
    if oi_baja and vol_anormal:
        tipo = "SALIDA ALCISTA" if cvd_alcista else "SALIDA BAJISTA"
        return True, tipo, detalles

    # VOLUMEN ANORMAL solo — sin señal de dirección clara aún
    if vol_anormal and abs(sesgo_cvd) >= CVD_MIN:
        tipo = "VOLUMEN ALCISTA" if cvd_alcista else "VOLUMEN BAJISTA"
        return True, tipo, detalles

    return False, None, detalles


def build_message(coin, tipo, d):
    """Construye el mensaje de Telegram para el market scanner."""
    oi_cambio_pct = d["oi_cambio"] * 100
    sesgo_pct = d["sesgo_cvd"] * 100
    funding_pct = d["funding"] * 100
    funding_cambio_pct = d["funding_cambio"] * 100
    vol_mult = d["vol_multiplicador"]
    mark = d["mark_price"]

    oi_fmt = f"{'+'if oi_cambio_pct>0 else ''}{oi_cambio_pct:.2f}%"
    sesgo_dir = "Compradores" if d["sesgo_cvd"] > 0 else "Vendedores"

    return (
        f"MARKET SCANNER — [ {tipo} ]\n"
        f"--------------------\n"
        f"Token: {coin}\n"
        f"Precio: ${mark:,.4f}\n\n"
        f"OI: {oi_fmt} vs scan anterior\n"
        f"CVD: {sesgo_dir} ({sesgo_pct:+.1f}%)\n"
        f"Volumen: {vol_mult:.1f}x el promedio\n"
        f"Funding: {funding_pct:.4f}% ({funding_cambio_pct:+.5f}% cambio)\n"
        f"--------------------\n"
        f"Señal de mercado — no de ballena."
    )


# ── Guardar datos para dashboard ──────────────────────────────────────────────

DASHBOARD_FILE = "scanner_datos.json"

def guardar_dashboard(datos_actuales, señales):
    """Guarda datos en vivo para que el dashboard los lea."""
    try:
        output = {}
        for coin, d in datos_actuales.items():
            output[coin] = {
                "mark_price": d["mark_price"],
                "oi": d["oi"],
                "funding": d["funding"] * 100,
                "volume_24h": d["volume_24h"],
                "señal": señales.get(coin, {}).get("tipo"),
                "oi_cambio": señales.get(coin, {}).get("oi_cambio", 0) * 100,
                "sesgo_cvd": señales.get(coin, {}).get("sesgo_cvd", 0) * 100,
                "vol_multiplicador": señales.get(coin, {}).get("vol_multiplicador", 0),
                "updated_at": datetime.now(timezone.utc).strftime("%H:%M UTC"),
            }
        with open(DASHBOARD_FILE, 'w') as f:
            json.dump(output, f)
    except Exception as e:
        logging.error(f"Error guardando dashboard: {e}")


# ── Loop principal ────────────────────────────────────────────────────────────

async def main_loop():
    bot = Bot(token=TELEGRAM_TOKEN)
    estado = cargar_estado()
    datos_anteriores = estado.get("datos", {})

    await bot.send_message(
        chat_id=CHAT_ID,
        text=(
            f"MARKET SCANNER iniciado\n"
            f"Tokens: {', '.join(TOKENS)}\n"
            f"Detectando: presion alcista/bajista, volumen anormal, salidas masivas\n"
            f"Scan cada 5 minutos."
        )
    )

    while True:
        try:
            print(f"[{time.strftime('%H:%M')}] Market scan...")

            datos_actuales = get_market_data(TOKENS)
            señales_scan = {}

            for coin in TOKENS:
                if coin not in datos_actuales:
                    continue

                hay_señal, tipo, detalles = analizar_token(
                    coin, datos_actuales, datos_anteriores
                )

                señales_scan[coin] = {
                    "tipo": tipo,
                    **detalles
                }

                if hay_señal:
                    msg = build_message(coin, tipo, detalles)
                    # await bot.send_message(chat_id=CHAT_ID, text=msg)
                    logging.info(f"SEÑAL: {coin} — {tipo}")
                else:
                    logging.info(
                        f"{coin}: OI {detalles.get('oi_cambio',0)*100:+.2f}% | "
                        f"CVD {detalles.get('sesgo_cvd',0)*100:+.1f}% | "
                        f"Vol {detalles.get('vol_multiplicador',0):.1f}x"
                    )

                await asyncio.sleep(0.3)

            guardar_dashboard(datos_actuales, señales_scan)
            datos_anteriores = datos_actuales
            guardar_estado({"datos": datos_actuales})

        except Exception as e:
            logging.error(f"Error en ciclo: {e}", exc_info=True)

        await asyncio.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main_loop())
