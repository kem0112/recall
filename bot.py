#!/usr/bin/env python3
"""Recall の常駐 bot。2つの入口を受け持ち、結果を #recall に返す。

  ① #recall への投稿（文字・音声メッセージ）: 投稿された瞬間に処理
  ② iPhoneショートカットの音声（Nextcloud /VoiceLog/Inbox）: 30秒おきに見に行って処理

  （音声なら Whisper で文字起こし）→ raw に追記 → Codex で仕分け → 種類のノートへ → #recall に結果

照明・鍵の bot（home-control の bot.py）とは別アプリ・別プロセス。こちらが落ちても家の制御には影響しない。
"""
import asyncio
import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone

import discord
from discord.ext import tasks

import classify as cl
from nc import JST, AUDIO_DIR, s as nc_session, url, append_raw, put_bytes, list_dir, mkcol, log
from voicelog import INBOX, AUDIO_EXT as INBOX_AUDIO_EXT, recorded_at, archive

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ["RECALL_DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["RECALL_CHANNEL_ID"])
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("RECALL_ALLOWED_USER_IDS", "").split(",") if x.strip()
}
if not ALLOWED_USER_IDS:
    # 空だと誰の投稿でも記録してしまう。設定漏れで起動させない
    sys.exit("RECALL_ALLOWED_USER_IDS が未設定です")

AUDIO_EXT = (".ogg", ".oga", ".opus", ".m4a", ".mp3", ".wav", ".aac", ".caf", ".mp4", ".webm")
TRANSCRIBE_TIMEOUT = 1800
REPLY_LIMIT = 1900  # Discord の上限 2000 字に余裕を持たせる
INBOX_POLL_SEC = 30
# Nextcloud は書き込み途中を .part にして完了後に名前を変えるので、見えた時点で揃っている。念のため少しだけ待つ
INBOX_MIN_AGE_SEC = 15
FAILED_DIR = "/VoiceLog/failed"   # 文字起こしできなかった音声（同じ失敗を30秒ごとに繰り返さないため退避）

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Whisper と Codex は重いので1件ずつ順番に処理する
_work_lock = asyncio.Lock()


def is_audio(att):
    ct = att.content_type or ""
    return ct.startswith("audio/") or att.filename.lower().endswith(AUDIO_EXT)


def audio_archive_path(message, att):
    """ショートカット経由と同じ並び（録音時刻のファイル名）で /VoiceLog/audio/YYYY-MM/ に置く。"""
    dt = message.created_at.astimezone(JST)
    suffix = os.path.splitext(att.filename)[1].lower() or ".ogg"
    return f"{AUDIO_DIR}/{dt:%Y-%m}/{dt:%Y-%m-%d_%H%M%S}_discord{message.id}_{att.id}{suffix}"


async def transcribe_bytes(data, suffix):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audio" + suffix)
        with open(path, "wb") as f:
            f.write(data)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, os.path.join(HERE, "transcribe.py"), path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=TRANSCRIBE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("文字起こしがタイムアウトしました")
        if proc.returncode != 0:
            raise RuntimeError("文字起こし失敗: " + err.decode(errors="replace")[-300:])
        return out.decode(errors="replace").strip()


async def safe_react(message, emoji, remove=False):
    try:
        if remove:
            await message.remove_reaction(emoji, client.user)
        else:
            await message.add_reaction(emoji)
    except discord.HTTPException:
        pass


def clip(text, limit):
    return text if len(text) <= limit else text[:limit] + "…"


async def handle(message):
    parts, voice, others = [], False, []
    if message.content.strip():
        parts.append(message.content.strip())
    for att in message.attachments:
        if is_audio(att):
            data = await att.read()
            # 元の音声を先に保存する（文字起こしに失敗しても音声は残る。後でやり直せる）
            try:
                await asyncio.to_thread(put_bytes, audio_archive_path(message, att), data)
            except Exception as e:  # noqa: BLE001
                log(f"NG audio archive discord:{message.id}: {e!r}")
            suffix = os.path.splitext(att.filename)[1] or ".ogg"
            parts.append(await transcribe_bytes(data, suffix) or "（音声なし）")
            voice = True
        else:
            others.append(att.filename)
    if others:
        # 画像などは今は中身を読まない。何を投げたかだけ残す
        parts.append("（添付: " + ", ".join(others) + "）")
    text = "\n".join(parts).strip()
    if not text:
        return None

    dt = message.created_at.astimezone(JST)
    source = "discord-voice" if voice else "discord"
    day, new = await asyncio.to_thread(append_raw, dt, f"discord:{message.id}", text, source)
    if not new:
        return "（記録済みのメッセージです）"

    lines = []
    if voice:
        lines.append("🎙️ " + clip(text, 1200))
    try:
        result = await asyncio.to_thread(cl.classify, text)
        await asyncio.to_thread(cl.record, result, dt, day)
        lines.append(cl.describe(result))
    except Exception as e:  # noqa: BLE001
        # 仕分けに失敗しても raw には残っている。日記には入る
        log(f"NG classify discord:{message.id}: {e!r}")
        lines.append(f"📓 記録はしました（仕分けは失敗: `{clip(str(e), 200)}`）")
    return clip("\n".join(lines), REPLY_LIMIT)


