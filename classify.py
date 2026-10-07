"""1件のメモを Codex で仕分けして、種類ごとのノートに1行追記する。

種類は4つ固定（タスク / 心を動かされたもの / やりたいこと / その他）。タグはAIが自由に付ける。
1週間ほど運用してタグを見直す前提。元データは raw にあるので、後から全件やり直せる。
"""
import json
import re
from datetime import date

from ai import run_codex, load_prompt, glossary
from nc import OB_ROOT, append_line, remove_lines_containing

TYPES = ["タスク", "心を動かされたもの", "やりたいこと", "その他"]
TYPE_FILES = {t: f"{OB_ROOT}/{t}.md" for t in TYPES if t != "その他"}
TYPE_ICON = {"タスク": "✅", "心を動かされたもの": "✨", "やりたいこと": "🌱", "その他": "📓"}
WEEKDAYS = "月火水木金土日"


def now_label(dt):
    """Codex に渡す「いつ投げたか」。相対的な日付（明日・来週）を解決する基準になる。"""
    return f"{dt:%Y-%m-%d}（{WEEKDAYS[dt.weekday()]}）{dt:%H:%M}"


def parse_due(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def parse_result(out, text):
    """Codex の返事から JSON を取り出して整える。壊れていても落とさず「その他」にする。"""
    data = {}
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    typ = data.get("type") if data.get("type") in TYPES else "その他"
    first_line = text.strip().splitlines()[0] if text.strip() else "（無題）"
    title = str(data.get("title") or first_line)[:40].replace("\n", " ").strip()
    summary = str(data.get("summary") or "").replace("\n", " ").strip()
    tags = []
    raw_tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    for t in raw_tags:
        t = re.sub(r"\s+", "_", str(t).strip().lstrip("#"))
        t = re.sub(r"[#\[\]()]", "", t)
        if t and not t.isdigit() and t not in tags:
            tags.append(t)
    return {"type": typ, "title": title, "summary": summary, "tags": tags[:5],
            "due": parse_due(data.get("due"))}


def classify(text, dt):
    prompt = (load_prompt("classify.md")
              .replace("{{NOW}}", now_label(dt))
              .replace("{{GLOSSARY}}", glossary())
              .replace("{{TEXT}}", text))
    return parse_result(run_codex(prompt, timeout=300, profile="light", task="recall_classify"), text)


def entry_token(entry_id):
    # Obsidian のコメント記法。表示されないが、削除のときにこの行を見つける目印になる
    return f"%%{entry_id}%%"


def format_line(result, dt, day, entry_id):
    link = f"[[Recall/raw/{day.isoformat()}|{dt:%m/%d %H:%M}]]"
    tags = " ".join("#" + t for t in result["tags"])
    if result["type"] == "タスク":
        body = f"- [ ] {result['title']}"
        if result.get("due"):
            # Obsidian の Tasks プラグインの期日の書き方（プラグインが無くてもただの文字として読める）
            body += f" 📅 {result['due']}"
    else:
        body = f"- **{result['title']}**"
        if result["summary"]:
            body += f" — {result['summary']}"
    return " ".join(x for x in [body, tags, f"（{link}）", entry_token(entry_id)] if x)


def record(result, dt, day, entry_id):
    """種類のノートに追記する。「その他」は raw と日記だけに残す。"""
    typ = result["type"]
    if typ == "その他":
        return
    append_line(TYPE_FILES[typ], f"# {typ}\n\n", format_line(result, dt, day, entry_id))


def unrecord(entry_id):
    """種類のノートから、その記録の行を消す。"""
    return sum(remove_lines_containing(path, entry_token(entry_id)) for path in TYPE_FILES.values())


def describe(result):
    tags = " ".join("#" + t for t in result["tags"])
    due = ""
    if result.get("due"):
        d = date.fromisoformat(result["due"])
        due = f" 📅 {d:%m/%d}（{WEEKDAYS[d.weekday()]}）"
    return f"{TYPE_ICON[result['type']]} **{result['type']}**：{result['title']}{due} {tags}".strip()
