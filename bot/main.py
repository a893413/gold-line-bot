"""
金價 LINE 通知機器人

資料來源：臺灣銀行「黃金存摺牌價」（公開資料）
  - 每日收盤：https://rate.bot.com.tw/gold/csv/YYYY-MM/TWD/0
  - 當天盤中：https://rate.bot.com.tw/gold/chart/YYYY-MM-DD/TWD/

用法（GitHub Actions 會自動執行，平常不用手動跑）：
  python bot/main.py update          抓價格、更新資料、畫圖、準備訊息
  python bot/main.py send IMAGE_URL  把準備好的訊息傳到 LINE
"""

import html
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
CHART_PATH = ROOT / "docs" / "chart.png"
OUT_DIR = ROOT / "out"
CONFIG_PATH = ROOT / "config.json"

TZ = ZoneInfo("Asia/Taipei")
GRAMS_PER_QIAN = 3.75  # 一錢 = 3.75 公克
WEEKDAY = "一二三四五六日"
UP_COLOR = "#D32F2F"    # 台灣習慣：漲 = 紅
DOWN_COLOR = "#2E7D32"  # 跌 = 綠
GOLD = "#B8860B"

DEBUG = []  # 抓不到資料時記錄台銀回了什麼，方便除錯

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9",
}


# ---------- 設定 ----------

def load_config():
    cfg = {"target_per_qian": 0, "chart_days": 30, "page_url": ""}
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    if not cfg.get("page_url"):
        repo = os.environ.get("GITHUB_REPOSITORY", "")  # 例如 someone/gold-line-bot
        if "/" in repo:
            owner, name = repo.split("/", 1)
            cfg["page_url"] = f"https://{owner.lower()}.github.io/{name}/"
    return cfg


# ---------- 抓台銀資料 ----------

SESSION = None


def http_get(url):
    """先開台銀首頁拿 cookie，再帶 Referer 抓資料（模擬一般瀏覽器）"""
    global SESSION
    if SESSION is None:
        SESSION = requests.Session()
        SESSION.headers.update(HEADERS)
        try:
            r0 = SESSION.get("https://rate.bot.com.tw/gold?Lang=zh-TW", timeout=30)
            DEBUG.append(f"首頁 → HTTP {r0.status_code}, {len(r0.content)} bytes, "
                         f"cookies={list(SESSION.cookies.keys())}")
        except Exception as e:  # noqa: BLE001
            DEBUG.append(f"首頁連線失敗：{e}")
    return SESSION.get(url, timeout=30,
                       headers={"Referer": "https://rate.bot.com.tw/gold?Lang=zh-TW"})


def page_text(raw):
    t = html.unescape(re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t)).strip()


def to_num(s):
    return int(float(s.replace(",", "")) + 0.5)


def qian(g):
    """每公克 → 每錢（四捨五入，和網頁算法一致）"""
    return int(g * GRAMS_PER_QIAN + 0.5)


def fetch_month(year, month):
    """抓某個月每天的收盤牌價，回傳 [[YYYY-MM-DD, 買入, 賣出], ...]"""
    url = f"https://rate.bot.com.tw/gold/csv/{year:04d}-{month:02d}/TWD/0"
    r = http_get(url)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    rows = []
    for line in text.splitlines()[1:]:
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 5 or not re.fullmatch(r"\d{8}", cols[0]):
            continue
        d = f"{cols[0][:4]}-{cols[0][4:6]}-{cols[0][6:]}"
        rows.append([d, to_num(cols[3]), to_num(cols[4])])
    if not rows:
        DEBUG.append(f"{url} → HTTP {r.status_code}, {r.headers.get('content-type')}, "
                     f"{len(r.content)} bytes，內容：{page_text(text)[:300]!r}")
    return rows


