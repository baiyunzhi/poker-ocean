# 交易信号展示系统 — 设计文档

> 在期货大师模拟器中增加均线聚散交易信号的实时检测与前端展示

---

## 一、目标

在模拟器的浏览器页面上实时展示基于均线聚散策略的交易信号，包括金叉/死叉、聚合/发散等，通过 WebSocket 从后端推送至前端信号面板。

---

## 二、架构选择

**方案 A（采纳）**：纯后端检测 + WebSocket 推送

- 信号检测逻辑集中在 `main.py` 的 `price_tick()` 循环中
- 前端只负责接收和展示，零信号计算逻辑
- 信号历史通过 REST API 可查

---

## 三、后端设计

### 3.1 MA 周期调整

`/api/kline` 返回的均线字段从 `MA240` 改为 `MA120`：

```python
# 修改前
"MA20": calc_ma(candles, 20),
"MA60": calc_ma(candles, 60),
"MA240": calc_ma(candles, 240),

# 修改后
"MA20": calc_ma(candles, 20),
"MA60": calc_ma(candles, 60),
"MA120": calc_ma(candles, 120),
```

WebSocket `tick` 推送中的 `kline_latest` 保持现有格式不变。

### 3.2 信号检测引擎

**位置**：新增 `detect_signals()` 函数，在 `price_tick()` 中每 tick 调用

**检测的信号类型**：

| 信号类型 | 检测方法 | 严重级别 |
|---------|---------|---------|
| `golden_cross` 金叉 | MA20 > MA60 且上次状态不是金叉 | info |
| `death_cross` 死叉 | MA20 < MA60 且上次状态不是死叉 | warning |
| `convergence` 聚合 | 偏离度 ≤ 3% E 且进入该区间 | info |
| `divergence_start` 发散初期 | 偏离度 > 3% 且 ≤ 5%，进入该区间 | info |
| `divergence_mid` 发散中期 | 偏离度 > 5% 且 ≤ 10%，进入该区间 | warning |
| `divergence_end` 发散末期 | 偏离度 > 10%，进入该区间 | critical |

**偏离度计算公式**：
```
偏离度 = (max(MA20, MA60, MA120) - min(MA20, MA60, MA120)) / median(MA20, MA60, MA120) × 100%
```

### 3.3 状态跟踪（防重复触发）

```python
# 每品种维护信号状态
signal_state: dict = {
    "AU": {
        "cross_state": None,         # "golden" | "death" | None
        "deviation_zone": None,      # "convergence" | "start" | "mid" | "end"
    },
    "SC": { ... },
    "IF": { ... },
}
signal_history: list = []  # 保留最近 200 条信号
```

只有当当前状态与上次不同时才触发新信号，避免重复推送。

### 3.4 信号数据结构

**WebSocket 推送格式**（`type: "signal"`）：

```python
{
    "type": "signal",
    "signals": [
        {
            "id": "sig_1689123456_AU_golden_cross",
            "time": 1689123456,          # Unix timestamp
            "product": "AU",
            "product_name": "黄金",
            "signal_type": "golden_cross",
            "severity": "info",           # info / warning / critical
            "title": "⭐ 金叉信号",
            "detail": "MA20(2580.5) 上穿 MA60(2572.3)",
            "values": {
                "ma20": 2580.5,
                "ma60": 2572.3,
                "ma120": 2560.0,
                "deviation": 0.8,
            }
        }
    ]
}
```

### 3.5 REST API 端点

```
GET /api/signals → 返回 signal_history 列表（最新 200 条）
```

---

## 四、前端设计

### 4.1 页面布局

新增信号面板，位于 K 线图下方、持仓列表上方：

```
┌─────────────────┬─────────────────┐
│    实时行情      │    交易面板      │
├─────────────────┴─────────────────┤
│             K 线图                 │
├───────────────────────────────────┤
│          📡 交易信号 (新增)        │
├───────────────────────────────────┤
│           当前持仓                 │
├───────────────────────────────────┤
│           交易历史                 │
└───────────────────────────────────┘
```

### 4.2 信号面板 UI

- 标题区域：`📡 交易信号` + 信号计数
- 信号卡片列表，最新在上，滚动显示
- 每条信号显示：
  - 左侧：品种标签（AU/SC/IF） + 时间
  - 中间：信号图标 + 标题 + 详细信息
  - 右侧：严重级别色条

**颜色方案**：

| 级别 | 颜色 | 用途 |
|------|------|------|
| `info` | `#3498db` 蓝 | 金叉、聚合、发散初期 |
| `warning` | `#f39c12` 黄 | 死叉、发散中期 |
| `critical` | `#e74c3c` 红 | 发散末期 |

### 4.3 浮动通知

- 新信号到达时，页面右上角弹出 toast
- 3 秒自动消失
- 仅首次到达时弹出，刷新信号面板不重复弹出

### 4.4 WebSocket 处理

在 `ws.onmessage` 中增加 `data.type === 'signal'` 分支：

```javascript
} else if (data.type === 'signal') {
    addSignals(data.signals);
    updateSignalPanel();
    showSignalToast(data.signals[0]);  // 最新一条
}
```

### 4.5 信号加载

- 页面加载时：`fetch('/api/signals')` 加载历史信号
- WebSocket 推送时：增量追加
- 信号列表最多保留 100 条在前端

---

## 五、涉及文件

| 文件 | 改动内容 |
|------|---------|
| `main.py` | 新增 `detect_signals()`、`signal_state`、`signal_history`、`/api/signals` 端点；修改 MA240→MA120；在 `price_tick()` 中调用检测 |
| `templates/index.html` | 新增信号面板 HTML、CSS、JS 渲染函数、浮动通知 |

---

## 六、不变的部分

- 不影响现有 REST API（除 MA240→MA120 外）
- 不影响 WebSocket `tick` 和 `order_result` 消息
- 不影响交易核心逻辑（开仓/平仓/风控）
- 不影响 K 线图功能

---

## 七、检查清单

- [x] 信号类型覆盖（金叉/死叉/聚合/发散四阶段）
- [x] 防重复触发机制（状态跟踪）
- [x] WebSocket 推送格式明确
- [x] REST API 历史查询
- [x] 前端面板布局
- [x] 浮动通知设计
- [x] 颜色编码
- [x] 无成交量信号（用户要求移除）
- [x] MA120 对齐策略文档
