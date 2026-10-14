"""1件の記録を Codex で項目に分け、種類ごとのノートに1行ずつ追記する。

長い音声メモには複数の話題（タスク・感想・気づき…）が混ざるので、1件から0〜数個の項目を取り出す。
種類は7つ固定（短期タスク / 開発タスク / アイデア / 心を動かされたもの / 人間関係 / 所感 / その他）。
タグは prompts/tags.txt の一覧からだけ選ぶ（一覧に無いタグは捨てる）。
元データは raw にあるので、後から全件やり直せる。1件から出た行には同じ目印（%%id%%）が付き、取り消しでまとめて消える。
"""
import json
import re
from datetime import date

from ai import run_codex, load_prompt, glossary
from nc import OB_ROOT, append_line, remove_lines_containing

TYPES = ["短期タスク", "開発タスク", "アイデア", "心を動かされたもの", "人間関係", "所感", "その他"]
TASK_TYPES = ("短期タスク", "開発タスク")              # 期日（📅）を付ける種類
CHECK_TYPES = ("短期タスク", "開発タスク", "アイデア")   # チェックボックス（- [ ]）で書く種類
TYPE_FILES = {t: f"{OB_ROOT}/{t}.md" for t in TYPES if t != "その他"}
# 以前の分類のノート。!tasks・ブリーフは引き続き読む（中の行はそのまま使える）
LEGACY_TASK_FILES = [f"{OB_ROOT}/タスク.md"]
LEGACY_WISH_FILES = [f"{OB_ROOT}/やりたいこと.md", f"{OB_ROOT}/長期タスク.md"]   # 長期タスク → アイデア（2026-10-14）
TYPE_ICON = {"短期タスク": "✅", "開発タスク": "🛠️", "アイデア": "💡", "心を動かされたもの": "✨",
             "人間関係": "🤝", "所感": "💭", "その他": "📓"}
MAX_TAGS = 3
WEEKDAYS = "月火水木金土日"
MAX_ITEMS = 8


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


def tag_list():
    """prompts/tags.txt の一覧（# 以降は説明、## は見出し）。ファイルが無ければ None（＝自由）。"""
    try:
        text = load_prompt("tags.txt")
    except FileNotFoundError:
        return None
    tags = []
    for line in text.splitlines():
        name = line.split("#", 1)[0].strip()
        if name and name not in tags:
            tags.append(name)
    return tags


def tag_prompt():
    try:
        lines = load_prompt("tags.txt").splitlines()
    except FileNotFoundError:
        return "（一覧なし。自由に付けてよい）"
    return "\n".join(l for l in lines if l.strip() and not l.startswith("# "))


def _clean_item(data, allowed=None):
    if not isinstance(data, dict):
        return None
    typ = data.get("type") if data.get("type") in TYPES else "その他"
    title = str(data.get("title") or "").replace("\n", " ").strip()[:40]
    if not title:
        return None
    summary = str(data.get("summary") or "").replace("\n", " ").strip()
    tags = []
    raw_tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    for t in raw_tags:
        t = re.sub(r"\s+", "_", str(t).strip().lstrip("#"))
        t = re.sub(r"[#\[\]()]", "", t)
        if t and not t.isdigit() and t not in tags and (allowed is None or t in allowed):
            tags.append(t)
    return {"type": typ, "title": title, "summary": summary, "tags": tags[:MAX_TAGS],
            "due": parse_due(data.get("due")) if typ in TASK_TYPES else None}


def parse_result(out, text):
    """Codex の返事から項目のリストを取り出して整える。壊れていても落とさず「その他」1件にする。"""
    data = None
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
        except ValueError:
            data = None
    raw_items = data.get("items") if isinstance(data, dict) else None
    if isinstance(data, dict) and raw_items is None and "type" in data:
        raw_items = [data]   # 1件だけの形で返ってきたとき
    allowed = tag_list()
    items = [it for it in (_clean_item(d, allowed) for d in (raw_items or [])) if it][:MAX_ITEMS]
    if not items:
        first_line = text.strip().splitlines()[0] if text.strip() else "（無題）"
        items = [{"type": "その他", "title": first_line[:40], "summary": "", "tags": [], "due": None}]
    return items


def classify(text, dt):
    prompt = (load_prompt("classify.md")
              .replace("{{NOW}}", now_label(dt))
              .replace("{{GLOSSARY}}", glossary())
              .replace("{{TAGS}}", tag_prompt())
              .replace("{{TEXT}}", text))
    return parse_result(run_codex(prompt, timeout=300, profile="light", task="recall_classify"), text)


def entry_token(entry_id):
    # Obsidian のコメント記法。表示されないが、削除のときにこの行を見つける目印になる
    return f"%%{entry_id}%%"


def format_line(result, dt, day, entry_id):
    link = f"[[Recall/raw/{day.isoformat()}|{dt:%m/%d %H:%M}]]"
    tags = " ".join("#" + t for t in result["tags"])
    if result["type"] in CHECK_TYPES:
        body = f"- [{'x' if result.get('done') else ' '}] {result['title']}"
        if result.get("due"):
            # Obsidian の Tasks プラグインの期日の書き方（プラグインが無くてもただの文字として読める）
            body += f" 📅 {result['due']}"
    else:
        body = f"- **{result['title']}**"
        if result["summary"]:
            body += f" — {result['summary']}"
    return " ".join(x for x in [body, tags, f"（{link}）", entry_token(entry_id)] if x)


def record(items, dt, day, entry_id):
    """項目ごとに種類のノートへ追記する。「その他」は raw と日記だけに残す。"""
    for it in items:
        if it["type"] == "その他":
            continue
        append_line(TYPE_FILES[it["type"]], f"# {it['type']}\n\n", format_line(it, dt, day, entry_id))


def unrecord(entry_id):
    """種類のノートから、その記録から出た行を全部消す（以前の分類のノートも含む）。"""
    paths = list(TYPE_FILES.values()) + LEGACY_TASK_FILES + LEGACY_WISH_FILES
    return sum(remove_lines_containing(p, entry_token(entry_id)) for p in paths)


def describe_one(it):
    tags = " ".join("#" + t for t in it["tags"])
    done = " （完了）" if it.get("done") else ""
    due = ""
    if it.get("due"):
        d = date.fromisoformat(it["due"])
        due = f" 📅 {d:%m/%d}（{WEEKDAYS[d.weekday()]}）"
    return f"{TYPE_ICON[it['type']]} **{it['type']}**：{it['title']}{due}{done} {tags}".strip()


def describe(items):
    return "\n".join(describe_one(it) for it in items)
