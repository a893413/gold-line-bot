"""
金價 LINE 通知機器人

資料來源：XAUS 國際黃金現貨價（免費公開 API，https://xaus.com/api）
  - 即時：/api/v1/spot?currency=TWD&unit=gram   國際金價 × 匯率，換算成台幣每公克
  - 歷史：/api/v1/history?days=370              每日收盤（美元／盎司）
  - 盤中：/api/v1/intraday?hours=24             每 5 分鐘一筆（美元／盎司）

（原本用台銀牌價，但台銀網站有防機器人檢查，會擋雲端伺服器，所以改用這個。）

用法（GitHub Actions 會自動執行，平常不用手動跑）：
  python bot/main.py update          抓價格、更新資料、畫圖、準備訊息
  python bot/main.py send IMAGE_URL  把準備好的訊息傳到 LINE
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
CHART_PATH = ROOT / "docs" / "chart.png"
OUT_DIR = ROOT / "out"
CONFIG_PATH = ROOT / "config.json"

API = "https://xaus.com/api/v1"
TZ = ZoneInfo("Asia/Taipei")
GRAMS_PER_QIAN = 3.75          # 一錢 = 3.75 公克
GRAMS_PER_OZ = 31.1034768      # 一金衡盎司 = 31.1034768 公克
WEEKDAY = "一二三四五六日"
UP_COLOR = "#D32F2F"    # 台灣習慣：漲 = 紅
DOWN_COLOR = "#2E7D32"  # 跌 = 綠
GOLD = "#B8860B"

HEADERS = {"User-Agent": "gold-line-bot (personal, family gold price notifier)"}


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


# ---------- 換算 ----------

def qian(g):
    """每公克 → 每錢（四捨五入，和網頁算法一致）"""
    return int(g * GRAMS_PER_QIAN + 0.5)


def usd_oz_to_twd_g(usd_oz, fx):
    return usd_oz * fx / GRAMS_PER_OZ


# ---------- 抓資料 ----------

def get_json(path, **params):
    r = requests.get(f"{API}/{path}", params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_spot():
    """回傳 dict：twd_g（台幣每公克）、usd_oz、fx（1 美元換多少台幣）、狀態"""
    j = get_json("spot", currency="TWD", unit="gram", compact=1)
    usd_oz = float(j["spot_usd_oz"])
    fx = float(j["fx_rate"])
    computed = usd_oz_to_twd_g(usd_oz, fx)
    x = j.get("xau") or {}
    twd_g = computed
    if x.get("currency") == "TWD" and x.get("unit") in ("gram", "g"):
        p = float(x["price"])
        if abs(p - computed) / computed < 0.01:  # 兩種算法一致才用官方換算值
            twd_g = p
    state = j.get("data_state") or {}
    return {"twd_g": twd_g, "usd_oz": usd_oz, "fx": fx,
            "status": state.get("status", ""), "age": state.get("age_seconds"),
            "updated_at": j.get("updated_at", "")}


def fetch_history(fx):
    """每日收盤，用目前匯率換成台幣每公克：[[YYYY-MM-DD, 台幣每公克], ...]"""
    j = get_json("history", days=370)
    rows = []
    for p in j.get("points", []):
        if p.get("d") and p.get("c"):
            rows.append([p["d"], round(usd_oz_to_twd_g(float(p["c"]), fx), 2)])
    return rows


def fetch_intraday(fx, today):
    """今天（台灣時間）每 5 分鐘的價格：[[HH:MM, 台幣每公克], ...]"""
    j = get_json("intraday", symbol="xau", hours=24)
    rows = []
    for p in j.get("points", []):
        t = datetime.fromtimestamp(int(p["t"]), timezone.utc).astimezone(TZ)
        if t.strftime("%Y-%m-%d") == today:
            rows.append([t.strftime("%H:%M"), round(usd_oz_to_twd_g(float(p["p"]), fx), 2)])
    rows.sort(key=lambda r: r[0])
    return rows


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
    """rows: [[YYYY-MM-DD, 台幣每公克], ...]，畫成每錢走勢"""
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
    span = (max(vals) - min(vals)) or 100
    ax.fill_between(x, vals, min(vals) - span * 0.15, color=GOLD, alpha=0.10)
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
    pad = span * 0.18 or 200
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


def row_kv(label, value, value_color="#333333"):
    return {
        "type": "box", "layout": "baseline", "spacing": "md",
        "contents": [
            {"type": "text", "text": label, "size": "md", "color": "#888888",
             "flex": 5},
            {"type": "text", "text": value, "size": "lg", "color": value_color,
             "align": "end", "flex": 6},
        ],
    }


def build_message(info, cfg, test=False):
    now_q = qian(info["twd_g"])
    when = info["when"]
    title = ("【測試】" if test else "") + "黃金參考價"
    subtitle = f"{when.month}/{when.day}（{WEEKDAY[when.weekday()]}）" \
               f"{when.strftime('%H:%M')} 國際金價即時換算"

    body = []
    target = int(cfg.get("target_per_qian") or 0)
    if target and now_q >= target:
        body.append({
            "type": "box", "layout": "vertical", "backgroundColor": "#FDECEC",
            "cornerRadius": "md", "paddingAll": "md",
            "contents": [{"type": "text", "wrap": True, "weight": "bold",
                          "size": "lg", "color": UP_COLOR,
                          "text": f"已經到你設定的每錢 {target:,} 元了！"}],
        })
    body += [
        {"type": "text", "text": "現在國際金價，每一錢", "size": "lg",
         "color": "#555555", "margin": "md"},
        {"type": "text", "text": money(now_q), "size": "4xl",
         "weight": "bold", "color": "#222222"},
    ]
    alt_change = ""
    if info.get("prev_g"):
        prev_q = qian(info["prev_g"])
        diff = now_q - prev_q
        pct = diff / prev_q * 100
        if diff > 0:
            txt, color = f"▲ 比前一天收盤漲 {diff:,} 元（+{pct:.1f}%）", UP_COLOR
        elif diff < 0:
            txt, color = f"▼ 比前一天收盤跌 {-diff:,} 元（-{-pct:.1f}%）", DOWN_COLOR
        else:
            txt, color = "和前一天收盤一樣", "#555555"
        body.append({"type": "text", "text": txt, "size": "lg", "color": color,
                     "weight": "bold", "wrap": True})
        alt_change = f"（{'漲' if diff > 0 else '跌' if diff < 0 else '平'}{abs(diff):,}）"
    rows = [row_kv("每一公克", f"{info['twd_g']:,.0f} 元")]
    if info.get("day_high"):
        rows += [row_kv("今天最高（每錢）", money(qian(info["day_high"]))),
                 row_kv("今天最低（每錢）", money(qian(info["day_low"])))]
    rows += [row_kv("國際金價", f"{info['usd_oz']:,.2f} 美元/盎司", "#888888"),
             row_kv("匯率", f"1 美元 = {info['fx']:.3f} 元", "#888888")]
    body.append({"type": "separator", "margin": "lg"})
    body.append({"type": "box", "layout": "vertical", "margin": "lg",
                 "spacing": "sm", "contents": rows})

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
                        "text": "上圖是近 30 天每錢走勢。這是國際金價換算的中間價，"
                                "拿去銀樓或銀行賣，收購價通常會再低一些，"
                                "實際以店家報價為準。"},
                   ]},
    }
    if cfg.get("page_url"):
        bubble["hero"]["action"] = {"type": "uri", "uri": cfg["page_url"]}
        bubble["footer"]["contents"].insert(0, {
            "type": "button", "style": "primary", "color": GOLD, "height": "md",
            "action": {"type": "uri", "label": "看詳細走勢",
                       "uri": cfg["page_url"]}})

    alt = f"{title}：國際金價每錢約 {now_q:,} 元{alt_change}"
    return {"type": "flex", "altText": alt[:400], "contents": bubble}


# ---------- 主要流程 ----------

def cmd_update(test=False):
    cfg = load_config()
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    try:
        spot = fetch_spot()
    except Exception as e:  # noqa: BLE001
        print("抓不到國際金價：", e)
        sys.exit(1)  # 讓 GitHub 寄失敗通知 email 給你
    if spot["status"] == "unavailable":
        print("資料來源暫時沒有價格（unavailable）")
        sys.exit(1)

    daily_doc = load_json(DATA_DIR / "daily.json", {"rows": []})
    daily = {r[0]: r for r in daily_doc["rows"]}
    if len(daily) < 150:  # 第一次執行先補一年歷史（用目前匯率換算）
        try:
            for r in fetch_history(spot["fx"]):
                daily.setdefault(r[0], r)
        except Exception as e:  # noqa: BLE001
            print("歷史資料抓取失敗（不影響今天的通知）：", e)

    # 前一天收盤：今天以前最後一筆
    prev = next((r for r in sorted(daily.values(), key=lambda r: r[0], reverse=True)
                 if r[0] < today), None)

    intraday = []
    try:
        intraday = fetch_intraday(spot["fx"], today)
    except Exception as e:  # noqa: BLE001
        print("盤中資料抓取失敗（不影響今天的通知）：", e)
    now_row = [now.strftime("%H:%M"), round(spot["twd_g"], 2)]
    if not intraday or intraday[-1][0] < now_row[0]:
        intraday.append(now_row)

    daily[today] = [today, round(spot["twd_g"], 2)]
    rows = sorted(daily.values(), key=lambda r: r[0])
    save_json(DATA_DIR / "daily.json",
              {"updated": now.strftime("%Y-%m-%d %H:%M"),
               "source": "XAUS 國際金價 × 匯率換算", "rows": rows})
    save_json(DATA_DIR / "today.json", {"date": today, "rows": intraday})

    info = {
        "when": now, "twd_g": spot["twd_g"], "usd_oz": spot["usd_oz"],
        "fx": spot["fx"], "prev_g": prev[1] if prev else None,
        "day_high": max(p[1] for p in intraday),
        "day_low": min(p[1] for p in intraday),
    }
    render_chart(rows[-int(cfg.get("chart_days", 30)):], CHART_PATH)
    msg = build_message(info, cfg, test=test)

    OUT_DIR.mkdir(exist_ok=True)
    save_json(OUT_DIR / "message.json", msg)
    save_json(OUT_DIR / "status.json", {"send": True, "reason": ""})
    print(f"國際金價 {spot['usd_oz']:,.2f} USD/oz × 匯率 {spot['fx']:.4f} "
          f"= {spot['twd_g']:,.1f} 元/公克，每錢 {qian(spot['twd_g']):,} 元；"
          f"資料狀態 {spot['status']}（{spot['age']} 秒前）")


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
