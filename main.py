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
    "AU":  {"name": "黄金",   "price": 2350.0, "multiplier": 1000, "volatility": 0.004, "margin_rate": 0.08, "base_volume": 1200, "base_oi": 80000},
    "SC":  {"name": "原油",   "price": 520.0,  "multiplier": 1000, "volatility": 0.006, "margin_rate": 0.10, "base_volume": 8000, "base_oi": 60000},
    "IF":  {"name": "沪深300", "price": 3850.0, "multiplier": 300,  "volatility": 0.005, "margin_rate": 0.12, "base_volume": 5000, "base_oi": 100000},
}

# ─── 市场状态（趋势结构引擎）─────────────────────────────────
# Regime: "bull" / "consolidation" / "bear"
# 每个品种维护独立状态机，慢速切换形成有延续性的趋势段
market_context: dict = {}  # { pid: { regime, duration, trend_strength, ... } }
REGIME_CONFIG = {
    "bull":           {"min_duration": 400, "vol_mult": (1.5, 2.0), "drift_range": (0.3, 0.6), "switch_weight": 0.3},
    "consolidation":  {"min_duration": 200, "vol_mult": (0.5, 0.8), "drift_range": (-0.05, 0.05), "switch_weight": 0.5},
    "bear":           {"min_duration": 400, "vol_mult": (1.5, 2.0), "drift_range": (-0.6, -0.3), "switch_weight": 0.3},
}

def init_market_context():
    """初始化或重置所有品种的市场状态"""
    for pid in PRODUCTS:
        market_context[pid] = {
            "regime": random.choice(["bull", "consolidation", "bear"]),
            "duration": 0,
            "trend_strength": 0.0,
            "peak_strength": 0.0,
            "oi_cycle": 0.5,   # 0~1, OI 在趋势中的位置
        }

def maybe_switch_regime(pid: str) -> bool:
    """在 K 线闭合���检查是否需要切换市场状态"""
    ctx = market_context[pid]
    cfg = REGIME_CONFIG[ctx["regime"]]
    # 最小持续时长未到 → 不切换
    if ctx["duration"] < cfg["min_duration"]:
        return False
    # 按概率掷骰切换
    if random.random() < cfg["switch_weight"]:
        # 从 consolidation 可切换至 bull 或 bear；从 bull/bear 只能回 consolidation
        if ctx["regime"] == "consolidation":
            new_regime = random.choices(
                ["bull", "bear"],
                weights=[REGIME_CONFIG["bull"]["switch_weight"],
                         REGIME_CONFIG["bear"]["switch_weight"]],
            )[0]
        else:
            new_regime = "consolidation"
        # 设定新状态参数
        rc = REGIME_CONFIG[new_regime]
        drift_lo, drift_hi = rc["drift_range"]
        ctx["regime"] = new_regime
        ctx["duration"] = 0
        ctx["peak_strength"] = random.uniform(drift_lo, drift_hi)
        ctx["trend_strength"] = ctx["peak_strength"]
        ctx["oi_cycle"] = 0.0 if new_regime != "consolidation" else 0.5
        return True
    return False

def get_current_candle_volume_oi(pid: str, candle_open: float, candle_close: float,
                                  candle_high: float, candle_low: float) -> tuple:
    """基于当前蜡烛特征和市场状态，生成成交量和持��量"""
    ctx = market_context[pid]
    cfg = PRODUCTS[pid]
    rc = REGIME_CONFIG[ctx["regime"]]
    vol_mult_lo, vol_mult_hi = rc["vol_mult"]
    vol_mult = random.uniform(vol_mult_lo, vol_mult_hi)
    # 价格波动幅度越大 → 成交量越大
    price_range = (candle_high - candle_low) / max(candle_open, 1)
    range_factor = 1.0 + price_range / cfg["volatility"] * 2
    volume = int(cfg["base_volume"] * vol_mult * range_factor * random.lognormvariate(0, 0.3))
    volume = max(volume, 1)
    # OI 跟踪趋势生命周期
    if ctx["regime"] in ("bull", "bear"):
        progress = ctx["oi_cycle"]  # 0~1
        if progress < 0.3:
            oi_adj = 0.0 + progress / 0.3 * 0.3    # 0 → 0.3 快速增长
        elif progress < 0.7:
            oi_adj = 0.3                             # 高位平台
        else:
            oi_adj = 0.3 * (1.0 - (progress - 0.7) / 0.3)  # 0.3 → 0 下降
        # OI 周期推进
        ctx["oi_cycle"] = min(ctx["oi_cycle"] + 0.005 * random.uniform(0.5, 1.5), 1.0)
    else:
        oi_adj = random.uniform(-0.1, 0.0)  # 震荡期 OI 轻微萎缩
    oi_value = int(cfg["base_oi"] * (1 + oi_adj * random.uniform(0.8, 1.2)))
    oi_value = max(oi_value, 1000)
    return volume, oi_value

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

