"""モーニングブリーフ: pk-206 にある材料を集めて、Codex に1通の読み物としてまとめさせる。

材料（第1段階）:
  - タスク.md の「今日・期限切れ・明日」（digest.py と同じ機械的な読み取り）
  - 京都の今日の天気（Open-Meteo。APIキー不要）
  - 昨日の日記の「要約」
  - やりたいこと.md からランダムに1つ
Codex が失敗したら、材料をそのまま並べた版を返す（届かない日を作らない）。
"""
import random
import re
from datetime import date, timedelta

import requests

from ai import run_codex, load_prompt
from classify import TYPE_FILES, WEEKDAYS
from digest import open_tasks_with_due, build_digest
from nc import JOURNAL_DIR, get_text, log

# 京都大学（吉田キャンパス）のあたり
LAT, LON = 35.03, 135.78
WMO = {0: "快晴", 1: "晴れ", 2: "晴れ時々くもり", 3: "くもり", 45: "霧", 48: "霧",
       51: "弱い霧雨", 53: "霧雨", 55: "強い霧雨", 61: "小雨", 63: "雨", 65: "強い雨",
       66: "着氷性の雨", 67: "強い着氷性の雨", 71: "小雪", 73: "雪", 75: "大雪", 77: "霧雪",
       80: "にわか雨", 81: "強いにわか雨", 82: "激しいにわか雨", 85: "にわか雪", 86: "強いにわか雪",
       95: "雷雨", 96: "雹を伴う雷雨", 99: "激しい雹を伴う雷雨"}


def weather_text(today):
    r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
        "latitude": LAT, "longitude": LON, "timezone": "Asia/Tokyo",
        "start_date": today.isoformat(), "end_date": today.isoformat(),
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "hourly": "precipitation_probability",
    })
    r.raise_for_status()
    j = r.json()
    d = j["daily"]
    hours = j["hourly"]["time"]
    probs = j["hourly"]["precipitation_probability"]

    def block(h0, h1):
        vals = [p for t, p in zip(hours, probs) if p is not None and h0 <= int(t[11:13]) < h1]
        return max(vals) if vals else None

    return (f"京都（大学付近）: {WMO.get(d['weather_code'][0], '不明')}、"
            f"最高{d['temperature_2m_max'][0]:.0f}℃ / 最低{d['temperature_2m_min'][0]:.0f}℃、"
            f"降水確率 朝(6-12時){block(6, 12)}% 昼(12-18時){block(12, 18)}% 夜(18-24時){block(18, 24)}%")


def yesterday_summary(today):
    text = get_text(f"{JOURNAL_DIR}/{(today - timedelta(days=1)).isoformat()}.md")
    if not text:
        return None
    m = re.search(r"## 要約\s*\n(.*?)(?=\n## |\Z)", text, re.S)
    return (m.group(1) if m else text[:600]).strip()


def random_wish():
    text = get_text(TYPE_FILES["やりたいこと"]) or ""
    items = [re.sub(r"\s*（\[\[.*$", "", ln[2:]).strip()
             for ln in text.splitlines() if ln.startswith("- ")]
    return random.choice(items) if items else None


def gather(today):
    def safe(fn, *a):
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001
            log(f"NG brief material {fn.__name__}: {e!r}")
            return None

    tasks_md = safe(get_text, TYPE_FILES["タスク"]) or ""
    tasks = open_tasks_with_due(tasks_md)
    return {
        "today": f"{today:%Y-%m-%d}（{WEEKDAYS[today.weekday()]}）",
        "weather": safe(weather_text, today) or "（取得できず）",
        "overdue": [f"{t}（期日 {d:%m/%d}）" for d, t in tasks if d < today],
        "due_today": [t for d, t in tasks if d == today],
        "tomorrow": [t for d, t in tasks if d == today + timedelta(days=1)],
        "this_week": [f"{t}（{d:%m/%d}）" for d, t in tasks if today + timedelta(days=1) < d <= today + timedelta(days=7)],
        "yesterday": safe(yesterday_summary, today) or "（昨日の日記はまだありません）",
        "wish": safe(random_wish) or "（まだありません）",
        "tasks_md": tasks_md,
    }


def materials_text(m):
    def lst(xs):
        return "\n".join(f"- {x}" for x in xs) if xs else "- なし"
    return (f"## 日付\n{m['today']}\n\n## 天気\n{m['weather']}\n\n"
            f"## 期日を過ぎたタスク\n{lst(m['overdue'])}\n\n## 今日が期日のタスク\n{lst(m['due_today'])}\n\n"
            f"## 明日が期日のタスク\n{lst(m['tomorrow'])}\n\n## 今週中（2〜7日後）が期日のタスク\n{lst(m['this_week'])}\n\n"
            f"## 昨日の日記の要約\n{m['yesterday']}\n\n## やりたいことリストから1つ\n- {m['wish']}\n")


def build_brief(today):
    m = gather(today)
    try:
        prompt = load_prompt("brief.md").replace("{{MATERIALS}}", materials_text(m))
        text = run_codex(prompt, timeout=300).strip()
        text = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", text)
        if text:
            return text
    except Exception as e:  # noqa: BLE001
        log(f"NG brief codex: {e!r}")
    # Codex が使えない日も、材料だけは届ける
    fallback = build_digest(today, m["tasks_md"], include_empty=True)
    return f"☀️ **おはようございます**（AIのまとめは失敗したので材料だけ）\n🌤 {m['weather']}\n\n{fallback}"
