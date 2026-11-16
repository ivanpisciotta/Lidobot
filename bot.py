import requests
import asyncio
import time
import logging
import csv
import os
import json
from datetime import datetime, timezone
from collections import defaultdict
from telegram import Bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

URL = "https://api.hyperliquid.xyz/info"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SCAN_INTERVAL = 60 * 5
FILL_MAX_EDAD_MIN = 6

with open("wallets.json") as f:
    SMART_WALLETS = json.load(f)

CSV_FILE = "alertas.csv"
STATE_FILE = "estado.json"
VENTANA_APERTURA_SEG = 1800  # 30 min: PRIMER FILL repetido = ACUMULACION

# ── CSV ───────────────────────────────────────────────────────────────────────

if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, mode='w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "alias", "wallet", "evento",
            "token", "direction", "position_usd",
            "entry_price", "current_price", "leverage",
            "funding", "cvd", "fill_time"
        ])


def guardar_csv(alias, wallet, evento, coin, direction, value_usd,
                entry_price, mark_price, leverage, funding, cvd, fill_time):
    with open(CSV_FILE, mode='a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            alias, wallet[:15], evento,
            coin, direction, value_usd,
            entry_price, mark_price, leverage,
            funding, cvd, fill_time
        ])


# ── Estado persistente ────────────────────────────────────────────────────────

def cargar_estado():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            fills = {k: set(v) for k, v in data.get("fills", {}).items()}
            posiciones = data.get("posiciones", {})
            aperturas = data.get("aperturas", {})
            return fills, posiciones, aperturas
        except Exception as e:
            logging.warning(f"No se pudo cargar estado: {e}")
    return {}, {}, {}