def fetch_intraday(day):
    """抓某一天盤中每次掛牌，回傳 [[HH:MM, 買入, 賣出], ...]（依時間排序）"""
    url = f"https://rate.bot.com.tw/gold/chart/{day}/TWD/"
    r = http_get(url)
    r.raise_for_status()
    text = html.unescape(re.sub(r"<[^>]+>", " ", r.text))
    text = re.sub(r"\s+", " ", text)
    pattern = (r"(\d{2}:\d{2})\s+新台幣\s*\(TWD\)\s+1\s*公克\s+"
               r"([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)")
    rows = [[t, to_num(b), to_num(s)] for t, b, s in re.findall(pattern, text)]
    rows.sort(key=lambda x: x[0])
    if not rows:
        DEBUG.append(f"{url} → HTTP {r.status_code}, {len(r.content)} bytes, "
                     f"開頭：{text[:150]!r}")
    return rows


def months_back(today, n):
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")


# ---------- 畫 LINE 訊息裡的圖 ----------

def render_chart(rows, path):
    """rows: [[YYYY-MM-DD, 買入每公克, 賣出], ...]，畫成「每錢回收參考價」走勢"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    labels = [r[0][5:].replace("-", "/") for r in rows]
    vals = [qian(r[1]) for r in rows]
    x = list(range(len(vals)))

    fig, ax = plt.subplots(figsize=(6.5, 4.225), dpi=160)  # 1040 x 676
    fig.patch.set_facecolor("white")
    ax.plot(x, vals, color=GOLD, linewidth=3, solid_capstyle="round")
    ax.fill_between(x, vals, min(vals) - (max(vals) - min(vals) or 100) * 0.15,
                    color=GOLD, alpha=0.10)
    ax.scatter([x[-1]], [vals[-1]], s=90, color=GOLD, zorder=5,
               edgecolors="white", linewidths=2)
    ax.annotate(f"{vals[-1]:,}", (x[-1], vals[-1]), textcoords="offset points",
                xytext=(-14, -26), ha="right", va="top", fontsize=17,
                fontweight="bold", color="#333333",
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=GOLD, lw=1))
    hi = max(range(len(vals)), key=lambda i: vals[i])
    lo = min(range(len(vals)), key=lambda i: vals[i])
    for i, dy, va in ((hi, 10, "bottom"), (lo, -12, "top")):
        if x[-1] - i >= max(3, len(x) // 6):  # 太靠近最新一點就不標，避免字疊在一起
            ax.annotate(f"{vals[i]:,}", (i, vals[i]), textcoords="offset points",
                        xytext=(0, dy), ha="center", va=va, fontsize=11,
                        color="#777777")

    step = max(1, len(x) // 6)
    ticks = list(range(0, len(x), step))
    if ticks[-1] != x[-1]:
        if x[-1] - ticks[-1] < step * 0.7:
            ticks.pop()
        ticks.append(x[-1])
    ax.set_xticks(ticks)
    ax.set_xticklabels([labels[i] for i in ticks], fontsize=11, color="#777777")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax.tick_params(axis="y", labelsize=11, colors="#777777")
    pad = (max(vals) - min(vals)) * 0.18 or 200
    ax.set_ylim(min(vals) - pad, max(vals) + pad)
    ax.grid(axis="y", color="#e5e5e5", linewidth=1)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color("#cccccc")
    fig.tight_layout(pad=1.2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor="white")
    plt.close(fig)


# ---------- 組 LINE 訊息 ----------

def money(v):
    return f"{v:,} 元"


def row_kv(label, value, value_color="#333333", bold=False):
    return {
        "type": "box", "layout": "baseline", "spacing": "md",
        "contents": [
            {"type": "text", "text": label, "size": "md", "color": "#888888",
             "flex": 4},
            {"type": "text", "text": value, "size": "lg", "color": value_color,
             "align": "end", "flex": 6, "weight": "bold" if bold else "regular"},
        ],
    }


def build_message(info, cfg, test=False):
    q = qian
    buy_q = q(info["buy"])
    sell_q = q(info["sell"])
    when = info["when"]
    title = ("【測試】" if test else "") + "黃金參考價"
    subtitle = f"{when.month}/{when.day}（{WEEKDAY[when.weekday()]}）" \
               f"{info['time']} 台銀公開牌價"

    body = []
    target = int(cfg.get("target_per_qian") or 0)
    if target and buy_q >= target:
        body.append({
            "type": "box", "layout": "vertical", "backgroundColor": "#FDECEC",
            "cornerRadius": "md", "paddingAll": "md", "margin": "none",
            "contents": [{"type": "text", "wrap": True, "weight": "bold",
                          "size": "lg", "color": UP_COLOR,
                          "text": f"已經到你設定的每錢 {target:,} 元了！"}],
        })
    body += [
        {"type": "text", "text": "現在拿去賣，每一錢大約", "size": "lg",
         "color": "#555555", "margin": "md"},
        {"type": "text", "text": money(buy_q), "size": "4xl",
         "weight": "bold", "color": "#222222"},
    ]
    alt_change = ""
    if info.get("prev_buy"):
        diff = buy_q - q(info["prev_buy"])
        pct = diff / q(info["prev_buy"]) * 100
        if diff > 0:
            txt, color = f"▲ 比前一天收盤漲 {diff:,} 元（+{pct:.1f}%）", UP_COLOR
        elif diff < 0:
            txt, color = f"▼ 比前一天收盤跌 {-diff:,} 元（-{-pct:.1f}%）", DOWN_COLOR
        else:
            txt, color = "和前一天收盤一樣", "#555555"
        body.append({"type": "text", "text": txt, "size": "lg", "color": color,
                     "weight": "bold", "wrap": True})
        alt_change = f"（{'漲' if diff > 0 else '跌' if diff < 0 else '平'}"\
                     f"{abs(diff):,}）"
    body.append({"type": "separator", "margin": "lg"})
    body.append({"type": "box", "layout": "vertical", "margin": "lg",
                 "spacing": "sm", "contents": [
                     row_kv("每一公克", money(info["buy"])),
                     row_kv("今天最高（每錢）", money(q(info["day_high"]))),
                     row_kv("今天最低（每錢）", money(q(info["day_low"]))),
                     row_kv("買金價（每錢）", money(sell_q), "#888888"),
                 ]})

    bubble = {
        "type": "bubble", "size": "giga",
        "header": {"type": "box", "layout": "vertical", "paddingBottom": "sm",
                   "contents": [
                       {"type": "text", "text": title, "size": "xl",
                        "weight": "bold", "color": GOLD},
                       {"type": "text", "text": subtitle, "size": "sm",
                        "color": "#888888"},
                   ]},
        "hero": {"type": "image", "url": "IMAGE_URL", "size": "full",
                 "aspectRatio": "20:13", "aspectMode": "fit"},
        "body": {"type": "box", "layout": "vertical", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm",
                   "contents": [
                       {"type": "text", "wrap": True, "size": "sm",
                        "color": "#999999",
                        "text": "上面是近 30 天每錢回收參考價。這是台銀公開價格，"
                                "拿金飾去賣的實際價格以店家報價為準。"},
                   ]},
    }
    if cfg.get("page_url"):
        bubble["hero"]["action"] = {"type": "uri", "uri": cfg["page_url"]}
        bubble["footer"]["contents"].insert(0, {
            "type": "button", "style": "primary", "color": GOLD, "height": "md",
            "action": {"type": "uri", "label": "看詳細走勢",
                       "uri": cfg["page_url"]}})

    alt = f"{title}：現在賣每錢約 {buy_q:,} 元{alt_change}"
    return {"type": "flex", "altText": alt[:400], "contents": bubble}


# ---------- 主要流程 ----------

def cmd_update(test=False):
    cfg = load_config()
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    daily_doc = load_json(DATA_DIR / "daily.json", {"rows": []})
    daily = {r[0]: r for r in daily_doc["rows"]}
    n_months = 13 if len(daily) < 150 else 2  # 第一次執行先補一年歷史
    errors = []
    for y, m in months_back(now, n_months):
        try:
            for r in fetch_month(y, m):
                daily[r[0]] = r
        except Exception as e:  # noqa: BLE001
            errors.append(f"每日資料 {y}-{m:02d}：{e}")

    intraday = []
    try:
        intraday = fetch_intraday(today)
    except Exception as e:  # noqa: BLE001
        errors.append(f"盤中資料：{e}")

    if intraday:
        daily[today] = [today, intraday[-1][1], intraday[-1][2]]

    rows = sorted(daily.values(), key=lambda r: r[0])
    if not rows:
        print("抓不到任何台銀資料：", *errors, *DEBUG[:4], sep="\n  ")
        sys.exit(1)  # 讓 GitHub 寄失敗通知 email 給你

    has_today = rows[-1][0] == today
    if not intraday and has_today:
        intraday = [["收盤", rows[-1][1], rows[-1][2]]]

    save_json(DATA_DIR / "daily.json",
              {"updated": now.strftime("%Y-%m-%d %H:%M"), "rows": rows})
    save_json(DATA_DIR / "today.json",
              {"date": today if has_today else rows[-1][0],
               "rows": [r for r in intraday if r[0] != "收盤"]})

    latest = rows[-1]
    prev = next((r for r in reversed(rows) if r[0] < latest[0]), None)
    day_pts = intraday or [["", latest[1], latest[2]]]
    when = datetime.strptime(latest[0], "%Y-%m-%d")
    info = {
        "when": when,
        "time": (intraday[-1][0] if intraday and has_today
                 and intraday[-1][0] != "收盤" else "收盤"),
        "buy": latest[1], "sell": latest[2],
        "prev_buy": prev[1] if prev else None,
        "day_high": max(p[1] for p in day_pts),
        "day_low": min(p[1] for p in day_pts),
    }

    render_chart(rows[-int(cfg.get("chart_days", 30)):], CHART_PATH)
    msg = build_message(info, cfg, test=test)

    send = has_today or test
    reason = "" if send else "今天台銀沒有掛牌（休假日），不傳訊息"
    if errors:
        print("部分資料抓取失敗：", *errors, sep="\n  ")
    OUT_DIR.mkdir(exist_ok=True)
    save_json(OUT_DIR / "message.json", msg)
    save_json(OUT_DIR / "status.json", {"send": send, "reason": reason})
    print(f"最新：{latest[0]} {info['time']} 買入 {latest[1]}/g，"
          f"每錢約 {qian(latest[1]):,}；send={send} {reason}")


def cmd_send(image_url):
    status = load_json(OUT_DIR / "status.json", {"send": False})
    if not status.get("send"):
        print("不傳訊息：", status.get("reason", ""))
        return
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token:
        print("缺少 LINE_CHANNEL_ACCESS_TOKEN（請到 GitHub Secrets 設定）")
        sys.exit(1)
    msg = json.loads((OUT_DIR / "message.json").read_text(encoding="utf-8"))
    msg["contents"]["hero"]["url"] = image_url

    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    to = [t.strip() for t in os.environ.get("LINE_TO", "").split(",") if t.strip()]
    if to:  # 指定傳給某幾個人
        url = "https://api.line.me/v2/bot/message/multicast"
        payload = {"to": to, "messages": [msg]}
    else:   # 傳給所有加這個帳號好友的人（建議只有媽媽加）
        url = "https://api.line.me/v2/bot/message/broadcast"
        payload = {"messages": [msg]}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code != 200:
        print("LINE 傳送失敗：", r.status_code, r.text)
        if r.status_code == 429:
            print("可能是這個月的免費訊息則數用完了。")
        sys.exit(1)
    print("LINE 傳送成功")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "update":
        cmd_update(test=os.environ.get("TEST_MODE", "").lower() == "true")
    elif len(sys.argv) >= 3 and sys.argv[1] == "send":
        cmd_send(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)
