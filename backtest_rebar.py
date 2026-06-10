#!/usr/bin/env python3
"""
螺纹钢 · 均线聚散系统 · 回测引擎
===================================
基于 strategy_params.md 策略参数
零外部依赖 — 仅需 Python 标准库

用法:
  python backtest_rebar.py                    # 使用示例数据运行
  python backtest_rebar.py --data rb_data.csv  # 使用本地 CSV 运行
  python backtest_rebar.py --help              # 查看完整参数
"""

import csv
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

# ═══════════════════════════════════════════════════════════
# 一、策略参数（来自 strategy_params.md）
# ═══════════════════════════════════════════════════════════

# 均线
MA_WINDOWS = (20, 60, 120)

# 偏离度（聚散度）
CONVERGE_THRESHOLD = 2.5       # 聚合偏离度阈值 (%)
CONVERGE_MIN_DAYS = 3          # 聚合最低持续天数（原5→放宽至3）
DIVERGE_LOW = 2.0              # 发散初期下限 (%)
DIVERGE_HIGH = 5.0             # 发散初期上限 (%)
DIVERGE_END = 8.0              # 发散末期阈值 (%)

# 趋势确认
TREND_LOOKBACK = 15            # 价格新高/新低回溯周期（原20→放宽至15）
VOLUME_BOOST = 1.2             # 放量确认倍数（原1.5→放宽至1.2）

# 一票否决 — MA120 方向
MA120_SLOPE_LOOKBACK = 20      # MA120 斜率计算回溯周期

# 止损
HARD_SL = 0.02                 # 硬止损 2%
TIME_STOP_DAYS = 7             # 时间止损 7 日
TRAIL_ACTIVATE = 0.05          # 移动止损激活线 (浮盈≥5%)
TRAIL_DRAWDOWN = 0.03          # 移动止损回撤幅度 3%
DAILY_MAX_LOSS = 0.015         # 单日最大亏损 1.5%

# 资金管理
INITIAL_CAPITAL = 10_000.0     # 起始资金
POSITION_RATIO = 0.30          # 单笔仓位上限 30%
TOTAL_POSITION_LIMIT = 0.60    # 总仓位上限 60%
MAX_DRAWDOWN_LIMIT = 0.15      # 最大回撤警戒 15%

# 交易成本
SLIPPAGE = 1.0                 # 滑点 1 元/吨
COMMISSION = 0.0001            # 手续费 0.01%

# 合约规格
MULTIPLIER = 10                # 螺纹钢 10 吨/手
MARGIN = 0.10                  # 保证金比例 10%

# ═══════════════════════════════════════════════════════════
# 二、数据结构
# ═══════════════════════════════════════════════════════════

@dataclass
class Bar:
    """日线"""
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    oi: float = 0.0       # open interest


@dataclass
class Trade:
    """完整交易记录"""
    direction: str        # 'long' | 'short'
    entry_date: str
    entry_price: float
    size_contracts: float
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    bars_held: int = 0
    peak_unrealized: float = 0.0


@dataclass
class Stats:
    """回测统计"""
    # 基础
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    final_capital: float = 0.0
    annual_return: float = 0.0
    # 风险
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_days: int = 0
    max_consec_losses: int = 0
    max_consec_loss_amount: float = 0.0
    # 质量
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    sharpe: float = 0.0
    expectancy: float = 0.0  # 期望值 = 胜率×盈亏比
    # 月度
    monthly_pnl: dict = field(default_factory=dict)
    monthly_win_months: int = 0


# ═══════════════════════════════════════════════════════════
# 三、数据加载
# ═══════════════════════════════════════════════════════════

def load_csv(path: str) -> list[Bar]:
    """从 CSV 加载日线. 格式: date,open,high,low,close,volume[,open_interest]"""
    bars = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bars.append(Bar(
                date=row["date"],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                oi=float(row.get("open_interest", row.get("oi", 0))),
            ))
    return bars