def guardar_estado(fills, posiciones, aperturas=None):
    try:
        data = {
            "fills": {k: list(v) for k, v in fills.items()},
            "posiciones": posiciones,
            "aperturas": aperturas or {}
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(data, f)
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


def get_all_mids():
    data = api_post({"type": "allMids"})
    return data if data else {}


def get_positions(wallet):
    data = api_post({"type": "clearinghouseState", "user": wallet})
    return data if data else {}


def get_recent_fills(wallet):
    data = api_post({"type": "userFills", "user": wallet})
    return data if data else []


def get_market_ctx(coin):
    data = api_post({"type": "metaAndAssetCtxs"})
    if not data:
        return None
    for i, asset in enumerate(data[0]["universe"]):
        if asset["name"] == coin:
            ctx = data[1][i]
            return {
                "funding": float(ctx.get("funding", 0)),
                "mark_price": float(ctx.get("markPx", 0)),
            }
    return None


def get_cvd(coin, limit=100):
    data = api_post({"type": "recentTrades", "coin": coin})
    if not data:
        return 0
    cvd = 0
    for trade in data[:limit]:
        size = float(trade.get("sz", 0))
        if trade.get("side") == "B":
            cvd += size
        elif trade.get("side") == "A":
            cvd -= size
    return cvd


def get_leverage(wallet, coin):
    data = api_post({"type": "clearinghouseState", "user": wallet})
    if not data:
        return None
    for p in data.get("assetPositions", []):
        pos = p.get("position", {})
        if pos.get("coin") == coin:
            return float(pos.get("leverage", {}).get("value", 0))
    return None


# ── Mensajes ──────────────────────────────────────────────────────────────────

def build_single(alias, evento, coin, direction, value_usd,
                 fill_px, mark_price, leverage, funding, cvd, fill_time):
    cvd_txt = "Compradores" if cvd > 0 else "Vendedores"
    funding_pct = (funding or 0) * 100
    lev_fmt = f"{leverage:.0f}x" if leverage else "-"

    if evento == "PRIMER FILL":
        return (
            f"🟢 {alias} — [ PRIMER FILL ]\n"
            f"--------------------\n"
            f"{fill_px and f'{coin} | {direction} ${value_usd:,.0f} @ ${fill_px:.4f}' or f'{coin} | {direction}'}\n"
            f"--------------------\n"
            f"Leverage: {lev_fmt}\n"
            f"Funding: {funding_pct:.4f}%\n"
            f"CVD: {cvd_txt}\n"
            f"--------------------"
        )
    elif evento == "CIERRE TOTAL":
        return (
            f"🔴 {alias} — [ CIERRE TOTAL ]\n"
            f"--------------------\n"
            f"{coin} | {direction} @ ${mark_price:.4f}\n"
            f"--------------------\n"
            f"Leverage: {lev_fmt}\n"
            f"Funding: {funding_pct:.4f}%\n"
            f"CVD: {cvd_txt}\n"
            f"--------------------"
        )
    elif evento == "FLIP":
        dir_anterior = "SHORT" if direction == "LONG" else "LONG"
        return (
            f"{alias} — [ FLIP ]\n"
            f"--------------------\n"
            f"{coin} | {dir_anterior} -> {direction} @ ${fill_px:.4f}\n"
            f"--------------------\n"
            f"Leverage: {lev_fmt}\n"
            f"Funding: {funding_pct:.4f}%\n"
            f"CVD: {cvd_txt}\n"
            f"--------------------"
        )
    else:
        return (
            f"{alias} — [ {evento} ]\n"
            f"--------------------\n"
            f"{coin} | {direction} ${value_usd:,.0f} @ ${fill_px:.4f}\n"
            f"--------------------\n"
            f"Leverage: {lev_fmt}\n"
            f"Funding: {funding_pct:.4f}%\n"
            f"CVD: {cvd_txt}\n"
            f"--------------------"
        )


def build_grouped(alias, evento, coin, direction, fills_list,
                  mark_price, leverage, funding, cvd, fill_time):
    total_usd = sum(f["value_usd"] for f in fills_list)
    n = len(fills_list)
    precio_min = min(f["fill_px"] for f in fills_list)
    precio_max = max(f["fill_px"] for f in fills_list)
    total_sz = sum(f["fill_sz"] for f in fills_list)
    precio_avg = total_usd / total_sz if total_sz > 0 else 0
    cvd_txt = "Compradores" if cvd > 0 else "Vendedores"
    funding_pct = (funding or 0) * 100
    lev_fmt = f"{leverage:.0f}x" if leverage else "-"

    if precio_min == precio_max:
        rango = f"${precio_min:.4f}"
    else:
        rango = f"(${precio_min:.4f} — ${precio_max:.4f})"

    return (
        f"{alias} — [ {evento} ]\n"
        f"--------------------\n"
        f"{coin} | {direction}\n"
        f"{n} fills — ${total_usd:,.0f}\n"
        f"Avg price: ${precio_avg:.4f} {rango}\n"
        f"Actual price: ${mark_price:.4f}\n"
        f"--------------------\n"
        f"Leverage: {lev_fmt}\n"
        f"Funding: {funding_pct:.4f}%\n"
        f"CVD: {cvd_txt}\n"
        f"--------------------"
    )

# ── Procesamiento ─────────────────────────────────────────────────────────────

async def procesar_wallet(bot, alias, wallet, fills_anteriores, posiciones_anteriores, mids, aperturas=None):
    if aperturas is None:
        aperturas = {}
    fills_actuales = get_recent_fills(wallet)
    posiciones_data = get_positions(wallet)

    pos_actuales = {}
    for p in posiciones_data.get("assetPositions", []):
        pos = p.get("position", {})
        coin = pos.get("coin", "")
        szi = float(pos.get("szi", 0))
        entry_px = float(pos.get("entryPx", 0) or 0)
        pos_actuales[coin] = {"szi": szi, "entry_px": entry_px}

    ids_anteriores = fills_anteriores.get(wallet, set())
    pos_anteriores = posiciones_anteriores.get(wallet, {})
    ids_nuevos = set()
    ahora = datetime.now(timezone.utc)

    # Guardia: respuesta vacía con posiciones previas = fallo de API, no cierre
    if not pos_actuales and len(pos_anteriores) > 1:
        logging.warning(
            f"{alias}: API devolvió 0 posiciones teniendo {len(pos_anteriores)} previas. "
            "Ciclo omitido."
        )
        return fills_anteriores, posiciones_anteriores, aperturas

    # Detectar cierres totales — requiere confirmación en dos ciclos
    pendientes = aperturas.setdefault("_cierres_pendientes", {})

    for coin, prev in pos_anteriores.items():
        if coin in pos_actuales or prev.get("szi", 0) == 0:
            pendientes.pop(f"{wallet}|{coin}", None)
            continue

        clave_c = f"{wallet}|{coin}"
        if clave_c not in pendientes:
            pendientes[clave_c] = True
            logging.info(f"Cierre pendiente de confirmar: {alias} {coin}")
            continue

        pendientes.pop(clave_c, None)

        if True:
            direction = "LONG" if prev["szi"] > 0 else "SHORT"
            ctx = get_market_ctx(coin)
            mark_price = ctx["mark_price"] if ctx else 0
            funding = ctx["funding"] if ctx else 0
            cvd = get_cvd(coin)
            leverage = get_leverage(wallet, coin) or 0
            fill_time = ahora.strftime("%Y-%m-%d %H:%M UTC")

            msg = build_single(alias, "CIERRE TOTAL", coin, direction,
                               0, prev["entry_px"], mark_price,
                               leverage, funding, cvd, fill_time)
            await bot.send_message(chat_id=CHAT_ID, text=msg)
            logging.info(f"CIERRE TOTAL: {alias} {coin}")
            guardar_csv(alias, wallet, "CIERRE TOTAL", coin, direction, 0,
                        prev["entry_px"], mark_price, leverage, funding, cvd, fill_time)

    # Clasificar fills nuevos y recientes
    # Agrupar por (coin, evento) — key para decidir cómo alertar
    # apertura_detectada: coins donde ya detectamos una APERTURA en este scan
    apertura_detectada = set()
    grupos = defaultdict(list)  # (coin, evento) -> lista de fills

    for fill in fills_actuales:
        fill_id = str(fill.get("tid") or fill.get("hash") or fill.get("time", ""))
        ids_nuevos.add(fill_id)

        if fill_id in ids_anteriores:
            continue

        fill_ts = fill.get("time")
        if not fill_ts:
            continue
        ts = datetime.fromtimestamp(fill_ts / 1000, tz=timezone.utc)
        edad_min = (ahora - ts).total_seconds() / 60
        if edad_min > FILL_MAX_EDAD_MIN:
            continue

        fill_time = ts.strftime("%Y-%m-%d %H:%M UTC")
        coin = fill.get("coin", "")
        side = fill.get("dir", "").lower()
        fill_px = float(fill.get("px", 0))
        fill_sz = float(fill.get("sz", 0))
        value_usd = fill_sz * fill_px

        # Clasificar
        if "open" in side and "long" in side:
            direction = "LONG"
            prev = pos_anteriores.get(coin, {})
            evento = "ACUMULACION" if prev.get("szi", 0) > 0 else "PRIMER FILL"
        elif "open" in side and "short" in side:
            direction = "SHORT"
            prev = pos_anteriores.get(coin, {})
            evento = "ACUMULACION" if prev.get("szi", 0) < 0 else "PRIMER FILL"
        elif "close" in side and "long" in side:
            direction = "LONG"
            actual = pos_actuales.get(coin, {})
            evento = "CIERRE PARCIAL" if actual.get("szi", 0) > 0 else None
        elif "close" in side and "short" in side:
            direction = "SHORT"
            actual = pos_actuales.get(coin, {})
            evento = "CIERRE PARCIAL" if actual.get("szi", 0) < 0 else None
        else:
            continue  # Ignorar fills desconocidos
        if not evento:
            continue

        # Detectar FLIP
        prev = pos_anteriores.get(coin, {})
        actual = pos_actuales.get(coin, {})
        if (prev.get("szi", 0) > 0 and actual.get("szi", 0) < 0) or \
           (prev.get("szi", 0) < 0 and actual.get("szi", 0) > 0):
            evento = "FLIP"
            direction = "LONG" if actual.get("szi", 0) > 0 else "SHORT"

        grupos[(coin, evento, direction)].append({
            "fill_px": fill_px,
            "fill_sz": fill_sz,
            "value_usd": value_usd,
            "fill_time": fill_time,
        })

    # Enviar alertas
    for (coin, evento, direction), fills_list in grupos.items():
        ctx = get_market_ctx(coin)
        mark_price = ctx["mark_price"] if ctx else 0
        funding = ctx["funding"] if ctx else 0
        cvd = get_cvd(coin)
        leverage = get_leverage(wallet, coin) or 0
        fill_time = fills_list[0]["fill_time"]
        total_usd = sum(f["value_usd"] for f in fills_list)

        # Degradar PRIMER FILL repetido dentro de la ventana
        if evento == "PRIMER FILL":
            clave_ap = f"{wallet}|{coin}|{direction}"
            ultima = aperturas.get(clave_ap)
            ahora_ts = time.time()
            if ultima and (ahora_ts - ultima) < VENTANA_APERTURA_SEG:
                evento = "ACUMULACION"
                logging.info(f"PRIMER FILL degradado a ACUMULACION: {alias} {coin} {direction}")
            else:
                aperturas[clave_ap] = ahora_ts

        # APERTURA — siempre alerta inmediata del primer fill
        if evento == "PRIMER FILL":
            primer = fills_list[0]
            msg = build_single(alias, "PRIMER FILL", coin, direction,
                               primer["value_usd"], primer["fill_px"],
                               mark_price, leverage, funding, cvd, fill_time)
            await bot.send_message(chat_id=CHAT_ID, text=msg)
            logging.info(f"PRIMER FILL: {alias} {coin} {direction} ${primer['value_usd']:,.0f}")
            guardar_csv(alias, wallet, "PRIMER FILL", coin, direction,
                        primer["value_usd"], primer["fill_px"],
                        mark_price, leverage, funding, cvd, fill_time)

            # Si hay más fills de apertura, los agrupamos aparte como ACUMULACION
            if len(fills_list) > 1:
                resto = fills_list[1:]
                msg2 = build_grouped(alias, "ACUMULACION", coin, direction,
                                     resto, mark_price, leverage, funding, cvd, fill_time)
                await bot.send_message(chat_id=CHAT_ID, text=msg2)
                logging.info(f"ACUMULACION (post-apertura): {alias} {coin} {len(resto)} fills")
                guardar_csv(alias, wallet, "ACUMULACION", coin, direction,
                            sum(f["value_usd"] for f in resto), resto[0]["fill_px"],
                            mark_price, leverage, funding, cvd, fill_time)

        # ACUMULACION, CIERRE PARCIAL, FLIP — agrupar siempre
        else:
            if len(fills_list) == 1:
                f = fills_list[0]
                msg = build_single(alias, evento, coin, direction,
                                   f["value_usd"], f["fill_px"],
                                   mark_price, leverage, funding, cvd, fill_time)
            else:
                msg = build_grouped(alias, evento, coin, direction,
                                    fills_list, mark_price, leverage, funding, cvd, fill_time)

            await bot.send_message(chat_id=CHAT_ID, text=msg)
            logging.info(f"{evento}: {alias} {coin} {direction} {len(fills_list)} fills ${total_usd:,.0f}")
            guardar_csv(alias, wallet, evento, coin, direction,
                        total_usd, fills_list[0]["fill_px"],
                        mark_price, leverage, funding, cvd, fill_time)

        await asyncio.sleep(0.3)

    # Actualizar estado
    fills_anteriores[wallet] = ids_anteriores | ids_nuevos
    if len(fills_anteriores[wallet]) > 5000:
        fills_anteriores[wallet] = set(list(fills_anteriores[wallet])[-3000:])
    posiciones_anteriores[wallet] = pos_actuales

    return fills_anteriores, posiciones_anteriores, aperturas


# ── Loop principal ────────────────────────────────────────────────────────────

async def main_loop():
    bot = Bot(token=TELEGRAM_TOKEN)
    fills_anteriores, posiciones_anteriores, aperturas = cargar_estado()

    if fills_anteriores:
        logging.info("Estado cargado desde disco.")
    else:
        logging.info("Sin estado previo — cargando estado inicial...")

    await bot.send_message(
        chat_id=CHAT_ID,
        text=(
            "iniciando...\n"
        )
    )

    mids = get_all_mids()

    if not fills_anteriores:
        for w in SMART_WALLETS:
            wallet = w["wallet"]
            fills = get_recent_fills(wallet)
            positions = get_positions(wallet)

            ids = set()
            for fill in fills:
                fid = str(fill.get("tid") or fill.get("hash") or fill.get("time", ""))
                ids.add(fid)
            fills_anteriores[wallet] = ids

            pos_actuales = {}
            for p in positions.get("assetPositions", []):
                pos = p.get("position", {})
                coin = pos.get("coin", "")
                szi = float(pos.get("szi", 0))
                entry_px = float(pos.get("entryPx", 0) or 0)
                pos_actuales[coin] = {"szi": szi, "entry_px": entry_px}
            posiciones_anteriores[wallet] = pos_actuales

            logging.info(f"  {w['alias']}: {len(ids)} fills cargados, {len(pos_actuales)} posiciones")
            await asyncio.sleep(0.5)

        guardar_estado(fills_anteriores, posiciones_anteriores, aperturas)
        logging.info("Estado inicial guardado. Iniciando monitoreo...")

    while True:
        await asyncio.sleep(SCAN_INTERVAL)
        try:
            print(f"[{time.strftime('%H:%M')}] Escaneando {len(SMART_WALLETS)} wallets...")
            mids = get_all_mids()

            for w in SMART_WALLETS:
                fills_anteriores, posiciones_anteriores, aperturas = await procesar_wallet(
                    bot, w["alias"], w["wallet"],
                    fills_anteriores, posiciones_anteriores, mids, aperturas
                )
                await asyncio.sleep(0.5)

            guardar_estado(fills_anteriores, posiciones_anteriores, aperturas)

        except Exception as e:
            logging.error(f"Error en ciclo: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main_loop())
