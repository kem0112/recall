"""Codex CLI を非対話で1回呼ぶ。読み取り専用・空の作業フォルダで動かし、返事のテキストだけ受け取る。
Codex には資格情報もファイルも渡さない。書き込みは呼び出し側（Python）が行う。"""
import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(HERE, "codex-work")


def run_codex(prompt, timeout=600):
    os.makedirs(WORKDIR, exist_ok=True)
    fd, out_path = tempfile.mkstemp(suffix=".md")
    os.close(fd)
    try:
        cmd = ["codex", "exec",
               "--sandbox", "read-only",
               "--skip-git-repo-check",
               "--output-last-message", out_path,
               "-"]
        p = subprocess.run(cmd, input=prompt, text=True, cwd=WORKDIR,
                           capture_output=True, timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError(f"codex終了コード {p.returncode}: {p.stderr[-500:]}")
        with open(out_path, encoding="utf-8") as f:
            result = f.read().strip()
        if not result:
            raise RuntimeError("codexの出力が空")
        return result
    finally:
        os.remove(out_path)


def load_prompt(name):
    with open(os.path.join(HERE, "prompts", name), encoding="utf-8") as f:
        return f.read()


def glossary():
    try:
        return load_prompt("glossary.txt").strip()
    except FileNotFoundError:
        return ""
