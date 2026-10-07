"""Codex CLI を非対話で1回呼ぶ。読み取り専用・空の作業フォルダで動かし、返事のテキストだけ受け取る。
Codex には資格情報もファイルも渡さない。書き込みは呼び出し側（Python）が行う。

モデルは用途で使い分ける（env で変更可）:
  light（仕分け・分類など、件数が多く簡単なもの） CODEX_MODEL_LIGHT / CODEX_EFFORT_LIGHT
  main （日記・ブリーフなど、1日1回で質が大事なもの） CODEX_MODEL_MAIN / CODEX_EFFORT_MAIN
未設定なら Codex の既定のモデル。使ったトークン数は state/codex_usage.jsonl に1行ずつ残す。
"""
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(HERE, "codex-work")
USAGE_LOG = os.path.join(HERE, "state", "codex_usage.jsonl")
JST = timezone(timedelta(hours=9))
PROFILES = {
    "light": (os.environ.get("CODEX_MODEL_LIGHT", ""), os.environ.get("CODEX_EFFORT_LIGHT", "")),
    "main": (os.environ.get("CODEX_MODEL_MAIN", ""), os.environ.get("CODEX_EFFORT_MAIN", "")),
}


def _log_usage(task, model, prompt_chars, stderr, seconds, ok):
    m = re.search(r"tokens used[^\d]*([\d,]+)", stderr or "", re.I)
    tokens = int(m.group(1).replace(",", "")) if m else None
    try:
        os.makedirs(os.path.dirname(USAGE_LOG), exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": datetime.now(JST).isoformat(timespec="seconds"), "task": task,
                                "model": model or "default", "prompt_chars": prompt_chars,
                                "tokens": tokens, "seconds": round(seconds, 1), "ok": ok},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


def run_codex(prompt, timeout=600, profile="main", task=""):
    model, effort = PROFILES.get(profile, ("", ""))
    os.makedirs(WORKDIR, exist_ok=True)
    fd, out_path = tempfile.mkstemp(suffix=".md")
    os.close(fd)
    cmd = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
           "--output-last-message", out_path]
    if model:
        cmd += ["-m", model]
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    cmd.append("-")
    started = datetime.now()
    stderr, ok = "", False
    try:
        p = subprocess.run(cmd, input=prompt, text=True, cwd=WORKDIR,
                           capture_output=True, timeout=timeout)
        stderr = (p.stderr or "") + (p.stdout or "")
        if p.returncode != 0:
            raise RuntimeError(f"codex終了コード {p.returncode}: {p.stderr[-500:]}")
        with open(out_path, encoding="utf-8") as f:
            result = f.read().strip()
        if not result:
            raise RuntimeError("codexの出力が空")
        ok = True
        return result
    finally:
        _log_usage(task or profile, model, len(prompt), stderr, (datetime.now() - started).total_seconds(), ok)
        os.remove(out_path)


def load_prompt(name):
    with open(os.path.join(HERE, "prompts", name), encoding="utf-8") as f:
        return f.read()


def glossary():
    try:
        return load_prompt("glossary.txt").strip()
    except FileNotFoundError:
        return ""