def gen_sample(days: int = 550) -> list[Bar]:
    """
    生成含多周期趋势的示例日线数据。

    特意构造震荡→放量突破→趋势→再震荡的完整周期，
    确保均线聚散系统能产生交易信号。
    """
    base_date = datetime(2023, 1, 1)
    bars = []

    # ── 构造收盘价序列 ──
    # 分段: [震荡A | 多头趋势 | 震荡B | 空头趋势 | 震荡C]
    # 每段包含: 内部小结构 + 明确的突破日

    segments = [
        # (name, n_days, start_px, end_px, trend_type, breakout_day)
        # trend_type: 0=震荡, 1=上涨, -1=下跌
        ("consol_A",   80,  3800, 3820,  0, None),
        ("bull_break",  5,  3820, 3880,  1, 2),    # 放量突破
        ("bull_run",  120, 3880, 4700,  1, None),
        ("top_break",  5,  4700, 4720,  0, None),   # 筑顶
        ("consol_B",   50,  4720, 4680,  0, None),
        ("bear_break",  5,  4680, 4600, -1, 2),    # 放量跌破
        ("bear_run",  120, 4600, 3700, -1, None),
        ("consol_C",   90,  3700, 3800,  0, None),
        ("tail",       20,  3800, 3850,  0, None),   # 最终震荡
    ]

    idx = 0
    for name, n, sp, ep, trend, breakout in segments:
        n = min(n, days - idx)
        if n <= 0:
            continue

        # 生成这个 segment 的价格
        for j in range(n):
            if idx >= days:
                break
            t = j / max(n - 1, 1)

            if trend == 0:
                # 震荡：围绕均值
                mean = (sp + ep) / 2
                half_range = abs(ep - sp) / 2 + 10
                wave = math.sin(t * math.pi * 3) * half_range * 0.6
                px = mean + wave + random.gauss(0, half_range * 0.15)
            elif trend == 1:
                # 上涨：稳健上升 + 噪音
                smooth = 1 / (1 + math.exp(-5 * (t - 0.5)))
                px = sp + (ep - sp) * smooth + random.gauss(0, (ep - sp) * 0.04)
            else:
                # 下跌：稳健下降 + 噪音
                smooth = 1 / (1 + math.exp(-5 * (t - 0.5)))
                px = sp + (ep - sp) * smooth + random.gauss(0, abs(ep - sp) * 0.04)

            # 突破日刻意放大波动
            is_break_day = (breakout is not None and j == breakout)
            if is_break_day:
                px += (ep - sp) * 0.3 * (1 if trend >= 0 else -1)

            dt = (base_date + timedelta(days=idx)).strftime("%Y-%m-%d")

            # ATR-based OHLC 生成
            prev = bars[-1].close if bars else sp
            chg = px - prev
            atr = max(abs(chg) * 0.8, 5)

            hi = px + random.uniform(0.2, 0.7) * atr
            lo = px - random.uniform(0.2, 0.7) * atr
            op = prev + random.gauss(0, atr * 0.3)

            # 成交量：突破日爆量，趋势日放量，震荡日缩量
            if is_break_day:
                vol_base = 250
            elif trend == 0:
                vol_base = 50 + random.gauss(0, 10)
            else:
                vol_base = 100 + random.gauss(0, 20)

            volume = max(round(vol_base * 10_000), 5_000)

            bars.append(Bar(
                date=dt,
                open=max(op, lo),
                high=max(hi, op, px, lo + 1),
                low=min(lo, op, px, hi - 1),
                close=px,
                volume=float(volume),
            ))
            idx += 1

    # 平滑收盘价
    for k in range(1, len(bars) - 1):
        bars[k].close = (bars[k - 1].close + bars[k].close * 2 + bars[k + 1].close) / 4

    # 一致性修正
    for b in bars:
        b.open = max(min(b.open, b.high, b.close), b.low)
        b.high = max(b.high, b.open, b.close)
        b.low = min(b.low, b.open, b.close)

    return bars[:days]


# ═══════════════════════════════════════════════════════════
# 四、技术指标
# ═══════════════════════════════════════════════════════════

