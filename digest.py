"""タスクのノートから、期日が近いものを機械的に拾う（Codex は使わない）。

短期タスク.md・長期タスク.md・開発タスク.md（と以前の タスク.md）、Concierge の 預かりタスク.md（📧 付き）を見る。
拾う条件は2つだけ:
  - 未完了の行（`- [ ]` で始まる。完了は `- [x]`）
  - 期日 `📅 YYYY-MM-DD` が付いている
bot が書く行はこの形に決まっているし、Obsidian で手で書き足したタスク（Tasks プラグインと同じ書き方）も拾える。
"""
import re
from datetime import date, timedelta

from classify import TYPE_FILES, TASK_TYPES, LEGACY_TASK_FILES, WEEKDAYS
from nc import get_text

HELD_TASKS_MD = "/ob/Concierge/預かりタスク.md"   # Concierge（別 bot）がメールから預かったタスク
TASK_RE = re.compile(r"^\s*- \[ \] (.*)$")
DUE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")


def clean(body):
    """表示用に、期日・元記録へのリンク・目印を取り除く。"""
    body = DUE_RE.sub("", body)
    body = re.sub(r"（\[\[[^\]]*\]\]）", "", body)
    body = re.sub(r"%%[^%]*%%", "", body)
    body = re.sub(r"（📧 ([^：）]*)：[^）]*）", r"（📧 \1）", body)
    return re.sub(r"\s+", " ", body).strip()


def open_tasks_with_due(content):
    out = []
    for line in (content or "").splitlines():
        m = TASK_RE.match(line)
        if not m:
            continue
        d = DUE_RE.search(m.group(1))
        if not d:
            continue
        try:
            due = date.fromisoformat(d.group(1))
        except ValueError:
            continue
        out.append((due, clean(m.group(1))))
    return sorted(out)


def build_digest(today, content=None, include_empty=False):
    if content is None:
        # 自分のタスク（Recall）と、Concierge がメールから預かったタスクの両方
        paths = [TYPE_FILES[t] for t in TASK_TYPES] + LEGACY_TASK_FILES + [HELD_TASKS_MD]
        content = "\n".join(get_text(p) or "" for p in paths)
    tasks = open_tasks_with_due(content)
    overdue = [(d, t) for d, t in tasks if d < today]
    due_today = [t for d, t in tasks if d == today]
    tomorrow = [t for d, t in tasks if d == today + timedelta(days=1)]
    if not (overdue or due_today or tomorrow) and not include_empty:
        return None
    lines = [f"📅 **今日のタスク**（{today:%m/%d}・{WEEKDAYS[today.weekday()]}）"]
    lines += [f"- {t}" for t in due_today] or ["- なし"]
    if overdue:
        lines.append("⚠️ **期日を過ぎたもの**")
        lines += [f"- {t}（{d:%m/%d}）" for d, t in overdue]
    if tomorrow:
        lines.append("🔜 **明日が期日**")
        lines += [f"- {t}" for t in tomorrow]
    return "\n".join(lines)