# ---- ② ショートカット経由（Inbox）----

def _download(path):
    r = nc_session.get(url(path))
    r.raise_for_status()
    return r.content


def _move_to_failed(name):
    mkcol(FAILED_DIR)
    r = nc_session.request("MOVE", url(f"{INBOX}/{name}"),
                           headers={"Destination": url(f"{FAILED_DIR}/{name}"), "Overwrite": "T"})
    r.raise_for_status()


async def _send(text):
    channel = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
    await channel.send(clip(text, REPLY_LIMIT))


async def process_inbox_file(name):
    dt = recorded_at(name)
    head = f"🎙️ ショートカット（{dt:%m/%d %H:%M}）"
    data = await asyncio.to_thread(_download, f"{INBOX}/{name}")
    if not data:
        await asyncio.to_thread(_move_to_failed, name)
        await _send(f"⚠️ {head}: 0バイトのファイルでした（VoiceLog/failed へ移動）")
        return
    try:
        text = await transcribe_bytes(data, os.path.splitext(name)[1] or ".m4a")
    except Exception as e:  # noqa: BLE001
        await asyncio.to_thread(_move_to_failed, name)
        await _send(f"⚠️ {head}: 文字起こしに失敗（VoiceLog/failed へ移動）`{clip(str(e), 200)}`")
        return
    day, new = await asyncio.to_thread(append_raw, dt, name, text or "（音声なし）", "voice")
    await asyncio.to_thread(archive, name, dt)
    log(f"OK inbox {name}")
    lines = [head, clip(text or "（音声なし）", 1200)]
    if new and text:
        try:
            result = await asyncio.to_thread(cl.classify, text)
            await asyncio.to_thread(cl.record, result, dt, day)
            lines.append(cl.describe(result))
        except Exception as e:  # noqa: BLE001
            log(f"NG classify {name}: {e!r}")
            lines.append(f"📓 記録はしました（仕分けは失敗: `{clip(str(e), 200)}`）")
    await _send("\n".join(lines))


@tasks.loop(seconds=INBOX_POLL_SEC)
async def poll_inbox():
    try:
        items = await asyncio.to_thread(list_dir, INBOX)
    except Exception as e:  # noqa: BLE001
        log(f"NG inbox list: {e!r}")
        return
    now = datetime.now(timezone.utc)
    targets = sorted(
        n for n, mt, is_dir in items
        if not is_dir and n.lower().endswith(INBOX_AUDIO_EXT)
        and (mt is None or (now - mt).total_seconds() >= INBOX_MIN_AGE_SEC))
    for name in targets:
        async with _work_lock:
            try:
                await process_inbox_file(name)
            except Exception as e:  # noqa: BLE001
                # 通信エラーなどは Inbox に残したまま、次の回にやり直す
                log(f"NG inbox {name}: {e!r}\n{traceback.format_exc()}")


@poll_inbox.before_loop
async def before_poll_inbox():
    await client.wait_until_ready()


@client.event
async def on_ready():
    log(f"Recall bot ready as {client.user} (channel={CHANNEL_ID})")
    if not poll_inbox.is_running():
        poll_inbox.start()


@client.event
async def on_message(message):
    if message.author.bot or message.channel.id != CHANNEL_ID:
        return
    if message.author.id not in ALLOWED_USER_IDS:
        return
    await safe_react(message, "⏳")
    try:
        async with _work_lock:
            reply = await handle(message)
        if reply:
            await message.reply(reply, mention_author=False)
        await safe_react(message, "✅")
    except Exception as e:  # noqa: BLE001
        log(f"NG discord:{message.id}: {e!r}\n{traceback.format_exc()}")
        await safe_react(message, "⚠️")
        try:
            await message.reply(f"⚠️ 記録に失敗しました: `{clip(str(e), 300)}`", mention_author=False)
        except discord.HTTPException:
            pass
    finally:
        await safe_react(message, "⏳", remove=True)


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