def create_regime_sequence(count: int) -> list:
    """创建包含牛→震→熊周期的历史 regime 序列"""
    patterns = [
        [("bear", 0.30), ("consolidation", 0.15), ("bull", 0.35), ("consolidation", 0.20)],
        [("bull", 0.35), ("consolidation", 0.15), ("bear", 0.30), ("consolidation", 0.20)],
        [("consolidation", 0.15), ("bull", 0.30), ("consolidation", 0.15), ("bear", 0.25), ("consolidation", 0.15)],
    ]
    pattern = random.choice(patterns)
    result = []
    for regime, ratio in pattern:
        n = max(int(count * ratio), 30)
        result.append((regime, n, random.uniform(*REGIME_CONFIG[regime]["drift_range"])))
    total = sum(n for _, n, _ in result)
    if total < count:
        result.append(("consolidation", count - total, 0.0))
    elif total > count:
        diff = total - count
        result[-1] = (result[-1][0], result[-1][1] - diff, result[-1][2])
    return result


def generate_candles(pid: str, interval: str, count: int, regime_sequence: list = None) -> list:
    """
    生成带趋势结构和量仓数据的 K 线。
    regime_sequence: [(regime, length, drift_strength), ...]
    """
    cfg = PRODUCTS[pid]
    interval_sec = HOUR_SECS if interval == "1h" else DAY_SECS
    now = int(datetime.now(timezone.utc).timestamp())
    start = now - (now % interval_sec)
    candles = []
    price = cfg["price"]
    base = cfg["price"]
    if regime_sequence is None:
        regime_sequence = create_regime_sequence(count)
    oi_value = cfg["base_oi"]
    for regime, length, drift_strength in regime_sequence:
        rc = REGIME_CONFIG[regime]
        vol_mult = random.uniform(*rc["vol_mult"])
        peak_strength = drift_strength
        for i in range(length):
            t = start - (count - len(candles) - length + i) * interval_sec
            # 趋势漂移：初段最强 → 逐渐衰减
            progress = i / length
            if regime in ("bull", "bear"):
                drift = peak_strength * (1 - progress * 0.85)
            else:
                drift = random.uniform(*rc["drift_range"])
            # 价格生成
            vol_scalar = cfg["volatility"] * vol_mult * math.sqrt(interval_sec / HOUR_SECS)
            reversion = 0.005 * (base - price) / max(price, 1)
            chg = drift * cfg["volatility"] + reversion + random.gauss(0, 1) * vol_scalar
            o = round(price, 2)
            c = round(price * (1 + chg), 2)
            h = round(max(o, c) * (1 + abs(random.gauss(0, 1)) * vol_scalar * 0.3), 2)
            l = round(min(o, c) * (1 - abs(random.gauss(0, 1)) * vol_scalar * 0.3), 2)
            # 成交量
            price_range = (h - l) / max(o, 1)
            range_factor = 1.0 + price_range / max(cfg["volatility"], 0.0001) * 2
            volume = int(cfg["base_volume"] * vol_mult * range_factor * random.lognormvariate(0, 0.3))
            volume = max(volume, 1)
            # 持仓量：跟随趋势生命周期
            if regime in ("bull", "bear"):
                oi_progress = min(progress * 2, 1.0)  # 0→1 覆盖前半段
                if progress > 0.5:
                    oi_progress = 1.0 - (progress - 0.5) * 2  # 1→0 在后半段
                oi_value = int(cfg["base_oi"] * (1 + oi_progress * 0.3 * random.uniform(0.8, 1.2)))
            else:
                oi_factor = random.uniform(-0.1, 0.05)
                oi_value = int(cfg["base_oi"] * (1 + oi_factor))
            oi_value = max(oi_value, 1000)
            candles.append({
                "time": t, "open": o, "high": h, "low": l, "close": c,
                "volume": volume, "oi": oi_value,
            })
            price = c
            if interval == "1h" and i > 0 and i % 600 == 0:
                base = price
            elif interval == "1d" and i > 0 and i % 60 == 0:
                base = price
    cfg["price"] = price
    return candles

