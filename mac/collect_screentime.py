#!/usr/bin/env python3
"""スクリーンタイムの収集係（Mac で launchd が1時間おきに動かす）。

iPhone から iCloud で同期されてきたアプリ使用記録と、この Mac 自身の分（Biome の App.InFocus）、
Mac の画面が点いていた区間（knowledgeC の /display/isBacklit）を読み、
前回からの新しい分だけを日付ごとの JSONL にして、pk-206 の ~/recall-data/screentime/ へ rsync で送る。

- 読むだけ。Biome は書き換えない。中身の解釈（どの項目が何か）は後で決めるので、取れる項目は全部残す
- 出力: ~/Library/Application Support/recall-screentime/out/YYYY-MM-DD.jsonl（JST の暦日）
        out/_arrivals.jsonl … いつ・どの端末の・何時までの記録が届いたか（同期の遅れを見る）
- 確認用: --dump で直近の記録を表示（送らない・状態も進めない）
"""
import datetime as dt
import glob
import json
import os
import sqlite3
import struct
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
BIOME = f"{HOME}/Library/Biome"
STREAM = "App.InFocus"
WORK = f"{HOME}/Library/Application Support/recall-screentime"
OUT = f"{WORK}/out"
STATE = f"{WORK}/state.json"
KNOWLEDGE = f"{HOME}/Library/Application Support/Knowledge/knowledgeC.db"
REMOTE = os.environ.get("RECALL_ST_REMOTE", "pk-206:recall-data/screentime/")
JST = dt.timezone(dt.timedelta(hours=9))
APPLE = 978307200
T_LO, T_HI = 694224000, 1262304000   # 2023〜2041（Apple 秒）: 時刻らしい double の範囲


def iso(apple_sec):
    return dt.datetime.fromtimestamp(apple_sec + APPLE, JST).isoformat(timespec="milliseconds")


# ---------- SEGB（Biome のファイル形式） ----------

def segb_entries(data):
    """(番号, 状態, 作成時刻(Apple秒), 中身bytes) を古い順に返す。v2（先頭が SEGB）と v1（52バイト目が SEGB）に対応。"""
    if data[:4] == b"SEGB":                       # v2: 32バイトのヘッダ、末尾に16バイトずつの索引
        count = struct.unpack_from("<I", data, 4)[0]
        if count * 16 > len(data):
            return
        recs = [struct.unpack_from("<IId", data, len(data) - 16 * (i + 1)) for i in range(count)]
        # 索引の「終わり位置」がファイル先頭からかヘッダの後ろからか資料で曖昧なので、両方試して読める方を使う
        best = max((_v2_walk(data, recs, base) for base in (0, 32)), key=len)
        yield from best
    elif data[52:56] == b"SEGB":                  # v1: 56バイトのヘッダ、各記録に32バイトの頭
        end_all = struct.unpack_from("<i", data, 0)[0] or len(data)
        off, i = 56, 0
        while off + 32 <= min(end_all, len(data)):
            length, state, ts1, _ts2, _crc, _u = struct.unpack_from("<iiddII", data, off)
            if length <= 0:
                break
            yield i, state, ts1, data[off + 32:off + 32 + length]
            off = (off + 32 + length + 7) & ~7
            i += 1


def _v2_walk(data, recs, base):
    out, start = [], 32
    for i, (end, state, ts) in enumerate(recs):
        end += base
        payload = data[start + 8:end]
        if not (start + 8 <= end <= len(data)) or (payload.strip(b"\0") and pb(payload) is None):
            break
        out.append((i, state, ts, data[start + 8:end]))
        start = (end + 3) & ~3
    return out


# ---------- protobuf（型定義なしで読む） ----------

def _varint(b, i):
    v = s = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << s
        if c < 0x80:
            return v, i
        s += 7


def pb(b, depth=0):
    """{"番号": 値 or [値…]}。文字列は str、入れ子は dict、読めないものは長さだけ。壊れていたら None"""
    out, i = {}, 0
    try:
        while i < len(b):
            key, i = _varint(b, i)
            f, wt = key >> 3, key & 7
            if f == 0:
                return None
            if wt == 0:
                v, i = _varint(b, i)
            elif wt == 1:
                v = struct.unpack_from("<d", b, i)[0]
                i += 8
            elif wt == 5:
                v = struct.unpack_from("<f", b, i)[0]
                i += 4
            elif wt == 2:
                n, i = _varint(b, i)
                raw = b[i:i + n]
                i += n
                if i > len(b):
                    return None
                try:
                    v = raw.decode("utf-8")
                    if not v.isprintable():
                        raise ValueError
                except ValueError:
                    v = (pb(raw, depth + 1) if depth < 3 else None) or f"<{n}B>"
            else:
                return None
            k = str(f)
            out[k] = v if k not in out else (out[k] if isinstance(out[k], list) else [out[k]]) + [v]
    except (IndexError, struct.error):
        return None
    return out


