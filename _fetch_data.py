"""下载螺纹钢RB0真实数据 (新浪财经)"""
import urllib.request, json, csv, os

url = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var/InnerFuturesNewService.getDailyKLine?symbol=RB0"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

with urllib.request.urlopen(req, timeout=15) as r:
    raw = r.read().decode("gbk")

start = raw.index("[")
end = raw.rindex("]") + 1
data = json.loads(raw[start:end])

print(f"数据条数: {len(data)}")
print(f"第一条: {data[0]}")
print(f"最后一条: {data[-1]}")

# 写入CSV
csv_path = os.path.join(os.path.dirname(__file__), "rb_daily.csv")
with open(csv_path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["date","open","high","low","close","volume","open_interest"])
    for item in data:
        w.writerow([
            item["d"],           # date
            item["o"],           # open
            item["h"],           # high
            item["l"],           # low
            item["c"],           # close
            item["v"],           # volume
            item.get("p", 0),    # open_interest
        ])

print(f"\n已写入 {len(data)} 行到 {csv_path}")
print("前3行:")
with open(csv_path) as f:
    for i, line in enumerate(f):
        if i >= 3: break
        print(line.strip())