def init_kline():
    """初始化 K 线数据：用有趋势结构的 regime 序列代替随机游走"""
    now = int(datetime.now(timezone.utc).timestamp())
    init_market_context()
    for pid in PRODUCTS:
        cfg = PRODUCTS[pid]
        base = cfg["price"]
        # ── 1h candles: 带趋势结构的蜡烛序列 ──
        seq_1h = create_regime_sequence(500)
        cfg["price"] = base * (1 + random.uniform(-0.02, 0.02))
        kline_data[pid] = {"1h": generate_candles(pid, "1h", 500, seq_1h)}
        hourly_last_close = cfg["price"]

        # ── 1d candles: 独立的日线序列 ──
        seq_1d = create_regime_sequence(300)
        cfg["price"] = base * (1 + random.uniform(-0.02, 0.02))
        kline_data[pid]["1d"] = generate_candles(pid, "1d", 300, seq_1d)

        # ── Live price aligns with last hourly close ──
        cfg["price"] = hourly_last_close

        # ── Forming candles ──
        for interval in ("1h", "1d"):
            key = f"{pid}_{interval}"
            interval_sec = HOUR_SECS if interval == "1h" else DAY_SECS
            period_start = now - (now % interval_sec)
            candles = kline_data[pid][interval]
            last = candles[-1]
            start_price = hourly_last_close if interval == "1h" else last["close"]
            current_candle[key] = {
                "time": period_start,
                "open": start_price,
                "high": start_price,
                "low": start_price,
                "close": start_price,
                "volume": 0,
                "oi": cfg["base_oi"],
            }

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
    """更新 forming candle 的价格/量/仓，在闭合时生成完整量仓数据"""
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
            # 在 candle 闭合时生成量/仓数据
            vol, oi = get_current_candle_volume_oi(pid, cc["open"], cc["close"], cc["high"], cc["low"])
            cc["volume"] = vol
            cc["oi"] = oi
            # 每小时检查 regime 切换（日线闭合时不再重复检查）
            if interval == "1h":
                maybe_switch_regime(pid)
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
                "volume": 0,
                "oi": cc.get("oi", PRODUCTS[pid]["base_oi"]),
            }
        else:
            cc["high"] = max(cc["high"], price)
            cc["low"] = min(cc["low"], price)
            cc["close"] = price

# ─── 价格模拟引擎（带趋势结构）────────────────────────────
def update_price(pid: str, cfg: dict) -> float:
    """根据当前市场状态生成带趋势漂移的价格"""
    cur = cfg["price"]
    base = PRODUCTS[pid]["price"]
    ctx = market_context[pid]
    # 更新状态机计时
    ctx["duration"] += 1
    # 趋势衰减：随时间推移趋势强度逐渐归零
    rc = REGIME_CONFIG[ctx["regime"]]
    if ctx["regime"] in ("bull", "bear"):
        decay_rate = 1.0 / max(rc["min_duration"] * 1.5, 1)
        ctx["trend_strength"] = ctx["peak_strength"] * max(0, 1 - ctx["duration"] * decay_rate)
    else:
        ctx["trend_strength"] = random.uniform(*rc["drift_range"])
    # 趋势漂移 + 均值回复 + 随机冲击
    trend = ctx["trend_strength"] * cfg["volatility"]
    reversion = 0.003 * (base - cur) / max(base, 1)
    shock = random.gauss(0, 1) * cfg["volatility"]
    new_price = cur * (1 + trend + reversion + shock)
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
        # 每个品种的活跃蜡烛量仓预热（forming candle 中 volume 按进度估算）
        now_s = int(datetime.now(timezone.utc).timestamp())
        kline_latest = {}
        for pid in PRODUCTS:
            hourly = dict(current_candle.get(f"{pid}_1h", {}))
            daily = dict(current_candle.get(f"{pid}_1d", {}))
            # 为 forming candle 估算 volume 进度（已过秒数/3600）
            interval_start = hourly.get("time", now_s)
            progress = max(0.05, min(1.0, (now_s - interval_start) / HOUR_SECS))
            if hourly and hourly.get("volume", 0) == 0:
                hourly["volume"] = max(1, int(PRODUCTS[pid]["base_volume"] * progress * random.uniform(0.2, 0.6)))
            if daily and daily.get("volume", 0) == 0:
                daily["volume"] = max(1, int(PRODUCTS[pid]["base_volume"] * progress * 0.1))
            kline_latest[pid] = {"1h": hourly, "1d": daily}

        await broadcast({
            "type": "tick",
            "market": market,
            "kline_latest": kline_latest,
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
    market_context.clear()
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