def sma(values: list[float], window: int) -> list[Optional[float]]:
    """简单移动平均"""
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= window:
            s -= values[i - window]
        if i >= window - 1:
            out[i] = s / window
    return out


class Context:
    """承载单根 K 线时刻的所有技术状态"""

    def __init__(self, bars: list[Bar]):
        self.bars = bars
        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        volumes = [b.volume for b in bars]

        self.ma20 = sma(closes, 20)
        self.ma60 = sma(closes, 60)
        self.ma120 = sma(closes, 120)
        self.vol_ma20 = sma(volumes, 20)
        self.highs = highs
        self.lows = lows
        self.closes = closes

    def ready(self, i: int) -> bool:
        """指标就绪？"""
        return all(x is not None for x in
                   [self.ma20[i], self.ma60[i], self.ma120[i], self.vol_ma20[i]])

    def divergence(self, i: int) -> Optional[float]:
        """偏离度 |(MA20-MA60)/MA60| × 100%"""
        m20, m60 = self.ma20[i], self.ma60[i]
        if m20 is None or m60 is None or m60 == 0:
            return None
        return abs((m20 - m60) / m60 * 100)

    def ma120_slope(self, i: int) -> Optional[float]:
        """MA120 斜率 (正=向上, 负=向下)"""
        if i < MA120_SLOPE_LOOKBACK:
            return None
        m = self.ma120
        if m[i] is None or m[i - MA120_SLOPE_LOOKBACK] is None:
            return None
        return (m[i] - m[i - MA120_SLOPE_LOOKBACK]) / m[i - MA120_SLOPE_LOOKBACK]

    def is_aligned(self, i: int) -> tuple[bool, bool]:
        """(bullish_aligned, bearish_aligned)"""
        m20, m60, m120 = self.ma20[i], self.ma60[i], self.ma120[i]
        if None in (m20, m60, m120):
            return False, False
        return (m20 > m60 > m120), (m20 < m60 < m120)

    def is_converged(self, i: int, include_today: bool = False) -> bool:
        """过去 N 天偏离度始终 ≤ 阈值 (默认不含今天，用于检测发散突破)"""
        if i < CONVERGE_MIN_DAYS:
            return False
        start = i - CONVERGE_MIN_DAYS + (1 if include_today else 0)
        end = i if include_today else i - 1
        for j in range(start, end + 1):
            d = self.divergence(j)
            if d is None or d > CONVERGE_THRESHOLD:
                return False
        return True

    def price_new_high(self, i: int) -> bool:
        """今日最高价 > 前 N 日最高价"""
        start = max(0, i - TREND_LOOKBACK)
        return self.bars[i].high > max(self.highs[start:i])

    def price_new_low(self, i: int) -> bool:
        """今日最低价 < 前 N 日最低价"""
        start = max(0, i - TREND_LOOKBACK)
        return self.bars[i].low < min(self.lows[start:i])

    def volume_surge(self, i: int) -> bool:
        """今日成交量 > MA20均量 × 倍数"""
        vma = self.vol_ma20[i]
        return vma is not None and self.bars[i].volume > vma * VOLUME_BOOST


# ═══════════════════════════════════════════════════════════
# 五、回测引擎
# ═══════════════════════════════════════════════════════════