def _walk(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk(v)
    else:
        yield x


def summarize(fields):
    """後で使いやすいように、時刻らしい double とアプリIDらしい文字列を拾っておく（元の項目も残す）"""
    times = [v for v in _walk(fields) if isinstance(v, float) and T_LO < v < T_HI]
    apps = [v for v in _walk(fields) if isinstance(v, str) and v.count(".") >= 2 and " " not in v]
    return times, (apps[0] if apps else None)


# ---------- 端末と状態 ----------

def iphone_dir():
    """同期されてきた iOS 端末（platform 2）のうち、最後に同期したもの"""
    try:
        con = sqlite3.connect(f"file:{BIOME}/sync/sync.db?mode=ro", uri=True)
        rows = con.execute("SELECT device_identifier FROM DevicePeer WHERE me = 0 AND platform = 2 "
                           "ORDER BY last_sync_date DESC").fetchall()
    except sqlite3.Error as e:
        log(f"sync.db を読めない: {e}")
        return None
    for (dev,) in rows:
        d = f"{BIOME}/streams/restricted/{STREAM}/remote/{dev}"
        if os.path.isdir(d):
            return d
    return None


def sources():
    out = []
    d = iphone_dir()
    if d:
        out.append(("iphone", d))
    local = f"{BIOME}/streams/restricted/{STREAM}/local"
    if os.path.isdir(local):
        out.append(("mac", local))
    return out


def files_in(d):
    # tombstone/ は消された・古くなった分。取り込み済みの重複になるので見ない
    return sorted(p for p in glob.glob(f"{d}/*") if os.path.isfile(p))


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def log(msg):
    print(f"{dt.datetime.now(JST):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def backlit(state):
    """Mac の画面の点灯・消灯の区間（前回より新しい行だけ）。App.InFocus はアプリが前面のまま放置しても続くので、
    実際に使っていたかはこれと重ねて判断する。{"kind": "backlit", "on": true/false, "t": 開始, "end": 終了}"""
    try:
        con = sqlite3.connect(f"file:{KNOWLEDGE}?mode=ro", uri=True)
        rows = con.execute("SELECT Z_PK, ZSTARTDATE, ZENDDATE, ZVALUEINTEGER FROM ZOBJECT "
                           "WHERE ZSTREAMNAME = '/display/isBacklit' AND Z_PK > ? ORDER BY Z_PK",
                           (state.get("_backlit_pk", 0),)).fetchall()
    except sqlite3.Error as e:
        log(f"knowledgeC を読めない: {e}")
        return []
    out = []
    for pk, s, e, v in rows:
        state["_backlit_pk"] = max(state.get("_backlit_pk", 0), pk)
        if s and e:
            out.append({"device": "mac", "kind": "backlit", "on": bool(v), "t": iso(s), "end": iso(e)})
    return out


# ---------- 本体 ----------

def collect(dump=False):
    state = load_state()
    new_by_day, arrivals = {}, []
    for device, d in sources():
        n_new, latest = 0, None
        for path in files_in(d):
            data = open(path, "rb").read()
            key = f"{device}/{os.path.basename(path)}"
            done = state.get(key, -1)
            for i, st, ts, payload in segb_entries(data):
                if i <= done and not dump:
                    continue
                if not payload.strip(b"\0"):          # 消された記録（中身がゼロ埋め）
                    done = max(done, i)
                    continue
                fields = pb(payload)
                if fields is None:
                    fields = {"raw": payload.hex()}
                # 実データで確認した意味（2026-10-15）: 3=開いた1/閉じた0, 4=時刻, 6=アプリID,
                # 1=切り替えの理由（iPhone）, 2=種類（3=ロック画面の上のウィジェット等）。他の項目も fields に残す
                times, app = summarize(fields)
                t = fields.get("4") if isinstance(fields.get("4"), float) else (min(times) if times else ts)
                app = fields.get("6") if isinstance(fields.get("6"), str) else app
                ev = {"device": device, "t": iso(t), "app": app,
                      "start": {1: True, 0: False}.get(fields.get("3")), "state": st,
                      "file": key, "n": i, "fields": fields}
                new_by_day.setdefault(ev["t"][:10], []).append(ev)
                done = max(done, i)
                n_new += 1
                latest = max(latest or ev["t"], ev["t"])
            state[key] = done
        if n_new:
            arrivals.append({"run_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
                             "device": device, "new": n_new, "latest": latest})
    if not dump:
        for ev in backlit(state):
            new_by_day.setdefault(ev["t"][:10], []).append(ev)

    if dump:
        evs = sorted((e for v in new_by_day.values() for e in v), key=lambda e: e["t"])
        for e in evs[-40:]:
            print(e["device"], e["t"][5:23], "開" if e["start"] else "閉", e["app"], json.dumps(e["fields"], ensure_ascii=False)[:160])
        return

    os.makedirs(OUT, exist_ok=True)
    for day, evs in sorted(new_by_day.items()):
        with open(f"{OUT}/{day}.jsonl", "a") as f:
            for e in sorted(evs, key=lambda e: e["t"]):
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    with open(f"{OUT}/_arrivals.jsonl", "a") as f:
        for a in arrivals:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    tmp = STATE + ".tmp"
    json.dump(state, open(tmp, "w"), indent=0)
    os.replace(tmp, STATE)
    for a in arrivals:
        log(f"{a['device']}: {a['new']}件（最新 {a['latest'][5:16]}）")

    host, path = REMOTE.split(":", 1)
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    r = subprocess.run(ssh + [host, f"mkdir -p {path}"], capture_output=True, text=True)
    if r.returncode == 0:
        r = subprocess.run(["rsync", "-a", "-e", " ".join(ssh), f"{OUT}/", REMOTE], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"pk-206 へ送れず（次の回にまとめて送る）: {r.stderr.strip()[:200]}")
    elif arrivals:
        log("pk-206 へ送信済み")


if __name__ == "__main__":
    collect(dump="--dump" in sys.argv)
