"""
一次性設定：在 LINE 聊天室下方加一個固定按鈕「點擊此處查看金價走勢」
（用 LINE Messaging API 建立 rich menu，點下去打開走勢圖網頁）

在 GitHub Actions 的「設定聊天室按鈕」手動執行一次即可，之後不用再跑。
"""
import os
import sys
from pathlib import Path

import requests

TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
REPO = os.environ.get("GITHUB_REPOSITORY", "")
NAME = "gold-menu"
IMG = Path(__file__).resolve().parent / "richmenu.png"

if not TOKEN:
    sys.exit("缺少 LINE_CHANNEL_ACCESS_TOKEN")
owner, repo = REPO.split("/", 1)
page_url = f"https://{owner.lower()}.github.io/{repo}/"
H = {"Authorization": f"Bearer {TOKEN}"}


def check(r, what):
    if r.status_code != 200:
        sys.exit(f"{what} 失敗：{r.status_code} {r.text}")
    print(f"{what} 成功")
    return r


# 1. 刪掉之前用這個程式建立的按鈕（重跑時不會重複）
r = check(requests.get("https://api.line.me/v2/bot/richmenu/list", headers=H, timeout=30), "讀取現有按鈕")
for m in r.json().get("richmenus", []):
    if m.get("name") == NAME:
        check(requests.delete(f"https://api.line.me/v2/bot/richmenu/{m['richMenuId']}",
                              headers=H, timeout=30), "刪除舊按鈕")

# 2. 建立按鈕：整塊都是同一個連結
menu = {
    "size": {"width": 2500, "height": 843},
    "selected": True,                 # 預設展開
    "name": NAME,
    "chatBarText": "點此看金價走勢",   # 聊天室最下方那條的文字（最多 14 字）
    "areas": [{
        "bounds": {"x": 0, "y": 0, "width": 2500, "height": 843},
        "action": {"type": "uri", "label": "查看金價走勢", "uri": page_url},
    }],
}
r = check(requests.post("https://api.line.me/v2/bot/richmenu", headers=H, json=menu, timeout=30), "建立按鈕")
mid = r.json()["richMenuId"]

# 3. 上傳按鈕圖片
check(requests.post(f"https://api-data.line.me/v2/bot/richmenu/{mid}/content",
                    headers={**H, "Content-Type": "image/png"},
                    data=IMG.read_bytes(), timeout=60), "上傳按鈕圖片")

# 4. 設為所有好友的預設按鈕
check(requests.post(f"https://api.line.me/v2/bot/user/all/richmenu/{mid}",
                    headers=H, timeout=30), "設為預設按鈕")
print("完成！按鈕連到：", page_url)
