"""Nextcloud（WebDAV）とObsidian側の書き込みをまとめた共通部品。

Recall の各プログラム（bot.py / voicelog.py / summarize.py）はここを通して ob に書く。
同じファイルへ同時に追記しても消えないよう、pk-206 内のファイルロックで直列化する。
"""
import os
import fcntl
import contextlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote
import xml.etree.ElementTree as ET

import requests

NC_BASE = os.environ.get("NC_BASE", "http://192.168.11.200:8081")
NC_USER = os.environ["NC_USER"]
NC_PASS = os.environ["NC_PASS"]
DAV = f"{NC_BASE}/remote.php/dav/files/{NC_USER}"

OB_ROOT = "/ob/Recall"
RAW_DIR = f"{OB_ROOT}/raw"          # その日の全記録（唯一の元データ）
JOURNAL_DIR = f"{OB_ROOT}/日記"      # 朝4時の日次まとめ

DAY_START_HOUR = 4                   # この時刻より前は前日扱い
JST = timezone(timedelta(hours=9))
LOCK_PATH = "/tmp/recall-ob.lock"

SOURCE_ICON = {"voice": "🎙️", "discord": "💬", "discord-voice": "🎙️"}

s = requests.Session()
s.auth = (NC_USER, NC_PASS)


def url(path):
    return DAV + quote(path)


def log(msg):
    print(f"{datetime.now(JST):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def log_date(dt):
    return (dt - timedelta(hours=DAY_START_HOUR)).date()


def mkcol(path):
    cur = ""
    for p in path.strip("/").split("/"):
        cur += "/" + p
        r = s.request("MKCOL", url(cur))
        if r.status_code not in (201, 405):  # 405 = 既に存在
            r.raise_for_status()


def get_text(path):
    r = s.get(url(path))
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content.decode("utf-8")


def put_text(path, text):
    mkcol(path.rsplit("/", 1)[0])
    s.put(url(path), data=text.encode("utf-8")).raise_for_status()


def put_bytes(path, data):
    mkcol(path.rsplit("/", 1)[0])
    s.put(url(path), data=data).raise_for_status()


AUDIO_DIR = "/VoiceLog/audio"        # 元の音声（ショートカット経由も Discord 経由もここ）


def list_dir(path):
    """[(name, mtime(UTC) or None, is_dir)]。フォルダが無ければ空。"""
    body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
            '<d:getlastmodified/><d:resourcetype/></d:prop></d:propfind>')
    r = s.request("PROPFIND", url(path.rstrip("/") + "/"), data=body,
                  headers={"Depth": "1"})
    if r.status_code == 404:
        return []
    r.raise_for_status()
    ns = {"d": "DAV:"}
    out = []
    responses = ET.fromstring(r.content).findall("d:response", ns)
    for resp in responses[1:]:  # 先頭はフォルダ自身
        name = unquote(resp.find("d:href", ns).text).rstrip("/").split("/")[-1]
        is_dir = resp.find(".//d:collection", ns) is not None
        lm = resp.find(".//d:getlastmodified", ns)
        mtime = parsedate_to_datetime(lm.text) if lm is not None else None
        out.append((name, mtime, is_dir))
    return out


@contextlib.contextmanager
def ob_lock():
    """bot と cron が同じ md を同時に書き換えて片方が消えるのを防ぐ。"""
    with open(LOCK_PATH, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def append_raw(dt, entry_id, text, source):
    """その日の raw に1件追記する。戻り値 (日付, 新規か)。同じ entry_id は二重に書かない。"""
    day = log_date(dt)
    path = f"{RAW_DIR}/{day.isoformat()}.md"
    marker = f"<!-- {entry_id} -->"
    # 本文に "<!--" があると件数の数え方が狂うので無害化する
    text = (text or "").replace("<!--", "<!‐‐").strip() or "（内容なし）"
    with ob_lock():
        content = get_text(path)
        if content is None:
            content = f"# Recall {day.isoformat()}（原文）\n"
        if marker in content:
            return day, False
        icon = SOURCE_ICON.get(source, "")
        content += f"\n## {dt:%H:%M} {icon}\n{marker}\n{text}\n"
        put_text(path, content)
    return day, True


def append_line(path, header, line):
    """md の末尾に1行足す。ファイルが無ければ header から作る。"""
    with ob_lock():
        content = get_text(path)
        if content is None:
            content = header
        if not content.endswith("\n"):
            content += "\n"
        content += line + "\n"
        put_text(path, content)
