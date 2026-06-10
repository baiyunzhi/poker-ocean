# 交易信号展示系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在期货大师模拟器中实时检测并展示均线聚散交易信号（金叉/死叉、聚合/发散）

**Architecture:** 后端 `main.py` 中新增信号检测引擎 `detect_signals()`，每 tick 调用，通过 WebSocket 推送信号到前端；前端新增信号面板展示实时信号列表 + 浮动通知

**Tech Stack:** Python 3.14 / FastAPI / WebSocket / Vanilla JS

---

## File Structure

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `main.py` | Modify | MA240→MA120; 新增 `signal_state`, `signal_history`, `detect_signals()`; 在 `price_tick()` 中调用; 新增 `/api/signals` 端点; `reset_account()` 中清空信号 |
| `templates/index.html` | Modify | 新增信号面板 HTML+CSS; 新增 JS 信号渲染函数; WS onmessage 增加 signal 分支; 浮动 toast 通知; K线 MA240→MA120 |

---

### Task 1: 后端 — MA120 对齐 + 信号状态容器

**Files:**
- Modify: `main.py:44-64` (PRODUCTS 下方插入 signal_state/signal_history)
- Modify: `main.py:490-498` (MA240→MA120)
- Modify: `templates/index.html:297-299` (MA240→MA120 图例)
- Modify: `templates/index.html:557` (变量名)
- Modify: `templates/index.html:626-648` (Series 名称)

- [ ] **Step 1: 信号状态容器 — 在 main.py PRODUCTS 定义后插入**

在 `main.py` 第 49 行（`PRODUCTS` 定义结束）后、第 52 行（`account`）前，插入信号状态和信号历史容器：

```python
# ─── 交易信号 ─────────────────────────────────────────────
signal_state: dict = {}  # { pid: { "cross_state": ..., "deviation_zone": ... } }
signal_history: list = []  # list of signal dicts, max 200
```

- [ ] **Step 2: 修改 /api/kline 返回 MA120 替代 MA240**

在 `main.py:496`:

```python
# 修改前
"MA240": calc_ma(candles, 240),

# 修改后
"MA120": calc_ma(candles, 120),
```

- [ ] **Step 3: index.html K线图例 MA240→MA120**

```html
<!-- 修改前 第297-299行 -->
<span style="color:#f0b90b">MA20</span>
<span style="color:#e91e63;margin-left:8px">MA60</span>
<span style="color:#7c4dff;margin-left:8px">MA240</span>

<!-- 修改后 -->
<span style="color:#f0b90b">MA20</span>
<span style="color:#e91e63;margin-left:8px">MA60</span>
<span style="color:#7c4dff;margin-left:8px">MA120</span>
```

- [ ] **Step 4: index.html JS 变量名 MA240→MA120**

```javascript
// 第557行 — 变量声明
let ma120Series = null;  // 原 ma240Series

// 第626-631行 — Series 创建
ma120Series = chart.addLineSeries({  // 原 ma240Series
  color: '#7c4dff',
  lineWidth: 1,
  lastValueVisible: false,
  priceLineVisible: false,
});

// 第648行 — setData
ma120Series.setData(data.mas.MA120 || []);  // 原 ma240Series / MA240
```

---

### Task 2: 后端 — 信号检测引擎 detect_signals()

**Files:**
- Modify: `main.py:309-318` (在 calc_ma 后插入检测逻辑)

- [ ] **Step 1: 编写 detect_signals() 函数**

在 `calc_ma` 函数后（第318行后）、`# ─── WebSocket 连接管理`（第320行）前，插入：