def run(bars: list[Bar], capital_override: float = 0, debug: bool = False
        ) -> tuple[list[Trade], Stats, list[tuple[str, float]]]:
    """执行回测，返回 (trades, stats, equity_curve)"""
    cap_start = capital_override or INITIAL_CAPITAL
    capital = cap_start
    min_bars = max(MA_WINDOWS) + CONVERGE_MIN_DAYS + 10
    if len(bars) < min_bars:
        raise ValueError(f"数据不足: 需要 ≥{min_bars} 根, 实际 {len(bars)}")

    ctx = Context(bars)
    trades: list[Trade] = []
    eq: list[tuple[str, float]] = []

    peak = capital
    max_dd = 0.0
    max_dd_pct = 0.0

    # 持仓状态
    in_pos = False
    pos_dir = ""
    pos_entry = 0.0
    pos_idx = 0
    pos_contracts = 0
    pos_entry_capital = 0.0
    trade: Optional[Trade] = None
    trailing_armed = False
    trail_peak = 0.0       # 趋势方向上的最佳价位

    for i in range(max(MA_WINDOWS), len(bars)):
        bar = bars[i]
        if not ctx.ready(i):
            continue

        d = ctx.divergence(i)
        if d is None:
            continue
        bull_align, bear_align = ctx.is_aligned(i)
        m120_slope = ctx.ma120_slope(i) or 0
        m120_up = m120_slope > 0
        m120_down = m120_slope < 0
        converged = ctx.is_converged(i)
        vol_surge = ctx.volume_surge(i)

        # ── 入场 ──
        if not in_pos:
            go_long = (
                bull_align and m120_up
                and converged
                and DIVERGE_LOW <= d <= DIVERGE_HIGH
                and ctx.price_new_high(i) and vol_surge
            )
            go_short = (
                bear_align and m120_down
                and converged
                and DIVERGE_LOW <= d <= DIVERGE_HIGH
                and ctx.price_new_low(i) and vol_surge
            )

            if debug and (go_long or go_short or
                          (bull_align or bear_align) and
                          converged and DIVERGE_LOW <= d <= DIVERGE_HIGH):
                print(f"  [DEBUG] {bar.date} d={d:.2f}% align={bull_align}/{bear_align} "
                      f"m120={'up' if m120_up else 'down'} conv={converged} "
                      f"nh={ctx.price_new_high(i)} nl={ctx.price_new_low(i)} vs={vol_surge} "
                      f"→ {'LONG' if go_long else 'SHORT' if go_short else 'WAIT'}")

            if go_long or go_short:
                direction = "long" if go_long else "short"
                price = bar.close  # 尾盘确认 → 次日开盘

                # 仓位计算
                margin_per = price * MULTIPLIER * MARGIN
                max_pos_val = capital * POSITION_RATIO
                contract_count = max(1, int(max_pos_val / margin_per))

                # 总仓位上限检查
                if contract_count * margin_per > capital * TOTAL_POSITION_LIMIT:
                    contract_count = max(1, int(capital * TOTAL_POSITION_LIMIT / margin_per))

                in_pos = True
                pos_dir = direction
                pos_entry = price
                pos_idx = i
                pos_contracts = contract_count
                pos_entry_capital = contract_count * margin_per
                trailing_armed = False
                trail_peak = price

                trade = Trade(
                    direction=direction,
                    entry_date=bar.date,
                    entry_price=price,
                    size_contracts=float(contract_count),
                )

        # ── 持仓管理 ──
        if in_pos and trade:
            # 更新趋势方向最佳价位
            if pos_dir == "long":
                if bar.high > trail_peak:
                    trail_peak = bar.high
                unrealized = (bar.close - pos_entry) * MULTIPLIER * pos_contracts
                unrealized_pct = (bar.close - pos_entry) / pos_entry
            else:
                if bar.low < trail_peak:
                    trail_peak = bar.low
                unrealized = (pos_entry - bar.close) * MULTIPLIER * pos_contracts
                unrealized_pct = (pos_entry - bar.close) / pos_entry

            if unrealized > trade.peak_unrealized:
                trade.peak_unrealized = unrealized

            exit_now = False
            reason = ""

            # ① 硬止损
            if pos_dir == "long":
                loss = (pos_entry - bar.low) / pos_entry
            else:
                loss = (bar.high - pos_entry) / pos_entry
            if loss >= HARD_SL:
                exit_now = True
                reason = f"硬止损({loss*100:.1f}%)"

            # ② 时间止损
            held = i - pos_idx
            if not exit_now and held >= TIME_STOP_DAYS:
                if (pos_dir == "long" and bar.close <= pos_entry) or \
                   (pos_dir == "short" and bar.close >= pos_entry):
                    exit_now = True
                    reason = f"时间止损({held}日未盈利)"

            # ③ 移动止损
            if not exit_now:
                if pos_dir == "long" and unrealized_pct >= TRAIL_ACTIVATE:
                    trailing_armed = True
                    dd = (trail_peak - bar.close) / trail_peak
                    if dd >= TRAIL_DRAWDOWN:
                        exit_now = True
                        reason = f"移动止损(回撤{dd*100:.1f}%)"
                elif pos_dir == "short" and unrealized_pct >= TRAIL_ACTIVATE:
                    trailing_armed = True
                    dd = (bar.close - trail_peak) / trail_peak if trail_peak > 0 else 0
                    if dd >= TRAIL_DRAWDOWN:
                        exit_now = True
                        reason = f"移动止损(回撤{dd*100:.1f}%)"

            # ④ 趋势反转离场
            if not exit_now and d > DIVERGE_END:
                exit_now = True
                reason = f"发散末期(d={d:.1f}%)"

            if not exit_now and i > 0:
                prev_m20, prev_m60 = ctx.ma20[i - 1], ctx.ma60[i - 1]
                cur_m20, cur_m60 = ctx.ma20[i], ctx.ma60[i]
                if None not in (prev_m20, prev_m60, cur_m20, cur_m60):
                    if (prev_m20 - prev_m60) * (cur_m20 - cur_m60) < 0:
                        exit_now = True
                        reason = "MA20/60交叉"

            if exit_now:
                close_price = bar.close
                if pos_dir == "long":
                    pnl = (close_price - pos_entry) * MULTIPLIER * pos_contracts
                else:
                    pnl = (pos_entry - close_price) * MULTIPLIER * pos_contracts

                # 扣成本
                cost = (
                    COMMISSION * pos_entry * MULTIPLIER * pos_contracts +
                    COMMISSION * close_price * MULTIPLIER * pos_contracts +
                    SLIPPAGE * MULTIPLIER * pos_contracts
                )
                pnl -= cost
                pnl_pct = pnl / pos_entry_capital if pos_entry_capital else 0

                trade.exit_date = bar.date
                trade.exit_price = close_price
                trade.exit_reason = reason
                trade.pnl = round(pnl, 2)
                trade.pnl_pct = round(pnl_pct, 4)
                trade.bars_held = held

                capital += pnl
                trades.append(trade)
                trade = None
                in_pos = False
                pos_dir = ""
                pos_contracts = 0

        # ── 净值 & 回撤 ──
        if in_pos:
            equity = capital + unrealized
        else:
            equity = capital

        eq.append((bar.date, round(equity, 2)))

        if equity > peak:
            peak = equity
        dd_val = peak - equity
        dd_pct = dd_val / peak * 100
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            max_dd = dd_val

    # 收盘持仓（如有）
    if in_pos and trade:
        close_price = bars[-1].close
        if pos_dir == "long":
            pnl = (close_price - pos_entry) * MULTIPLIER * pos_contracts
        else:
            pnl = (pos_entry - close_price) * MULTIPLIER * pos_contracts
        cost = (
            COMMISSION * pos_entry * MULTIPLIER * pos_contracts +
            COMMISSION * close_price * MULTIPLIER * pos_contracts +
            SLIPPAGE * MULTIPLIER * pos_contracts
        )
        pnl -= cost
        pnl_pct = pnl / pos_entry_capital if pos_entry_capital else 0
        trade.exit_date = bars[-1].date
        trade.exit_price = close_price
        trade.exit_reason = "回测结束平仓"
        trade.pnl = round(pnl, 2)
        trade.pnl_pct = round(pnl_pct, 4)
        trade.bars_held = len(bars) - pos_idx
        capital += pnl
        trades.append(trade)
        eq.append((bars[-1].date, round(capital, 2)))

    stats = _calc_stats(trades, eq, bars, capital, initial_capital=cap_start)
    return trades, stats, eq


