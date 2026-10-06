#!/usr/bin/env python3
import os, sys, re, tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
import requests

NC_BASE = "http://192.168.11.200:8081"
NC_USER = os.environ["NC_USER"]
NC_PASS = os.environ["NC_PASS"]
DAV = f"{NC_BASE}/remote.php/dav/files/{NC_USER}"

INBOX = "/VoiceLog/Inbox"
AUDIO_DIR = "/VoiceLog/audio"
RAW_DIR = "/ob/ボイスログ/raw"

MODEL = "large-v3-turbo"
DAY_START_HOUR = 4      # この時刻より前の録音は前日扱い
MIN_AGE_SEC = 120       # 届いてからこの秒数が経ったものだけ処理
AUDIO_EXT = (".m4a", ".mp3", ".wav", ".aac", ".caf")
JST = timezone(timedelta(hours=9))

s = requests.Session()
s.auth = (NC_USER, NC_PASS)


def url(path):
    return DAV + quote(path)


def log(msg):
    print(f"{datetime.now(JST):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def list_inbox():
    body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
            '<d:getlastmodified/><d:resourcetype/></d:prop></d:propfind>')
    r = s.request("PROPFIND", url(INBOX + "/"), data=body, headers={"Depth": "1"})
    r.raise_for_status()
    ns = {"d": "DAV:"}
    items = []
    for resp in ET.fromstring(r.content).findall("d:response", ns):
        if resp.find(".//d:collection", ns) is not None:
            continue
        name = unquote(resp.find("d:href", ns).text).rstrip("/").split("/")[-1]
        if not name.lower().endswith(AUDIO_EXT):
            continue
        lm = resp.find(".//d:getlastmodified", ns)
        mtime = parsedate_to_datetime(lm.text) if lm is not None else None
        items.append((name, mtime))
    return sorted(items)


def recorded_at(name):
    m = re.match(r"(\d{4}-\d{2}-\d{2}_\d{6})", name)
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d_%H%M%S").replace(tzinfo=JST)
    return datetime.now(JST)


def log_date(dt):
    return (dt - timedelta(hours=DAY_START_HOUR)).date()


def mkcol(path):
    cur = ""
    for p in path.strip("/").split("/"):
        cur += "/" + p
        r = s.request("MKCOL", url(cur))
        if r.status_code not in (201, 405):  # 405 = 既に存在
            r.raise_for_status()


def append_raw(day, dt, name, text):
    path = f"{RAW_DIR}/{day.isoformat()}.md"
    r = s.get(url(path))
    if r.status_code == 404:
        content = f"# ボイスログ {day.isoformat()}（文字起こし）\n"
    else:
        r.raise_for_status()
        content = r.content.decode("utf-8")
    marker = f"<!-- {name} -->"
    if marker in content:
        return
    content += f"\n## {dt:%H:%M}\n{marker}\n{text}\n"
    mkcol(RAW_DIR)
    s.put(url(path), data=content.encode("utf-8")).raise_for_status()


def archive(name, dt):
    dest_dir = f"{AUDIO_DIR}/{dt:%Y-%m}"
    mkcol(dest_dir)
    r = s.request("MOVE", url(f"{INBOX}/{name}"),
                  headers={"Destination": url(f"{dest_dir}/{name}"), "Overwrite": "F"})
    r.raise_for_status()


def main():
    now = datetime.now(timezone.utc)
    items = list_inbox()
    targets = [n for n, mt in items
               if mt is None or (now - mt).total_seconds() >= MIN_AGE_SEC]
    if not targets:
        if items:
            log(f"待機中: {len(items)}件（届いてから{MIN_AGE_SEC}秒未満）")
        return
    from faster_whisper import WhisperModel
    model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=4)
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
                segments, info = model.transcribe(
                    tmp.name, language="ja", vad_filter=True,
                    condition_on_previous_text=False)
                text = "\n".join(seg.text.strip() for seg in segments).strip()
            append_raw(log_date(dt), dt, name, text or "（音声なし）")
            archive(name, dt)
            log(f"OK {name}（{info.duration:.0f}秒）")
        except Exception as e:
            log(f"NG {name}: {e}")


if __name__ == "__main__":
    main()
