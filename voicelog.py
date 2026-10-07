#!/usr/bin/env python3
"""iPhoneショートカット経由の音声（Nextcloud /VoiceLog/Inbox）を処理する。cron で10分おき。

  Inbox の音声 → Whisper → raw に追記 → 音声を /VoiceLog/audio/YYYY-MM/ へ移動 → Codex で仕分け
"""
import os
import re
import tempfile
from datetime import datetime, timezone

from nc import s, url, mkcol, log, list_dir, append_raw, JST
import classify as cl

INBOX = "/VoiceLog/Inbox"
AUDIO_DIR = "/VoiceLog/audio"
MIN_AGE_SEC = 120       # 届いてからこの秒数が経ったものだけ処理（アップロード途中を拾わない）
AUDIO_EXT = (".m4a", ".mp3", ".wav", ".aac", ".caf", ".ogg")


def recorded_at(name):
    m = re.match(r"(\d{4}-\d{2}-\d{2}_\d{6})", name)
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d_%H%M%S").replace(tzinfo=JST)
    return datetime.now(JST)


def archive(name, dt):
    dest_dir = f"{AUDIO_DIR}/{dt:%Y-%m}"
    mkcol(dest_dir)
    r = s.request("MOVE", url(f"{INBOX}/{name}"),
                  headers={"Destination": url(f"{dest_dir}/{name}"), "Overwrite": "F"})
    r.raise_for_status()


def main():
    now = datetime.now(timezone.utc)
    items = [(n, mt) for n, mt, is_dir in list_dir(INBOX)
             if not is_dir and n.lower().endswith(AUDIO_EXT)]
    targets = sorted(n for n, mt in items
                     if mt is None or (now - mt).total_seconds() >= MIN_AGE_SEC)
    if not targets:
        if items:
            log(f"待機中: {len(items)}件（届いてから{MIN_AGE_SEC}秒未満）")
        return
    from transcribe import load_model, transcribe_file
    model = load_model()
    for name in targets:
        try:
            dt = recorded_at(name)
            r = s.get(url(f"{INBOX}/{name}"))
            r.raise_for_status()
            if not r.content:
                log(f"NG {name}: 0バイトのファイル")
                continue
            with tempfile.NamedTemporaryFile(suffix=os.path.splitext(name)[1]) as tmp:
                tmp.write(r.content)
                tmp.flush()
                text, duration = transcribe_file(model, tmp.name)
            day, new = append_raw(dt, name, text or "（音声なし）", "voice")
            archive(name, dt)
            log(f"OK {name}（{duration:.0f}秒）")
        except Exception as e:  # noqa: BLE001
            log(f"NG {name}: {e}")
            continue
        if new and text:
            try:
                result = cl.classify(text)
                cl.record(result, dt, day)
                log(f"  → {result['type']}: {result['title']}")
            except Exception as e:  # noqa: BLE001
                log(f"  → 仕分け失敗（rawには記録済み）: {e}")


if __name__ == "__main__":
    main()