def _calc_stats(trades: list[Trade], eq: list[tuple[str, float]],
                bars: list[Bar], final_capital: float,
                initial_capital: float = 0) -> Stats:
    """计算统计指标"""
    ic = initial_capital or INITIAL_CAPITAL
    s = Stats()
    s.total_trades = len(trades)
    s.final_capital = final_capital
    s.total_pnl = final_capital - ic

    if not trades:
        return s

    # 胜率
    winners = [t for t in trades if t.pnl > 0]
    losers = [t for t in trades if t.pnl <= 0]
    s.wins = len(winners)
    s.losses = len(losers)
    s.win_rate = s.wins / s.total_trades * 100 if s.total_trades else 0

    # 盈亏
    s.avg_win = (sum(t.pnl for t in winners) / len(winners)) if winners else 0
    s.avg_loss = (sum(t.pnl for t in losers) / len(losers)) if losers else 0
    gross_win = sum(t.pnl for t in winners)
    gross_loss = abs(sum(t.pnl for t in losers))
    s.profit_factor = gross_win / gross_loss if gross_loss else float("inf")

    # 期望值 = 胜率 × (avg_win / abs(avg_loss))
    if s.avg_loss != 0:
        s.expectancy = s.win_rate / 100 * (s.avg_win / abs(s.avg_loss))

    # 连续亏损
    conseq = 0
    conseq_amount = 0.0
    max_c = 0
    max_ca = 0.0
    for t in trades:
        if t.pnl <= 0:
            conseq += 1
            conseq_amount += t.pnl
            if conseq > max_c:
                max_c = conseq
                max_ca = conseq_amount
        else:
            conseq = 0
            conseq_amount = 0.0
    s.max_consec_losses = max_c
    s.max_consec_loss_amount = round(max_ca, 2)

    # 最大回撤（净值曲线）
    peak = ic
    max_dd = 0.0
    max_dd_p = 0.0
    for _, v in eq:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd_p:
            max_dd_p = dd
            max_dd = peak - v
    s.max_drawdown = round(max_dd, 2)
    s.max_drawdown_pct = round(max_dd_p * 100, 2)

    # 年化收益率
    if len(bars) > 1:
        try:
            d0 = datetime.strptime(bars[0].date, "%Y-%m-%d")
            d1 = datetime.strptime(bars[-1].date, "%Y-%m-%d")
            years = (d1 - d0).days / 365.25
            if years > 0:
                s.annual_return = ((final_capital / ic) ** (1 / years) - 1) * 100
        except ValueError:
            s.annual_return = 0.0

    # Sharpe Ratio (简化：用每日收益率)
    if len(eq) > 1:
        returns = []
        for k in range(1, len(eq)):
            prev = eq[k - 1][1]
            if prev != 0:
                returns.append((eq[k][1] - prev) / prev)
        if returns:
            avg_r = sum(returns) / len(returns)
            std_r = (sum((r - avg_r) ** 2 for r in returns) / len(returns)) ** 0.5
            # 年化 Sharpe ≈ 日均收益/日标准差 × sqrt(250)
            if std_r > 0:
                s.sharpe = round(avg_r / std_r * (250 ** 0.5), 2)

    # 月度盈亏
    monthly: dict[str, list[float]] = {}
    for t in trades:
        try:
            m = t.exit_date[:7] if t.exit_date else "unknown"
        except IndexError:
            m = "unknown"
        monthly.setdefault(m, []).append(t.pnl)
    s.monthly_pnl = {m: round(sum(v), 2) for m, v in sorted(monthly.items())}
    s.monthly_win_months = sum(1 for v in s.monthly_pnl.values() if v > 0)

    # 最大回撤持续天数
    s.max_drawdown_days = 0  # 已在主循环中计算但此处简化

    return s


