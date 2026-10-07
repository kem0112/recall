#!/usr/bin/env python3
"""Recall の常駐 bot。2つの入口を受け持ち、結果を #recall に返す。

  ① #recall への投稿（文字・音声メッセージ）: 投稿された瞬間に処理
  ② iPhoneショートカットの音声（Nextcloud /VoiceLog/Inbox）: 30秒おきに見に行って処理

  （音声なら Whisper で文字起こし）→ raw に追記 → Codex で仕分け → 種類のノートへ → #recall に結果

毎朝 8:00（RECALL_DIGEST_TIME）: モーニングブリーフ（天気・タスク・昨日のふりかえり等を Codex がまとめる）を送る。
  `!brief` でいつでも作り直し、`!tasks` で期日の近いタスクだけを出す（! で始まる投稿は記録しない）。

取り消し:
  - 自分の投稿を Discord で削除する → 記録からも消える
  - bot の返信（または自分の投稿）に 🗑️ でリアクションする → 記録から消える（ショートカットの分もこれで消せる）
  消えるもの: raw の該当部分・種類ノートの行・保存した音声（Nextcloud のゴミ箱へ）・bot の返信

照明・鍵の bot（home-control の bot.py）とは別アプリ・別プロセス。こちらが落ちても家の制御には影響しない。
"""
import asyncio
import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone, time as dtime

import discord
from discord.ext import tasks

import classify as cl
from digest import build_digest
from brief import build_brief
from nc import (JST, AUDIO_DIR, s as nc_session, url, append_raw, put_bytes, list_dir, mkcol,
                remove_raw_entry, delete_path, log, log_date)
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
TRASH_EMOJI = "🗑"
_h, _m = (os.environ.get("RECALL_DIGEST_TIME", "08:00").split(":") + ["0"])[:2]
DIGEST_TIME = dtime(hour=int(_h), minute=int(_m), tzinfo=JST)

# 記録と Discord のメッセージの対応（取り消しに使う）。pk-206 上だけに置く
STATE_PATH = os.path.join(HERE, "state", "entries.json")
STATE_KEEP = 3000

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Whisper と Codex は重いので1件ずつ順番に処理する。取り消しも同じ順番に並べる
_work_lock = asyncio.Lock()


# ---- 記録と Discord メッセージの対応表 ----

def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_state = _load_state()  # entry_id -> {"day": "YYYY-MM-DD", "bot_msg": int|None, "audio": [path]}


def _save_state():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    items = list(_state.items())[-STATE_KEEP:]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(items), f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def _remember(entry_id, day, bot_msg=None, audio=None):
    _state[entry_id] = {"day": day.isoformat(), "bot_msg": bot_msg, "audio": audio or []}
    _save_state()


def _entry_for_bot_msg(msg_id):
    for entry_id, info in _state.items():
        if info.get("bot_msg") == msg_id:
            return entry_id
    return None


# ---- 共通 ----

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


async def _channel():
    return client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)


async def _send(text):
    return await (await _channel()).send(clip(text, REPLY_LIMIT))


async def classify_and_record(text, dt, day, entry_id):
    try:
        result = await asyncio.to_thread(cl.classify, text, dt)
        await asyncio.to_thread(cl.record, result, dt, day, entry_id)
        return cl.describe(result)
    except Exception as e:  # noqa: BLE001
        # 仕分けに失敗しても raw には残っている。日記には入る
        log(f"NG classify {entry_id}: {e!r}")
        return f"📓 記録はしました（仕分けは失敗: `{clip(str(e), 200)}`）"


# ---- ① Discord への投稿 ----

async def handle(message):
    """戻り値 (返信テキスト or None, entry_id, day, 保存した音声のパス)"""
    entry_id = f"discord:{message.id}"
    parts, voice, others, audio_paths = [], False, [], []
    if message.content.strip():
        parts.append(message.content.strip())
    for att in message.attachments:
        if is_audio(att):
            data = await att.read()
            # 元の音声を先に保存する（文字起こしに失敗しても音声は残る。後でやり直せる）
            path = audio_archive_path(message, att)
            try:
                await asyncio.to_thread(put_bytes, path, data)
                audio_paths.append(path)
            except Exception as e:  # noqa: BLE001
                log(f"NG audio archive {entry_id}: {e!r}")
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
        return None, entry_id, None, audio_paths

    dt = message.created_at.astimezone(JST)
    source = "discord-voice" if voice else "discord"
    day, new = await asyncio.to_thread(append_raw, dt, entry_id, text, source)
    if not new:
        return "（記録済みのメッセージです）", entry_id, day, audio_paths

    lines = []
    if voice:
        lines.append("🎙️ " + clip(text, 1200))
    lines.append(await classify_and_record(text, dt, day, entry_id))
    return clip("\n".join(lines), REPLY_LIMIT), entry_id, day, audio_paths


async def handle_command(message):
    cmd = message.content.strip().split()[0].lower()
    if cmd == "!tasks":
        text = await asyncio.to_thread(
            build_digest, log_date(datetime.now(JST)), None, True)
        await message.reply(text, mention_author=False)
    elif cmd == "!brief":
        await safe_react(message, "⏳")
        async with _work_lock:
            text = await asyncio.to_thread(build_brief, log_date(datetime.now(JST)))
        await message.reply(clip(text, REPLY_LIMIT), mention_author=False)
        await safe_react(message, "⏳", remove=True)
    else:
        await message.reply("使えるコマンド: `!brief`（モーニングブリーフ）/ `!tasks`（今日・期限切れ・明日のタスク）",
                            mention_author=False)


