#!/usr/bin/env python3
"""日次まとめ: その日の raw を Codex で清書・要約し、/ob/Recall/日記/YYYY-MM-DD.md に書く。

使い方:
  summarize.py                     終わった日（朝4時区切り）のうち、未要約 or 記録が増えた日を処理
  summarize.py --date 2026-10-07   指定日を強制的に（作り直して）処理
"""
import argparse
import re
from datetime import datetime, date

from ai import run_codex, load_prompt, glossary
from nc import RAW_DIR, JOURNAL_DIR, JST, get_text, put_text, list_dir, log, log_date


def list_raw_days():
    days = []
    for name, _, is_dir in list_dir(RAW_DIR):
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.md", name)
        if m and not is_dir:
            days.append(date.fromisoformat(m.group(1)))
    return sorted(days)


def count_entries(raw):
    return len(re.findall(r"<!-- .+? -->", raw))


def summarized_count(summary):
    if not summary:
        return None
    m = re.search(r"^entries:\s*(\d+)", summary, re.M)
    return int(m.group(1)) if m else None


def summarize(day, force=False):
    raw = get_text(f"{RAW_DIR}/{day.isoformat()}.md")
    if raw is None:
        log(f"SKIP {day}: rawなし")
        return
    n = count_entries(raw)
    path = f"{JOURNAL_DIR}/{day.isoformat()}.md"
    if not force and summarized_count(get_text(path)) == n:
        return  # 要約済みで記録も増えていない
    prompt = (load_prompt("summary.md")
              .replace("{{DATE}}", day.isoformat())
              .replace("{{GLOSSARY}}", glossary())
              .replace("{{RAW}}", raw))
    body = run_codex(prompt, profile="main", task="recall_journal")
    body = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", body)  # 囲みが付いたら外す
    header = (f"---\ndate: {day.isoformat()}\ntags: [recall, 日記]\n"
              f"entries: {n}\n"
              f"generated: {datetime.now(JST):%Y-%m-%dT%H:%M}\n---\n\n")
    footer = f"\n\n---\n原文: [[Recall/raw/{day.isoformat()}]]\n"
    put_text(path, header + body + footer)
    log(f"SUMMARY OK {day}（{n}件）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD を強制処理")
    a = ap.parse_args()
    if a.date:
        summarize(date.fromisoformat(a.date), force=True)
        return
    today = log_date(datetime.now(JST))
    for day in list_raw_days():
        if day >= today:
            continue  # まだ終わっていない日は対象外
        try:
            summarize(day)
        except Exception as e:  # noqa: BLE001
            log(f"SUMMARY NG {day}: {e}")


if __name__ == "__main__":
    main()
