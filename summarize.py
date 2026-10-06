#!/usr/bin/env python3
"""ボイスログの要約: raw をCodexで清書・要約し、/ob/ボイスログ/YYYY-MM-DD.md に書く。

使い方:
  summarize.py                 終わった日（朝4時区切り）のうち、未要約 or 録音が増えた日を処理
  summarize.py --date 2026-10-07   指定日を強制的に（作り直して）処理
"""
import os, re, sys, subprocess, tempfile, argparse
from datetime import datetime, date
from urllib.parse import unquote
import xml.etree.ElementTree as ET

from voicelog import s, url, mkcol, log, log_date, JST, RAW_DIR

SUMMARY_DIR = "/ob/ボイスログ"
HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT_FILE = os.path.join(HERE, "prompt.md")
GLOSSARY_FILE = os.path.join(HERE, "glossary.txt")
WORKDIR = os.path.join(HERE, "codex-work")   # Codexを起動する空フォルダ
CODEX_TIMEOUT = 600


def list_raw_days():
    body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
            '<d:resourcetype/></d:prop></d:propfind>')
    r = s.request("PROPFIND", url(RAW_DIR + "/"), data=body, headers={"Depth": "1"})
    if r.status_code == 404:
        return []
    r.raise_for_status()
    days = []
    for resp in ET.fromstring(r.content).findall("{DAV:}response"):
        name = unquote(resp.find("{DAV:}href").text).rstrip("/").split("/")[-1]
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.md", name)
        if m:
            days.append(date.fromisoformat(m.group(1)))
    return sorted(days)


def get_text(path):
    r = s.get(url(path))
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content.decode("utf-8")


def count_recordings(raw):
    return len(re.findall(r"<!-- .+? -->", raw))


def summarized_count(summary):
    if not summary:
        return None
    m = re.search(r"^recordings:\s*(\d+)", summary, re.M)
    return int(m.group(1)) if m else None


def run_codex(day, raw):
    with open(PROMPT_FILE, encoding="utf-8") as f:
        prompt = f.read()
    glossary = ""
    if os.path.exists(GLOSSARY_FILE):
        with open(GLOSSARY_FILE, encoding="utf-8") as f:
            glossary = f.read().strip()
    prompt = (prompt.replace("{{DATE}}", day.isoformat())
                    .replace("{{GLOSSARY}}", glossary)
                    .replace("{{RAW}}", raw))
    os.makedirs(WORKDIR, exist_ok=True)
    with tempfile.NamedTemporaryFile("r", suffix=".md", delete=False) as out:
        out_path = out.name
    try:
        cmd = ["codex", "exec",
               "--sandbox", "read-only",
               "--skip-git-repo-check",
               "--output-last-message", out_path,
               "-"]
        p = subprocess.run(cmd, input=prompt, text=True, cwd=WORKDIR,
                           capture_output=True, timeout=CODEX_TIMEOUT)
        if p.returncode != 0:
            raise RuntimeError(f"codex終了コード {p.returncode}: {p.stderr[-500:]}")
        with open(out_path, encoding="utf-8") as f:
            result = f.read().strip()
        if not result:
            raise RuntimeError("codexの出力が空")
        return result
    finally:
        os.remove(out_path)


def summarize(day, force=False):
    raw = get_text(f"{RAW_DIR}/{day.isoformat()}.md")
    if raw is None:
        log(f"SKIP {day}: rawなし")
        return
    n = count_recordings(raw)
    path = f"{SUMMARY_DIR}/{day.isoformat()}.md"
    if not force and summarized_count(get_text(path)) == n:
        return  # 要約済みで録音も増えていない
    body = run_codex(day, raw)
    body = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", body)  # 囲みが付いたら外す
    header = (f"---\ndate: {day.isoformat()}\ntags: [voicelog]\n"
              f"recordings: {n}\n"
              f"generated: {datetime.now(JST):%Y-%m-%dT%H:%M}\n---\n\n")
    footer = f"\n\n---\n文字起こし原文: [[ボイスログ/raw/{day.isoformat()}]]\n"
    mkcol(SUMMARY_DIR)
    s.put(url(path), data=(header + body + footer).encode("utf-8")).raise_for_status()
    log(f"SUMMARY OK {day}（録音{n}件）")


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
        except Exception as e:
            log(f"SUMMARY NG {day}: {e}")


if __name__ == "__main__":
    main()
