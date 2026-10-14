#!/usr/bin/env python3
"""raw にある記録を、今の分類でやり直す（種類のノートの古い行を消して、新しく書き直す）。

使い方（pk-206 で ~/recall/run_reclassify.sh 経由）:
  run_reclassify.sh 2026-10-07              その日の記録の一覧（ID と最初の1行）
  run_reclassify.sh 2026-10-07 <ID>         その1件をやり直す
  run_reclassify.sh 2026-10-07 --all        その日の全件をやり直す
Discord の返信は書き換わらない（結果はここに表示される）。
"""
import re
import sys
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

import classify as cl
from nc import RAW_DIR, JST, DAY_START_HOUR, get_text

DONE_RE = re.compile(r"^- \[x\] (.+?)(?: 📅| #| （\[\[|$)")

ENTRY_RE = re.compile(r"^## (\d{2}):(\d{2})[^\n]*\n<!-- (.+?) -->\n(.*?)(?=\n## |\Z)", re.S | re.M)


def entries(day):
    raw = get_text(f"{RAW_DIR}/{day.isoformat()}.md") or ""
    out = []
    for m in ENTRY_RE.finditer(raw):
        h, mi = int(m.group(1)), int(m.group(2))
        d = day + timedelta(days=1) if h < DAY_START_HOUR else day
        dt = datetime(d.year, d.month, d.day, h, mi, tzinfo=JST)
        out.append((m.group(3), dt, m.group(4).strip()))
    return out


def done_titles(entry_id):
    """消す前に、その記録から出た行のうち完了（- [x]）にしてあるものの見出しを集める。"""
    token = cl.entry_token(entry_id)
    paths = list(cl.TYPE_FILES.values()) + cl.LEGACY_TASK_FILES + cl.LEGACY_WISH_FILES
    titles = []
    for p in paths:
        for line in (get_text(p) or "").splitlines():
            m = DONE_RE.match(line)
            if token in line and m:
                titles.append(m.group(1).strip())
    return titles


def carry_done(items, old_done):
    """新しい項目のうち、以前完了にしていたものと見出しが近いものを完了のまま引き継ぐ。"""
    left = list(old_done)
    for it in items:
        if it["type"] not in cl.CHECK_TYPES or not left:
            continue
        best = max(left, key=lambda t: SequenceMatcher(None, t, it["title"]).ratio())
        if SequenceMatcher(None, best, it["title"]).ratio() >= 0.5:
            it["done"] = True
            left.remove(best)
    return left


def redo(day, entry_id, dt, text):
    old_done = done_titles(entry_id)
    removed = cl.unrecord(entry_id)
    if text in ("（音声なし）", "（内容なし）"):
        print(f"- {entry_id}: 中身なし（古い行 {removed} 行を消しただけ）")
        return
    items = cl.classify(text, dt)
    lost = carry_done(items, old_done)
    cl.record(items, dt, day, entry_id)
    print(f"■ {dt:%m/%d %H:%M} {entry_id}（古い行 {removed} 行 → 新しく {sum(i['type'] != 'その他' for i in items)} 行）")
    print(cl.describe(items))
    for t in lost:
        print(f"  ⚠️ 完了済みだった「{t}」は引き継ぎ先なし（タスク以外になったか、見出しが変わった）")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    day = date.fromisoformat(sys.argv[1])
    es = entries(day)
    if len(sys.argv) == 2:
        for eid, dt, text in es:
            print(f"{dt:%H:%M}  {eid}  {text.splitlines()[0][:40] if text else ''}")
        return
    target = sys.argv[2]
    picked = es if target == "--all" else [e for e in es if e[0] == target]
    if not picked:
        sys.exit(f"{day} に {target} は見つかりません")
    for eid, dt, text in picked:
        redo(day, eid, dt, text)


if __name__ == "__main__":
    main()
