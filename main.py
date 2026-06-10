"""期货大师模拟器 — Futures Master Simulator"""
import asyncio
import json
import math
import os
import random
import statistics
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── 飞书通知 ─────────────────────────────────────────────
FEISHU_WEBHOOK = os.getenv(
    "FEISHU_WEBHOOK",
    "https://open.feishu.cn/open-apis/bot/v2/hook/269c441d-7352-4b90-8f9a-32b84ea4a798",
)

def send_feishu(msg: str):
    payload = json.dumps({
        "msg_type": "text",
        "content": {"text": msg},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            FEISHU_WEBHOOK, data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[Feishu] send failed: {e}")

app = FastAPI(title="期货大师模拟器")
static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ─── 品种配置 ─────────────────────────────────────────────
PRODUCTS = {
    "AU":  {"name": "黄金",   "price": 2350.0, "multiplier": 1000, "volatility": 0.004, "margin_rate": 0.08},
    "SC":  {"name": "原油",   "price": 520.0,  "multiplier": 1000, "volatility": 0.006, "margin_rate": 0.10},
    "IF":  {"name": "沪深300", "price": 3850.0, "multiplier": 300,  "volatility": 0.005, "margin_rate": 0.12},
}

# ─── 交易信号 ─────────────────────────────────────────────
signal_state: dict = {}  # { pid: { "cross_state": ..., "deviation_zone": ... } }
signal_history: list = []  # list of signal dicts, max 200

# ─── 账户 ─────────────────────────────────────────────────
account = {
    "balance": 1_000_000.0,
    "used_margin": 0.0,
    "total_pnl": 0.0,
    "equity": 1_000_000.0,
    "free": 1_000_000.0,
    "risk_rate": 1.0,
}

positions = {}       # id -> position
trade_history = []   # list of closed trades
next_id = 1
price_history = {pid: deque([cfg["price"]]) for pid, cfg in PRODUCTS.items()}

# ─── K 线数据 ─────────────────────────────────────────────
HOUR_SECS = 3600
DAY_SECS = 86400

# { product_id: { "1h": [candles], "1d": [candles] } }
kline_data: dict = {}

# Track current forming candle per product per period
current_candle: dict = {}  # { "AU_1h": {...}, "AU_1d": {...}, ... }

def generate_candles(pid: str, interval: str, count: int) -> list:
    cfg = PRODUCTS[pid]
    interval_sec = HOUR_SECS if interval == "1h" else DAY_SECS
    vol = cfg["volatility"] * math.sqrt(interval_sec / HOUR_SECS) * 2.5
    now = int(datetime.now(timezone.utc).timestamp())
    start = now - (now % interval_sec)
    candles = []
    price = cfg["price"]
    base = cfg["price"]
    for i in range(count):
        t = start - (count - i) * interval_sec
        o = round(price, 2)
        reversion = 0.008 * (base - price) / max(price, 1)
        chg = random.gauss(0, 1) * vol + reversion
        c = round(price * (1 + chg), 2)
        h = round(max(o, c) * (1 + abs(random.gauss(0, 1)) * vol * 0.3), 2)
        l = round(min(o, c) * (1 - abs(random.gauss(0, 1)) * vol * 0.3), 2)
        candles.append({"time": t, "open": o, "high": h, "low": l, "close": c})
        price = c
        # Allow slow trend shifts every ~3 months (in hourly candles)
        if interval == "1h" and i > 0 and i % 600 == 0:
            base = price
        elif interval == "1d" and i > 0 and i % 60 == 0:
            base = price
    cfg["price"] = price
    return candles

def init_kline():
    now = int(datetime.now(timezone.utc).timestamp())
    for pid in PRODUCTS:
        cfg = PRODUCTS[pid]
        base = cfg["price"]

        # ── 1h candles: independent random walk from near base ──
        cfg["price"] = base * (1 + random.uniform(-0.02, 0.02))
        kline_data[pid] = {"1h": generate_candles(pid, "1h", 500)}
        hourly_last_close = cfg["price"]  # snapshot before daily overwrites it

        # ── 1d candles: also from near base (different seed path is fine) ──
        cfg["price"] = base * (1 + random.uniform(-0.02, 0.02))
        kline_data[pid]["1d"] = generate_candles(pid, "1d", 300)

        # ── Live price starts from the last hourly close so hourly K‑line & market panel agree ──
        cfg["price"] = hourly_last_close

        # ── Forming candles ──
        for interval in ("1h", "1d"):
            key = f"{pid}_{interval}"
            interval_sec = HOUR_SECS if interval == "1h" else DAY_SECS
            period_start = now - (now % interval_sec)
            candles = kline_data[pid][interval]
            last = candles[-1]
            # Use hourly_last_close for the forming hourly candle, last daily close for daily
            start_price = hourly_last_close if interval == "1h" else last["close"]
            current_candle[key] = {
                "time": period_start,
                "open": start_price,
                "high": start_price,
                "low": start_price,
                "close": start_price,
            }

    # Correct risk_rate from initial 1.0 to actual value (0 = no positions)
    recalc_account()

def get_kline(pid: str, interval: str, limit: int = 300):
    candles = kline_data.get(pid, {}).get(interval, [])
    if not candles:
        return []
    # Merge the forming candle in (don't drop last completed candle)
    result = list(candles)
    cc = current_candle.get(f"{pid}_{interval}")
    if cc and cc["time"] != candles[-1]["time"]:
        result.append(cc)
    return result[-limit:]

def update_candle(pid: str, price: float):
    now = int(datetime.now(timezone.utc).timestamp())
    for interval in ("1h", "1d"):
        interval_sec = HOUR_SECS if interval == "1h" else DAY_SECS
        period_start = now - (now % interval_sec)
        key = f"{pid}_{interval}"
        cc = current_candle.get(key)
        if not cc:
            continue
        # If a new period started, finalise old candle and start new
        if cc["time"] < period_start:
            # Push completed candle to history
            kline_data[pid][interval].append(dict(cc))
            # Start new
            candles = kline_data[pid][interval]
            prev_close = candles[-1]["close"] if candles else price
            current_candle[key] = {
                "time": period_start,
                "open": prev_close,
                "high": prev_close,
                "low": prev_close,
                "close": price,
            }
        else:
            cc["high"] = max(cc["high"], price)
            cc["low"] = min(cc["low"], price)
            cc["close"] = price

# ─── 价格模拟引擎 ──────────────────────────────────────────
def update_price(pid: str, cfg: dict) -> float:
    cur = cfg["price"]
    base = PRODUCTS[pid]["price"]  # original base
    mu = 0.0
    reversion = 0.002 * (base - cur) / base
    shock = random.gauss(0, 1) * cfg["volatility"]
    new_price = cur * (1 + mu + reversion + shock)
    new_price = round(max(new_price, base * 0.3), 2)
    cfg["price"] = new_price
    price_history[pid].append(new_price)
    if len(price_history[pid]) > 300:
        price_history[pid].popleft()
    return new_price

# ─── 交易核心逻辑 ──────────────────────────────────────────
def calc_margin(pid: str, price: float, volume: int, leverage: int) -> float:
    cfg = PRODUCTS[pid]
    contract_value = price * cfg["multiplier"] * volume
    return round(contract_value / leverage, 2)

def calc_pnl(position: dict, current_price: float) -> float:
    cfg = PRODUCTS[position["product"]]
    diff = current_price - position["entry_price"]
    if position["direction"] == "short":
        diff = -diff
    return round(diff * cfg["multiplier"] * position["volume"], 2)

def recalc_account():
    total_pnl = sum(p["pnl"] for p in positions.values())
    account["total_pnl"] = round(total_pnl, 2)
    account["equity"] = round(account["balance"] + total_pnl, 2)
    account["used_margin"] = round(sum(p["margin"] for p in positions.values()), 2)
    account["free"] = round(max(account["equity"] - account["used_margin"], 0), 2)
    account["risk_rate"] = round(
        account["used_margin"] / account["equity"] if account["equity"] > 0 else 0,
        4,
    )

def open_position(pid: str, direction: str, volume: int, leverage: int) -> dict:
    global next_id
    price = PRODUCTS[pid]["price"]
    margin = calc_margin(pid, price, volume, leverage)

    if margin > account["free"] + 1:  # small tolerance for float
        return {"success": False, "msg": f"可用资金不足，需保证金 ¥{margin:,.2f}，可用 ¥{account['free']:,.2f}"}

    pos_id = str(next_id)
    next_id += 1
    pos = {
        "id": pos_id,
        "product": pid,
        "product_name": PRODUCTS[pid]["name"],
        "direction": direction,
        "volume": volume,
        "leverage": leverage,
        "entry_price": price,
        "current_price": price,
        "margin": margin,
        "pnl": 0.0,
        "pnl_percent": 0.0,
        "entry_time": datetime.now().strftime("%H:%M:%S"),
    }
    positions[pos_id] = pos

    recalc_account()
    d = "📈 买涨" if direction == "long" else "📉 买跌"
    send_feishu(
        f"🟢 开仓成功\n"
        f"┌───────\n"
        f"│ {d}\n"
        f"│ 品种: {PRODUCTS[pid]['name']} ({pid})\n"
        f"│ 价格: {price:.2f}\n"
        f"│ 手数: {volume}  杠杆: {leverage}x\n"
        f"│ 保证金: ¥{margin:,.2f}\n"
        f"│ 可用: ¥{account['free']:,.2f}\n"
        f"└───────\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )
    return {"success": True, "position": pos}

def close_position(pos_id: str) -> Optional[dict]:
    pos = positions.pop(pos_id, None)
    if not pos:
        return None
    cfg = PRODUCTS[pos["product"]]
    exit_price = cfg["price"]
    pnl = calc_pnl(pos, exit_price)
    # Add realized PnL to balance (margin was never deducted)
    account["balance"] += pnl
    trade_history.append({
        **pos,
        "exit_price": exit_price,
        "realized_pnl": pnl,
        "exit_time": datetime.now().strftime("%H:%M:%S"),
    })
    recalc_account()
    d = "📈" if pos["direction"] == "long" else "📉"
    emoji = "🟢" if pnl >= 0 else "🔴"
    send_feishu(
        f"{emoji} 平仓 {d}\n"
        f"┌───────\n"
        f"│ {pos['product_name']} ({pos['product']})\n"
        f"│ 开仓: {pos['entry_price']:.2f} → 平仓: {exit_price:.2f}\n"
        f"│ 手数: {pos['volume']}  杠杆: {pos['leverage']}x\n"
        f"│ {'✅ 盈利' if pnl >= 0 else '❌ 亏损'}: ¥{pnl:+,.2f}\n"
        f"└───────\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )
    return {**pos, "exit_price": exit_price, "realized_pnl": pnl}

def check_liquidation():
    """Force-close positions if risk rate is too high."""
    if account["risk_rate"] >= 0.9 and positions:
        for pos_id in list(positions.keys()):
            pos = positions.get(pos_id)
            if pos:
                send_feishu(
                    f"🚨 爆仓强平！\n"
                    f"┌───────\n"
                    f"│ {pos['product_name']} ({pos['product']})\n"
                    f"│ {'📈多' if pos['direction']=='long' else '📉空'} {pos['volume']}手\n"
                    f"│ 开仓价: {pos['entry_price']:.2f}\n"
                    f"│ 风险率: {account['risk_rate']*100:.1f}%\n"
                    f"└───────\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                )
            close_position(pos_id)

# ─── 均线计算 ────────────────────────────────────────────
def calc_ma(candles: list, period: int) -> list:
    """Return list of {time, value} for MA lines, skipping initial incomplete periods."""
    result = []
    for i in range(len(candles)):
        if i < period - 1:
            continue
        avg = sum(c["close"] for c in candles[i - period + 1 : i + 1]) / period
        result.append({"time": candles[i]["time"], "value": round(avg, 2)})
    return result


def calc_deviation(ma20_val: float, ma60_val: float, ma120_val: float) -> float:
    """计算偏离度 = (max - min) / median x 100%"""
    vals = [ma20_val, ma60_val, ma120_val]
    ma_min = min(vals)
    ma_max = max(vals)
    ma_median = statistics.median(vals)
    if ma_median == 0:
        return 0.0
    return round((ma_max - ma_min) / ma_median * 100, 2)


def get_ma_values(pid: str) -> tuple:
    """获取当前品种的最新 MA20/MA60/MA120 值 (使用1h K线)"""
    candles_1h = kline_data.get(pid, {}).get("1h", [])
    if len(candles_1h) < 120:
        return None, None, None
    ma20_list = calc_ma(candles_1h, 20)
    ma60_list = calc_ma(candles_1h, 60)
    ma120_list = calc_ma(candles_1h, 120)
    if not ma20_list or not ma60_list or not ma120_list:
        return None, None, None
    return ma20_list[-1]["value"], ma60_list[-1]["value"], ma120_list[-1]["value"]


def compute_deviation_zone(deviation: float, prev_zone: str | None = None) -> str:
    """确定偏离度所在区间，含 0.5% 滞回区避免边界震荡"""
    if deviation <= 3.0:
        return "convergence"        # 聚合
    elif deviation > 10.0:
        return "divergence_end"     # 发散末期
    # 滞回逻辑：向上突破需要更大偏离度，向下回落用标准阈值
    if prev_zone == "convergence" and deviation <= 3.5:
        return "convergence"
    if prev_zone == "divergence_end" and deviation > 9.5:
        return "divergence_end"
    if deviation <= 5.0:
        return "divergence_start"   # 发散初期
    else:
        return "divergence_mid"     # 发散中期


SIGNAL_META = {
    "golden_cross": {
        "severity": "info",
        "title": "⭐ 金叉信号",
    },
    "death_cross": {
        "severity": "warning",
        "title": "💀 死叉信号",
    },
    "convergence": {
        "severity": "info",
        "title": "📊 聚合信号",
    },
    "divergence_start": {
        "severity": "info",
        "title": "🚀 发散初期",
    },
    "divergence_mid": {
        "severity": "warning",
        "title": "🔥 发散中期",
    },
    "divergence_end": {
        "severity": "critical",
        "title": "⚠️ 发散末期",
    },
}


def detect_signals() -> list:
    """检测所有品种的交易信号，返回新触发的信号列表"""
    new_signals = []

    for pid in PRODUCTS:
        ma20, ma60, ma120 = get_ma_values(pid)
        if ma20 is None:
            continue

        prev_state = signal_state.get(pid, {"cross_state": None, "deviation_zone": None})
        cfg = PRODUCTS[pid]
        deviation = calc_deviation(ma20, ma60, ma120)
        zone = compute_deviation_zone(deviation, prev_state["deviation_zone"])

        # ── 判断金叉/死叉 ──
        # 金叉: MA20 > MA60; 死叉: MA20 < MA60
        threshold = ma60 * 0.001  # 0.1% 相对阈值，避免价格震荡导致的反复切换
        if ma20 > ma60 + threshold:
            new_cross = "golden"
        elif ma20 < ma60 - threshold:
            new_cross = "death"
        else:
            new_cross = signal_state.get(pid, {}).get("cross_state", None)

        # 检测交叉状态变化（跳过首次初始化，避免启动时误报）
        if prev_state["cross_state"] is not None and prev_state["cross_state"] != new_cross and new_cross in ("golden", "death"):
            signal_type = "golden_cross" if new_cross == "golden" else "death_cross"
            meta = SIGNAL_META[signal_type]
            sig = {
                "id": f"sig_{int(datetime.now().timestamp())}_{pid}_{signal_type}",
                "time": int(datetime.now().timestamp()),
                "product": pid,
                "product_name": cfg["name"],
                "signal_type": signal_type,
                "severity": meta["severity"],
                "title": meta["title"],
                "detail": (
                    f"MA20({ma20}) {'上穿' if new_cross == 'golden' else '下穿'} MA60({ma60})"
                ),
                "values": {"ma20": ma20, "ma60": ma60, "ma120": ma120, "deviation": deviation},
            }
            new_signals.append(sig)
            signal_history.append(sig)

        # 检测偏离度区间变化
        if prev_state["deviation_zone"] != zone:
            meta = SIGNAL_META.get(zone, {})
            if meta:
                zone_labels = {
                    "convergence": "偏离度 ≤ 3%，均线聚合",
                    "divergence_start": "偏离度 3%-5%，发散初期",
                    "divergence_mid": "偏离度 5%-10%，发散中期",
                    "divergence_end": "偏离度 > 10%，发散末期",
                }
                sig = {
                    "id": f"sig_{int(datetime.now().timestamp())}_{pid}_{zone}",
                    "time": int(datetime.now().timestamp()),
                    "product": pid,
                    "product_name": cfg["name"],
                    "signal_type": zone,
                    "severity": meta["severity"],
                    "title": meta["title"],
                    "detail": zone_labels.get(zone, ""),
                    "values": {"ma20": ma20, "ma60": ma60, "ma120": ma120, "deviation": deviation},
                }
                new_signals.append(sig)
                signal_history.append(sig)

        # 更新状态
        signal_state[pid] = {"cross_state": new_cross, "deviation_zone": zone}

    # 限制信号历史数量
    if len(signal_history) > 200:
        signal_history[:] = signal_history[-200:]

    return new_signals


# ─── WebSocket 连接管理 ────────────────────────────────────
connected_clients: list[WebSocket] = []

async def broadcast(data: dict):
    msg = json.dumps(data, ensure_ascii=False)
    dead = []
    for ws in connected_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            connected_clients.remove(ws)
        except ValueError:
            pass

# ─── 行情推送任务 ──────────────────────────────────────────
async def price_tick():
    while True:
        await asyncio.sleep(1)
        # Update prices
        for pid, cfg in PRODUCTS.items():
            old = cfg["price"]
            new = update_price(pid, cfg)
            cfg["change"] = round((new - old) / old * 100, 2) if old else 0.0
            update_candle(pid, new)

        # Update position PnLs
        for pos in positions.values():
            cur = PRODUCTS[pos["product"]]["price"]
            pos["current_price"] = cur
            pos["pnl"] = calc_pnl(pos, cur)
            entry_val = pos["entry_price"] * PRODUCTS[pos["product"]]["multiplier"] * pos["volume"]
            pos["pnl_percent"] = round(pos["pnl"] / entry_val * 100, 2) if entry_val else 0

        recalc_account()
        check_liquidation()

        # Detect and broadcast trading signals
        new_signals = detect_signals()
        if new_signals:
            asyncio.create_task(broadcast({"type": "signal", "signals": new_signals}))

        # Broadcast
        market = {
            pid: {"name": cfg["name"], "price": cfg["price"], "change": cfg.get("change", 0)}
            for pid, cfg in PRODUCTS.items()
        }
        pos_list = [
            {
                "id": p["id"],
                "product": p["product"],
                "product_name": p["product_name"],
                "direction": p["direction"],
                "volume": p["volume"],
                "leverage": p["leverage"],
                "entry_price": p["entry_price"],
                "current_price": p["current_price"],
                "pnl": p["pnl"],
                "pnl_percent": p["pnl_percent"],
                "margin": p["margin"],
                "entry_time": p["entry_time"],
            }
            for p in positions.values()
        ]
        await broadcast({
            "type": "tick",
            "market": market,
            "kline_latest": {
                pid: {
                    "1h": current_candle.get(f"{pid}_1h", {}),
                    "1d": current_candle.get(f"{pid}_1d", {}),
                }
                for pid in PRODUCTS
            },
            "account": {
                "balance": account["balance"],
                "equity": account["equity"],
                "used_margin": account["used_margin"],
                "free": account["free"],
                "total_pnl": account["total_pnl"],
                "risk_rate": account["risk_rate"],
            },
            "positions": pos_list,
        })

@app.on_event("startup")
async def startup():
    init_kline()
    asyncio.create_task(price_tick())
    send_feishu(
        f"🟢 期货大师模拟器已启动\n"
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📊 黄金AU | 原油SC | 沪深300IF\n"
        f"💰 初始资金 ¥1,000,000"
    )

# ─── REST API ─────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home():
    path = os.path.join(BASE_DIR, "templates", "index.html")
    with open(path, encoding="utf-8") as f:
        return f.read()

@app.get("/api/account")
def get_account():
    return {
        "balance": account["balance"],
        "equity": account["equity"],
        "used_margin": account["used_margin"],
        "free": account["free"],
        "risk_rate": account["risk_rate"],
    }

@app.get("/api/products")
def get_products():
    return {pid: {"name": cfg["name"], "price": cfg["price"], "change": cfg.get("change", 0)} for pid, cfg in PRODUCTS.items()}

@app.get("/api/positions")
def get_positions():
    return list(positions.values())

@app.get("/api/history")
def get_history():
    return list(reversed(trade_history[-100:]))

@app.post("/api/orders")
async def place_order(pid: str, direction: str, volume: int = 1, leverage: int = 10):
    if pid not in PRODUCTS:
        return {"success": False, "msg": "品种不存在"}
    if direction not in ("long", "short"):
        return {"success": False, "msg": "方向错误"}
    if volume < 1 or volume > 100:
        return {"success": False, "msg": "手数需在1-100之间"}
    if leverage < 1 or leverage > 20:
        return {"success": False, "msg": "杠杆需在1-20倍之间"}
    result = open_position(pid, direction, volume, leverage)
    if result["success"]:
        await broadcast({"type": "order_result", "success": True, "msg": f"开仓成功！{PRODUCTS[pid]['name']} {('多' if direction == 'long' else '空')} {volume}手"})
    return result

@app.post("/api/orders/{pos_id}/close")
async def close_order(pos_id: str):
    result = close_position(pos_id)
    if not result:
        return {"success": False, "msg": "持仓不存在"}
    d = "多" if result["direction"] == "long" else "空"
    await broadcast({"type": "order_result", "success": True, "msg": f"平仓成功！{result['product_name']} {d} {result['volume']}手 盈亏: ¥{result['realized_pnl']:+,.2f}"})
    return {"success": True, "trade": result}

@app.post("/api/reset")
async def reset_account():
    global next_id
    positions.clear()
    trade_history.clear()
    account["balance"] = 1_000_000.0
    account["used_margin"] = 0.0
    account["total_pnl"] = 0.0
    account["equity"] = 1_000_000.0
    account["free"] = 1_000_000.0
    account["risk_rate"] = 1.0
    next_id = 1
    # Reset price history & re-init kline data without changing current prices
    saved_prices = {pid: cfg["price"] for pid, cfg in PRODUCTS.items()}
    price_history.clear()
    for pid in PRODUCTS:
        price_history[pid] = deque([saved_prices[pid]])
    kline_data.clear()
    current_candle.clear()
    init_kline()
    for pid, price in saved_prices.items():
        PRODUCTS[pid]["price"] = price
    signal_state.clear()
    signal_history.clear()
    return {"success": True}

@app.get("/api/kline")
def get_kline_api(pid: str = "AU", interval: str = "1h", limit: int = 300):
    candles = get_kline(pid, interval, limit)
    mas = {
        "MA20": calc_ma(candles, 20),
        "MA60": calc_ma(candles, 60),
        "MA120": calc_ma(candles, 120),
    }
    return {"candles": candles, "mas": mas}


@app.get("/api/signals")
def get_signals(limit: int = 100):
    return list(reversed(signal_history[-limit:]))


# ─── WebSocket ────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.append(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive
    except WebSocketDisconnect:
        pass
    finally:
        try:
            connected_clients.remove(ws)
        except ValueError:
            pass
