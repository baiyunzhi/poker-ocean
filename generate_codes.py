#!/usr/bin/env python3
"""Generate 500 redeem codes for 扑克海洋 + validation logic for the game."""

import random
import csv

# Characters that are unambiguous (no O/0, I/1, etc.)
CHARS = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
PREFIX = 'PKR'

def checksum(code_body):
    """Simple checksum: sum of char values modulo 36, mapped to a digit/letter."""
    s = sum(ord(c) for c in code_body) % 36
    # 0-9 -> 0-9, 10-35 -> A-Z (without ambiguous chars)
    if s < 10:
        return str(s)
    # Map to our CHARS set (skip first 10 which are digits)
    idx = s - 10
    if idx >= len(CHARS) - 10:  # if beyond our char set, wrap
        idx = idx % (len(CHARS) - 10)
    return CHARS[10 + idx]

def generate_code(used):
    """Generate a unique code: PKR-XXXX-XXXXC"""
    while True:
        part1 = ''.join(random.choice(CHARS) for _ in range(4))
        part2 = ''.join(random.choice(CHARS) for _ in range(4))
        body = part1 + part2
        c = checksum(body)
        code = f"{PREFIX}-{part1}-{part2}{c}"
        if code not in used:
            return code

# Generate 500 unique codes
used = set()
codes = []
for _ in range(500):
    code = generate_code(used)
    used.add(code)
    codes.append(code)

# Write CSV for 闲鱼 (one code per row)
with open('redeem_codes.csv', 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    writer.writerow(['兑换码', '状态'])
    for code in codes:
        writer.writerow([code, '未使用'])

# Write TXT (plain list for 闲鱼 auto-delivery)
with open('redeem_codes.txt', 'w', encoding='utf-8') as f:
    for code in codes:
        f.write(code + '\n')

# Write validation JS snippet (embedded in the game)
js_snippet = f'''/* ==============================
   REDEEM CODE SYSTEM
   ============================== */
const REDEEM_CODES = {{
  _codes: new Set({[c for c in codes]}),

  _check(code) {{
    // Format: PKR-XXXX-XXXXC
    const m = code.match(/^PKR-([A-HJ-NP-Z2-9]{{4}})-([A-HJ-NP-Z2-9]{{4}}[A-HJ-NP-Z2-9])$/);
    if (!m) return false;
    const body = m[1] + m[2].slice(0, -1);
    const givenC = m[2].slice(-1);
    // Compute expected checksum
    const s = [...body].reduce((sum, c) => sum + c.charCodeAt(0), 0) % 36;
    const expectedC = s < 10 ? String(s) : '{CHARS[10:]}'[s - 10];
    return givenC === expectedC;
  }},

  redeem(code) {{
    const c = code.toUpperCase().trim();
    // Already used?
    const used = JSON.parse(localStorage.getItem('poker_redeemed') || '[]');
    if (used.includes(c)) return {{ok:false, msg:'此兑换码已使用过'}};
    // Validate format + checksum
    if (!this._check(c)) return {{ok:false, msg:'兑换码格式不正确'}};
    // Check against valid codes list
    if (!this._codes.has(c)) return {{ok:false, msg:'兑换码无效'}};
    // Mark used
    used.push(c);
    localStorage.setItem('poker_redeemed', JSON.stringify(used));
    return {{ok:true, msg:'复活成功！已恢复3条命'}};
  }},

  showDialog(callback) {{
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center;z-index:999';
    overlay.innerHTML = `
      <div style="background:#1a2838;border-radius:16px;padding:28px;max-width:360px;width:90%;border:1px solid rgba(240,185,11,0.2);text-align:center">
        <div style="font-size:2.5rem;margin-bottom:8px">\\u{{1F7E5}}</div>
        <h3 style="color:#f0b90b;margin-bottom:6px">花1元复活</h3>
        <p style="color:#8899aa;font-size:0.85rem;margin-bottom:16px;line-height:1.5">
          去闲鱼搜索 <strong>"扑克海洋复活币"</strong><br>
          花1元购买，获得复活码<br>
          输入下方即可继续游戏！
        </p>
        <input id="redeem-input" placeholder="输入复活码" style="width:100%;padding:12px;border-radius:8px;border:1px solid rgba(240,185,11,0.2);background:rgba(0,0,0,0.3);color:#e8eef4;font-size:1rem;text-align:center;box-sizing:border-box;outline:none">
        <div style="color:#6a8a9a;font-size:0.75rem;margin:6px 0">格式：PKR-XXXX-XXXXX</div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button id="redeem-cancel" style="flex:1;padding:10px;border-radius:8px;border:1px solid rgba(100,120,140,0.2);background:transparent;color:#8899aa;cursor:pointer;font-size:0.9rem">取消</button>
          <button id="redeem-confirm" style="flex:1;padding:10px;border-radius:8px;border:none;background:linear-gradient(135deg,#f0b90b,#d4a008);color:#0d1b2a;font-weight:700;cursor:pointer;font-size:0.9rem">验证复活</button>
        </div>
        <div id="redeem-msg" style="margin-top:10px;font-size:0.85rem;min-height:20px"></div>
      </div>`;
    document.body.appendChild(overlay);

    overlay.querySelector('#redeem-cancel').onclick = () => {{ overlay.remove(); if(callback) callback(false); }};
    overlay.querySelector('#redeem-confirm').onclick = () => {{
      const input = overlay.querySelector('#redeem-input');
      const msg = overlay.querySelector('#redeem-msg');
      const result = REDEEM_CODES.redeem(input.value);
      if (result.ok) {{
        msg.style.color = '#44cc44';
        msg.textContent = result.msg;
        setTimeout(() => {{ overlay.remove(); if(callback) callback(true); }}, 1200);
      }} else {{
        msg.style.color = '#ff4444';
        msg.textContent = result.msg;
      }}
    }};
    overlay.querySelector('#redeem-input').onkeydown = (e) => {{
      if (e.key === 'Enter') overlay.querySelector('#redeem-confirm').click();
    }};
    setTimeout(() => overlay.querySelector('#redeem-input').focus(), 100);
  }}
}};
'''

with open('redeem_js.txt', 'w', encoding='utf-8') as f:
    f.write(js_snippet)

print(f"✅ 生成了 {len(codes)} 个兑换码")
print(f"📄 CSV: redeem_codes.csv（闲鱼自动发货用）")
print(f"📄 TXT: redeem_codes.txt（纯文本列表）")
print(f"📄 JS:  redeem_js.txt（游戏校验逻辑，待嵌入）")
print(f"\n前5个兑换码示例：")
for c in codes[:5]:
    print(f"   {c}")