```python
import statistics

def calc_deviation(ma20_val: float, ma60_val: float, ma120_val: float) -> float:
    """计算偏离度 = (max - min) / median × 100%"""
    vals = [ma20_val, ma60_val, ma120_val]
    ma_min = min(vals)
    ma_max = max(vals)
    ma_median = statistics.median(vals)
    if ma_median == 0:
        return 0.0
    return round((ma_max - ma_min) / ma_median * 100, 2)


def get_ma_values(pid: str) -> tuple:
    """获取当前品种的最新 MA20/MA60/MA120 值"""
    candles_1h = kline_data.get(pid, {}).get("1h", [])
    if len(candles_1h) < 120:
        return None, None, None
    ma20_list = calc_ma(candles_1h, 20)
    ma60_list = calc_ma(candles_1h, 60)
    ma120_list = calc_ma(candles_1h, 120)
    if not ma20_list or not ma60_list or not ma120_list:
        return None, None, None
    return ma20_list[-1]["value"], ma60_list[-1]["value"], ma120_list[-1]["value"]


def compute_deviation_zone(deviation: float) -> str:
    """确定偏离度所在区间"""
    if deviation <= 3.0:
        return "convergence"     # 聚合
    elif deviation <= 5.0:
        return "divergence_start"  # 发散初期
    elif deviation <= 10.0:
        return "divergence_mid"    # 发散中期
    else:
        return "divergence_end"    # 发散末期


SIGNAL_META = {
    "golden_cross": {
        "severity": "info",
        "title": "⭐ 金叉信号",
        "icon": "🟢",
    },
    "death_cross": {
        "severity": "warning",
        "title": "💀 死叉信号",
        "icon": "🔴",
    },
    "convergence": {
        "severity": "info",
        "title": "📊 聚合信号",
        "icon": "🔵",
    },
    "divergence_start": {
        "severity": "info",
        "title": "🚀 发散初期",
        "icon": "🟠",
    },
    "divergence_mid": {
        "severity": "warning",
        "title": "🔥 发散中期",
        "icon": "🟡",
    },
    "divergence_end": {
        "severity": "critical",
        "title": "⚠️ 发散末期",
        "icon": "🔴",
    },
}


def detect_signals() -> list:
    """检测所有品种的交易信号，返回新触发的信号列表"""
    new_signals = []
    
    for pid in PRODUCTS:
        ma20, ma60, ma120 = get_ma_values(pid)
        if ma20 is None:
            continue
        
        cfg = PRODUCTS[pid]
        deviation = calc_deviation(ma20, ma60, ma120)
        zone = compute_deviation_zone(deviation)
        
        # ── 判断金叉/死叉 ──
        # 金叉: MA20 > MA60
        # 死叉: MA20 < MA60
        # 注意: MA20 == MA60 保留原状态, 不触发新信号
        threshold = 0.05  # 5分钱容差避免反复切换
        if ma20 > ma60 + threshold:
            new_cross = "golden"
        elif ma20 < ma60 - threshold:
            new_cross = "death"
        else:
            new_cross = signal_state.get(pid, {}).get("cross_state", None)
        
        prev_state = signal_state.get(pid, {"cross_state": None, "deviation_zone": None})
        
        # 检测交叉状态变化
        if prev_state["cross_state"] != new_cross and new_cross in ("golden", "death"):
            signal_type = "golden_cross" if new_cross == "golden" else "death_cross"
            meta = SIGNAL_META[signal_type]
            detail = (
                f"MA20({ma20}) {'上穿' if new_cross == 'golden' else '下穿'} MA60({ma60})"
            )
            sig = {
                "id": f"sig_{int(datetime.now().timestamp())}_{pid}_{signal_type}",
                "time": int(datetime.now().timestamp()),
                "product": pid,
                "product_name": cfg["name"],
                "signal_type": signal_type,
                "severity": meta["severity"],
                "title": meta["title"],
                "detail": detail,
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
```

**重要**：在文件顶部 `import math` 旁边追加 `import statistics`（不要重复 import）。

- [ ] **Step 2: 在 price_tick() 中调用 detect_signals() 并广播**

在 `main.py:357`（`check_liquidation()` 调用后）、第 359 行（`# Broadcast` 注释前）插入：

```python
        # Detect and broadcast trading signals
        new_signals = detect_signals()
        if new_signals:
            asyncio.create_task(broadcast({"type": "signal", "signals": new_signals}))
```

- [ ] **Step 3: 在 reset_account() 中清空信号**

在 `main.py:487`（`init_kline()` 调用后）、第 488 行（`for pid, price` 循环前）插入：

```python
    signal_state.clear()
    signal_history.clear()
```

---

### Task 3: 后端 — /api/signals REST 端点

**Files:**
- Modify: `main.py:490-498` (在 /api/kline 后添加)

- [ ] **Step 1: 添加 GET /api/signals**

在 `main.py` 的 `get_kline_api` 函数后（第498行后）、`# ─── WebSocket`（第500行）前插入：

```python
@app.get("/api/signals")
def get_signals(limit: int = 100):
    return list(reversed(signal_history[-limit:]))
```

---

### Task 4: 前端 — 信号面板 HTML + CSS

**Files:**
- Modify: `templates/index.html:315-322` (在持仓和交易历史之间插入信号面板)