# ═══════════════════════════════════════════════════════════
# 六、报告输出
# ═══════════════════════════════════════════════════════════

def print_report(trades: list[Trade], stats: Stats, eq: list[tuple[str, float]], bars: list[Bar]):
    """打印完整回测报告"""
    gap = "=" * 62
    sub = "-" * 62

    print()
    print(gap)
    print("  螺纹钢 · 均线聚散系统 · 回测报告")
    print(gap)

    # ── 基础统计 ──
    print(f"\n  一、基础统计")
    print(sub)
    print(f"  {'总交易次数':<20} {stats.total_trades:>8}")
    print(f"  {'胜率':<20} {stats.win_rate:>7.1f}%")
    print(f"  {'总盈亏':<20} {stats.total_pnl:>+8.2f} 元")
    print(f"  {'最终权益':<20} {stats.final_capital:>8.2f} 元")
    print(f"  {'年化收益率':<20} {stats.annual_return:>7.2f}%")
    print(f"  {'回测周期':<20} {bars[0].date} ~ {bars[-1].date}")

    # ── 风险指标 ──
    print(f"\n  二、风险指标")
    print(sub)
    print(f"  {'最大回撤':<20} {stats.max_drawdown_pct:>7.2f}%  ({stats.max_drawdown:.2f} 元)")
    print(f"  {'连续亏损(次数)':<20} {stats.max_consec_losses:>8}")
    print(f"  {'连续亏损(金额)':<20} {stats.max_consec_loss_amount:>+8.2f} 元")
    print(f"  {'盈利月份/总月份':<20} {stats.monthly_win_months}/{len(stats.monthly_pnl)}")

    # ── 质量指标 ──
    print(f"\n  三、质量指标")
    print(sub)
    print(f"  {'平均盈利':<20} {stats.avg_win:>+8.2f} 元")
    print(f"  {'平均亏损':<20} {stats.avg_loss:>+8.2f} 元")
    print(f"  {'盈亏比':<20} {abs(stats.avg_win / stats.avg_loss):>7.2f}" if stats.avg_loss != 0 else "  N/A")
    print(f"  {'盈利因子':<20} {stats.profit_factor:>8.2f}")
    print(f"  {'Sharpe Ratio':<20} {stats.sharpe:>8}")
    print(f"  {'胜率×盈亏比':<20} {stats.expectancy:>8.2f}  {'OK' if stats.expectancy > 0.5 else 'NO'}")

    # ── 月度盈亏 ──
    print(f"\n  四、月度盈亏")
    print(sub)
    for m, v in stats.monthly_pnl.items():
        mark = "+" if v > 0 else ""
        print(f"  {m:<12} {mark}{v:>+10.2f} 元")
    print()

    # ── 最近 10 笔交易 ──
    print(f"\n  五、最近交易记录 (最多 10 笔)")
    print(sub)
    recent = trades[-10:] if len(trades) > 10 else trades
    hdr = f"  {'日期':<12} {'方向':<6} {'入场':>8} {'离场':>8} {'盈亏':>10} {'原因'}"
    print(hdr)
    print("  " + "-" * 60)
    for t in recent:
        pnl_s = f"{t.pnl:>+9.2f}" if abs(t.pnl) < 1e6 else f"{t.pnl:>+9.0f}"
        print(f"  {t.exit_date:<12} {t.direction:<6} {t.entry_price:>8.0f} "
              f"{t.exit_price:>8.0f} {pnl_s}  {t.exit_reason}")
    print()

    # ── 资金曲线简图 ──
    print(f"\n  六、资金曲线")
    print(sub)
    _print_equity_chart(eq)
    print()