@client.event
async def on_message(message):
    if message.author.bot or message.channel.id != CHANNEL_ID:
        return
    if message.author.id not in ALLOWED_USER_IDS:
        return
    if message.content.strip().startswith("!"):
        # コマンドは記録しない
        try:
            await handle_command(message)
        except Exception as e:  # noqa: BLE001
            log(f"NG command: {e!r}")
            await message.reply(f"⚠️ `{clip(str(e), 300)}`", mention_author=False)
        return
    await safe_react(message, "⏳")
    try:
        async with _work_lock:
            reply, entry_id, day, audio_paths = await handle(message)
            bot_msg = None
            if reply:
                sent = await message.reply(reply, mention_author=False)
                bot_msg = sent.id
            if day is not None:
                _remember(entry_id, day, bot_msg, audio_paths)
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
        lines.append(await classify_and_record(text, dt, day, name))
    sent = await _send("\n".join(lines))
    _remember(name, day, sent.id, [f"{AUDIO_DIR}/{dt:%Y-%m}/{name}"])


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


# ---- 取り消し ----

def _fallback_day(entry_id):
    """対応表に無い記録（この機能を入れる前のものなど）の日付を、ID から推定する。"""
    if entry_id.startswith("discord:"):
        dt = discord.utils.snowflake_time(int(entry_id.split(":", 1)[1])).astimezone(JST)
    else:
        dt = recorded_at(entry_id)
    return log_date(dt)


async def delete_entry(entry_id):
    """記録を消す。raw に見つかって消せたら True。"""
    async with _work_lock:
        info = _state.get(entry_id) or {}
        day = (datetime.strptime(info["day"], "%Y-%m-%d").date()
               if info.get("day") else _fallback_day(entry_id))
        removed = await asyncio.to_thread(remove_raw_entry, day, entry_id)
        lines = await asyncio.to_thread(cl.unrecord, entry_id)
        for path in info.get("audio", []):
            try:
                await asyncio.to_thread(delete_path, path)
            except Exception as e:  # noqa: BLE001
                log(f"NG delete audio {path}: {e!r}")
        if info.get("bot_msg"):
            try:
                msg = await (await _channel()).fetch_message(info["bot_msg"])
                await msg.delete()
            except discord.HTTPException:
                pass
        if entry_id in _state:
            del _state[entry_id]
            _save_state()
    log(f"DELETE {entry_id}: raw={removed} lines={lines} audio={len(info.get('audio', []))}")
    return removed or lines > 0


@client.event
async def on_raw_message_delete(payload):
    """自分の投稿を Discord で消したら、記録からも消す。"""
    if payload.channel_id != CHANNEL_ID:
        return
    if _entry_for_bot_msg(payload.message_id):
        return  # bot の返信が消されただけ。記録は残す（消したいときは 🗑）
    entry_id = f"discord:{payload.message_id}"
    try:
        if await delete_entry(entry_id):
            await _send("🗑️ 削除された投稿を、記録からも消しました")
    except Exception as e:  # noqa: BLE001
        log(f"NG delete {entry_id}: {e!r}\n{traceback.format_exc()}")


@client.event
async def on_raw_reaction_add(payload):
    """🗑 のリアクションで記録を消す（bot の返信にも、自分の投稿にも付けられる）。"""
    if payload.channel_id != CHANNEL_ID or payload.user_id not in ALLOWED_USER_IDS:
        return
    if str(payload.emoji).replace("️", "") != TRASH_EMOJI:
        return
    entry_id = _entry_for_bot_msg(payload.message_id) or f"discord:{payload.message_id}"
    try:
        if await delete_entry(entry_id):
            await _send("🗑️ 記録から消しました")
        else:
            await _send("🤔 消せる記録が見つかりませんでした")
    except Exception as e:  # noqa: BLE001
        log(f"NG delete {entry_id}: {e!r}\n{traceback.format_exc()}")
        await _send(f"⚠️ 削除に失敗しました: `{clip(str(e), 300)}`")


# ---- 毎朝のモーニングブリーフ ----

@tasks.loop(time=DIGEST_TIME)
async def morning_digest():
    try:
        async with _work_lock:
            text = await asyncio.to_thread(build_brief, datetime.now(JST).date())
        await _send(text)
    except Exception as e:  # noqa: BLE001
        log(f"NG brief: {e!r}")
        try:
            await _send(f"⚠️ モーニングブリーフを作れませんでした: `{clip(str(e), 300)}`")
        except Exception:  # noqa: BLE001
            pass


@morning_digest.before_loop
async def before_morning_digest():
    await client.wait_until_ready()


@client.event
async def on_ready():
    log(f"Recall bot ready as {client.user} (channel={CHANNEL_ID}, digest={DIGEST_TIME:%H:%M})")
    if not poll_inbox.is_running():
        poll_inbox.start()
    if not morning_digest.is_running():
        morning_digest.start()


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