- [ ] **Step 1: 在持仓前插入信号面板 HTML**

在第303行（`</div>` 结束 K线面板）后、第305行（`<!-- Positions -->`）前插入：

```html
  <!-- Trading Signals -->
  <div class="panel full-width">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <h3 style="margin:0;padding:0;border:none">📡 交易信号</h3>
      <span id="signalBadge" style="font-size:11px;color:#6b7280">等待信号...</span>
    </div>
    <div class="signal-scroll" id="signalContainer">
      <div class="empty-state">暂无交易信号</div>
    </div>
  </div>
```

- [ ] **Step 2: 新增信号面板 CSS**

在 `index.html` 的 `/* ─── History ─────────────────── */` 区块后（第193行后）、`/* ─── Responsive ──────────────── */` 前插入：

```css
/* ─── Signals ─────────────────── */
.signal-scroll{max-height:260px;overflow-y:auto}
.signal-scroll::-webkit-scrollbar{width:4px}
.signal-scroll::-webkit-scrollbar-thumb{background:#1f2530;border-radius:2px}
.signal-item{
  display:flex;align-items:center;gap:12px;
  padding:10px 12px;
  border-radius:6px;
  margin-bottom:6px;
  background:#0b0e14;
  border-left:3px solid #6b7280;
  transition:opacity .3s;
}
.signal-item.new-signal{animation:signalFlash .5s ease}
@keyframes signalFlash{
  0%{opacity:.5;transform:translateX(-4px)}
  100%{opacity:1;transform:translateX(0)}
}
.signal-item:last-child{margin-bottom:0}
.signal-item.severity-info{border-left-color:#3498db}
.signal-item.severity-warning{border-left-color:#f39c12}
.signal-item.severity-critical{border-left-color:#e74c3c}
.signal-time{font-size:11px;color:#6b7280;white-space:nowrap;min-width:60px}
.signal-prod-tag{
  font-size:11px;font-weight:600;
  padding:2px 6px;border-radius:3px;
  background:#1f2530;color:#5e7ce0;
  white-space:nowrap;min-width:36px;text-align:center;
}
.signal-body{flex:1;min-width:0}
.signal-title{font-size:13px;font-weight:600;color:#e8ecf0}
.signal-detail{font-size:11px;color:#6b7280;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.signal-badge{
  font-size:10px;padding:2px 6px;border-radius:3px;
  white-space:nowrap;font-weight:600;
}
.signal-badge.info{background:rgba(52,152,219,.15);color:#3498db}
.signal-badge.warning{background:rgba(243,156,18,.15);color:#f39c12}
.signal-badge.critical{background:rgba(231,76,60,.15);color:#e74c3c}

/* ─── Signal Toast ─────────────── */
.signal-toast{
  position:fixed;top:16px;right:16px;z-index:9999;
  padding:12px 20px;border-radius:8px;
  background:#1a1f2a;border:1px solid #1f2530;
  box-shadow:0 8px 32px rgba(0,0,0,.4);
  max-width:360px;
  animation:toastIn .3s ease,toastOut .3s ease 2.7s forwards;
  pointer-events:none;
}
.signal-toast .toast-title{font-size:14px;font-weight:600;margin-bottom:4px}
.signal-toast .toast-detail{font-size:12px;color:#6b7280}
@keyframes toastIn{0%{opacity:0;transform:translateX(40px)}100%{opacity:1;transform:translateX(0)}}
@keyframes toastOut{0%{opacity:1}100%{opacity:0;transform:translateY(-10px)}}
```

---

### Task 5: 前端 — 信号展示 JS 逻辑

**Files:**
- Modify: `templates/index.html:425-447` (WebSocket onmessage 分支)
- Modify: `templates/index.html:690-698` (Init 中加载历史信号)

- [ ] **Step 1: WebSocket onmessage 新增 signal 分支**

在第437行（`} else if (data.type === 'order_result') {`）之前插入：

```javascript
    } else if (data.type === 'signal') {
      addSignals(data.signals);
      data.signals.forEach(s => showSignalToast(s));
```

- [ ] **Step 2: 新增信号 JS 函数**

在 `closePosition` 函数后（第417行后）、`resetAccount`（第420行）前插入：