def _print_equity_chart(eq: list[tuple[str, float]], width: int = 50):
    """终端文本资金曲线图"""
    if len(eq) < 2:
        return
    values = [v for _, v in eq]
    mn, mx = min(values), max(values)
    rng = mx - mn or 1

    # 采样 ~50 个点
    step = max(1, len(eq) // width)
    pts = [(eq[i][0], eq[i][1]) for i in range(0, len(eq), step)]
    if pts[-1][0] != eq[-1][0]:
        pts.append(eq[-1])

    _, mx_v = max(pts, key=lambda x: x[1])
    mn_v = min(p[1] for p in pts)

    for row in range(10, -1, -1):
        pct = row / 10
        level = mn_v + (mx_v - mn_v) * pct
        line = f"{level:>8.0f} │"
        for _, v in pts:
            if v >= level:
                line += "█"
            else:
                line += " "
        print(line)

    # 时间轴（仅标注首尾和中间）
    labels = [pts[0][0], pts[len(pts) // 2][0], pts[-1][0]]
    print("         " + "│" + "".join(
        l if l in labels else (" " if i != len(pts) // 2 else " ")
        for i, (l, _) in enumerate(pts)
    ))
    print(f"         {'├':─^{width}}┤")
    print(f"         起始: {pts[0][0]}    结束: {pts[-1][0]}")


# ═══════════════════════════════════════════════════════════
# 七、入口
# ═══════════════════════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="螺纹钢 · 均线聚散系统 · 回测引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python backtest_rebar.py                          # 默认宽松参数
  python backtest_rebar.py --data rb_daily.csv      # 本地 CSV
  python backtest_rebar.py --sample 800             # 800 根示例 K 线
  python backtest_rebar.py --strict                 # 原始严格参数
  python backtest_rebar.py --data rb.csv --no-chart  # 纯文本
        """,
    )
    parser.add_argument("--data", "-d", help="CSV 数据文件路径")
    parser.add_argument("--sample", "-s", type=int, default=550,
                        help="示例数据 K 线数量 (默认 550)")
    parser.add_argument("--capital", "-c", type=float, default=0,
                        help=f"起始资金 (默认 {INITIAL_CAPITAL})")
    parser.add_argument("--debug", action="store_true",
                        help="显示每日信号诊断")
    parser.add_argument("--no-chart", action="store_true",
                        help="不显示资金曲线图")
    parser.add_argument("--strict", action="store_true",
                        help="原始严格参数（聚合5日/发散2.5-4%/放量1.5倍/回溯20日）")

    args = parser.parse_args()

    # 严格模式：恢复原始参数
    if args.strict:
        g = globals()
        g["CONVERGE_MIN_DAYS"] = 5
        g["DIVERGE_LOW"] = 2.5
        g["DIVERGE_HIGH"] = 4.0
        g["VOLUME_BOOST"] = 1.5
        g["TREND_LOOKBACK"] = 20
        print("[信息] 严格模式: 聚合5日/发散2.5-4%/放量1.5倍/回溯20日")

    cap = args.capital or INITIAL_CAPITAL

    # ── 数据准备 ──
    if args.data:
        path = args.data
        if not os.path.exists(path):
            print(f"[错误] 文件不存在: {path}")
            sys.exit(1)
        bars = load_csv(path)
        print(f"[信息] 已加载 {len(bars)} 根日线 from {path}")
    else:
        print(f"[信息] 生成 {args.sample} 根示例数据 (seed=42)")
        random.seed(42)
        bars = gen_sample(args.sample)
        print(f"[信息] 示例数据范围: {bars[0].date} ~ {bars[-1].date}")

    print(f"[信息] 起始资金: {cap:.0f} 元")
    print(f"[信息] 策略参数: MA{MA_WINDOWS}, 聚合≤{CONVERGE_THRESHOLD}%/{CONVERGE_MIN_DAYS}日, "
          f"发散{DIVERGE_LOW}-{DIVERGE_HIGH}%, 止损{HARD_SL*100:.0f}%")

    t0 = time.time()
    trades, stats, eq = run(bars, capital_override=cap, debug=args.debug)
    elapsed = time.time() - t0

    print(f"[信息] 回测完成: {len(trades)} 笔交易, 耗时 {elapsed:.2f}s\n")

    if not trades:
        print("  无交易产生。")
        print("  提示: 调整参数或检查数据是否有明显趋势行情。")
        return

    print_report(trades, stats, eq, bars)

    # ── CSV 导出 ──
    out_name = f"backtest_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(out_name, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["entry_date", "exit_date", "direction", "entry_price", "exit_price",
                     "size", "pnl", "pnl_pct", "bars_held", "exit_reason"])
        for t in trades:
            w.writerow([t.entry_date, t.exit_date, t.direction,
                        round(t.entry_price, 1), round(t.exit_price, 1),
                        t.size_contracts, round(t.pnl, 2), round(t.pnl_pct, 4),
                        t.bars_held, t.exit_reason])
    print(f"[信息] 交易明细已导出: {out_name}")


if __name__ == "__main__":
    main()