```javascript
// ─── Signal State ────────────────
let allSignals = [];

function addSignals(newSignals) {
  for (const s of newSignals) {
    // 去重：检查 id 是否已存在
    if (allSignals.some(ex => ex.id === s.id)) continue;
    allSignals.unshift(s);
  }
  // 最多保留 100 条
  if (allSignals.length > 100) allSignals.length = 100;
  renderSignals();
}

function renderSignals() {
  const container = document.getElementById('signalContainer');
  const badge = document.getElementById('signalBadge');
  if (!container) return;
  if (allSignals.length === 0) {
    container.innerHTML = '<div class="empty-state">暂无交易信号</div>';
    badge.textContent = '等待信号...';
    return;
  }
  badge.textContent = `共 ${allSignals.length} 条`;
  let html = '';
  for (const s of allSignals) {
    const sev = s.severity || 'info';
    const time = new Date(s.time * 1000).toLocaleTimeString('zh-CN', {hour12: false});
    html += `<div class="signal-item severity-${sev}">
      <span class="signal-time">${time}</span>
      <span class="signal-prod-tag">${s.product}</span>
      <div class="signal-body">
        <div class="signal-title">${s.title}</div>
        <div class="signal-detail">${s.detail}</div>
      </div>
      <span class="signal-badge ${sev}">${sev === 'critical' ? '严重' : sev === 'warning' ? '警告' : '提示'}</span>
    </div>`;
  }
  container.innerHTML = html;
}

function showSignalToast(signal) {
  const existing = document.querySelector('.signal-toast');
  if (existing) existing.remove();
  const toast = document.createElement('div');
  toast.className = 'signal-toast';
  const color = signal.severity === 'critical' ? '#e74c3c' : signal.severity === 'warning' ? '#f39c12' : '#3498db';
  toast.innerHTML = `
    <div class="toast-title" style="color:${color}">${signal.title} — ${signal.product_name}</div>
    <div class="toast-detail">${signal.detail}</div>
  `;
  document.body.appendChild(toast);
  setTimeout(() => { if (toast.parentNode) toast.remove(); }, 3000);
}
```

- [ ] **Step 3: 页面初始化时加载历史信号**

在 `connectWS();`（第693行）后、`loadHistory();`（第694行）前插入：

```javascript
loadSignals();
```

在 `loadHistory` 函数后（第550行后）、`// ─── K-line Chart ─────────────────`（第552行）前插入 `loadSignals` 函数：

```javascript
// ─── Load Signals on mount ───────
async function loadSignals() {
  try {
    const r = await fetch('/api/signals?limit=100');
    const signals = await r.json();
    if (signals && signals.length > 0) {
      allSignals = signals;
      renderSignals();
    }
  } catch(e) {
    console.warn('Load signals error:', e);
  }
}
```

---

### Task 6: 验证测试

**Files:**
- Test: 启动服务器验证

- [ ] **Step 1: 启动服务器并验证**

```bash
cd ~/futures-game && uvicorn main:app --reload
```

- [ ] **Step 2: 验证 API 端点**

```bash
# 检查 MA120 是否返回
curl http://127.0.0.1:8000/api/kline?pid=AU\&interval=1h\&limit=50 | python3 -c "import sys,json; d=json.load(sys.stdin); print('MA keys:', list(d['mas'].keys())); print('MA120 count:', len(d['mas']['MA120']))"

# 检查 signals API
curl http://127.0.0.1:8000/api/signals | python3 -c "import sys,json; d=json.load(sys.stdin); print('Signals count:', len(d))"

# 检查初始 risk_rate
curl http://127.0.0.1:8000/api/account | python3 -c "import sys,json; d=json.load(sys.stdin); print('risk_rate:', d['risk_rate'])"
```

- [ ] **Step 3: 浏览器验证**
- 打开 `http://127.0.0.1:8000`
- 确认 K 线图例显示 MA120 而非 MA240
- 等待约 10 秒，确认信号面板出现信号条目
- 确认信号到达时有右上角 toast 弹出
- 刷新页面，确认历史信号仍显示

---

## 自检清单

- [x] MA240→MA120 在后端 API、前端图例、JS 变量三处全部对齐
- [x] 信号检测 6 种类型覆盖（金叉/死叉/聚合/发散三阶段）
- [x] 状态跟踪防重复触发（`signal_state` per product）
- [x] 去重：信号 ID 唯一，前端 allSignals.some() 检查
- [x] REST 历史查询 `/api/signals`
- [x] 浮动 toast 通知（3s 自动消失）
- [x] 页面刷新后加载历史信号
- [x] reset_account 清空信号数据
- [x] 前端信号上限 100 条、后端 200 条
- [x] 无成交量信号（用户要求移除）
