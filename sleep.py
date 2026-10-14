"""睡眠の判定係（pk-206, cron で1時間おき）。

Mac の収集係が送ってくるスクリーンタイム（~/recall-data/screentime）に iPhone の新しい記録が届いていたら、
直近5日を「1日の枠なし」で見直し、Codex に寝ていた区間を判定させて /ob/Recall/睡眠.md に書く。

- 材料: iPhone/Mac の使用（Mac は画面点灯中だけ）・Google カレンダー（バイトのシフト・予定）・raw の言葉
- 候補（両端末が1時間以上使われていない空白）は Python が拾い、Codex は睡眠か・入眠/起床の時刻を決める
- 睡眠.md の行の末尾に #手直し があれば、その行は書き換えない（その区間は確定として Codex にも渡す）
- 窓の最初の12時間は端が切れて判定が不安定なので、そこに始まる区間は前回の結果を残す
  python sleep.py            # iPhone の新しい記録が届いていれば判定
  python sleep.py --force    # 届いていなくても判定
  python sleep.py --dry-run  # 判定して表示だけ（書かない）
"""
import glob
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

from ai import run_codex, load_prompt
from nc import OB_ROOT, RAW_DIR, JST, get_text, put_text, ob_lock, log

HERE = os.path.dirname(os.path.abspath(__file__))
ST_DIR = os.path.expanduser(os.environ.get("RECALL_SCREENTIME_DIR", "~/recall-data/screentime"))
STATE = os.path.join(HERE, "state", "sleep.json")
SLEEP_PATH = f"{OB_ROOT}/睡眠.md"
DAYS = 5
SETTLE = timedelta(hours=12)          # 窓の最初のこの時間に始まる区間は書き換えない
MERGE_GAP = timedelta(minutes=3)
CAND_GAP = timedelta(minutes=60)
WORDS_MAX = 400
FMT = "%Y-%m-%d %H:%M"
HEADER = ("# 睡眠\n\n"
          "pk-206 が自動で判定（スクリーンタイム・カレンダー・Recall の言葉から）。"
          "違っていたら時刻を直して行の末尾に #手直し を付けると、以後その行は書き換えない。\n\n")
NAME = {"com.hammerandchisel.discord": "Discord", "com.apple.mobilesafari": "Safari", "com.atebits.Tweetie2": "X",
        "com.brave.Browser": "Brave", "com.apple.Terminal": "ターミナル", "com.anthropic.claudefordesktop": "Claude",
        "com.openai.codex": "Codex", "com.apple.springboard.today-view": "ウィジェット(ロック画面)",
        "com.apple.ShortcutsUI": "ショートカット", "com.apple.mobiletimer": "時計"}


# ---------- スクリーンタイム ----------

def load_screentime():
    apps, lit = [], []
    for f in sorted(glob.glob(f"{ST_DIR}/20*.jsonl")):
        for line in open(f, encoding="utf-8"):
            e = json.loads(line)
            if e.get("kind") == "backlit":
                if e.get("on"):
                    lit.append((datetime.fromisoformat(e["t"]), datetime.fromisoformat(e["end"])))
            elif e.get("start") is not None and e.get("app"):
                apps.append((datetime.fromisoformat(e["t"]), e["device"], e["app"], e["start"]))
    return sorted(apps), sorted(lit)


def intervals(apps, device, lit=None):
    """アプリの開いた〜閉じたを組にし、使われていた区間にまとめる（Mac のロック中・画面消灯中は除く）"""
    open_, raw = {}, []
    for t, dev, app, start in apps:
        if dev != device:
            continue
        if start:
            open_[app] = t
        elif app in open_:
            s = open_.pop(app)
            if app != "com.apple.loginwindow" and t >= s:
                raw.append((s, t, app))
    if lit is not None:
        raw = [(max(s, a), min(e, b), app) for s, e, app in raw for a, b in lit if max(s, a) < min(e, b)]
    raw.sort()
    merged = []
    for s, e, app in raw:
        label = NAME.get(app, app.split(".")[-1])
        if merged and s - merged[-1][1] <= MERGE_GAP:
            m = merged[-1]
            merged[-1] = (m[0], max(m[1], e), m[2] | {label})
        else:
            merged.append((s, e, {label}))
    return merged


# ---------- カレンダー（iCal の非公開URL。合鍵なので env 以外に出さない） ----------
SHIFT = {"13": ("バイト(13-22時)", 13, 9), "17": ("バイト(17-22時)", 17, 5), "22": ("バイト夜勤(22-翌8時)", 22, 10)}


def _ics_time(key, val):
    if "VALUE=DATE" in key or len(val) == 8:
        return datetime.strptime(val[:8], "%Y%m%d").replace(tzinfo=JST), True
    if val.endswith("Z"):
        return datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(JST), False
    return datetime.strptime(val[:15], "%Y%m%dT%H%M%S").replace(tzinfo=JST), False


def load_calendar(lo, hi):
    """範囲内の予定 [(開始, 終了, 名前)]。繰り返し予定は最初の回だけ、終日の予定はシフト以外使わない"""
    out = []
    for url in os.environ.get("RECALL_ICAL_URLS", "").split():
        try:
            text = urllib.request.urlopen(url, timeout=20).read().decode("utf-8")
        except Exception as e:
            log(f"カレンダーを取得できない（{type(e).__name__}）")   # 例外の文面に URL が入るので出さない
            continue
        text = re.sub(r"\r?\n[ \t]", "", text)
        for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
            f = {}
            for line in block.strip().splitlines():
                k, _, v = line.partition(":")
                f.setdefault(k.split(";")[0], (k, v))
            if "DTSTART" not in f or f.get("STATUS", ("", ""))[1].upper() == "CANCELLED":
                continue
            title = f.get("SUMMARY", ("", ""))[1].strip()
            try:
                s, allday = _ics_time(*f["DTSTART"])
                e = _ics_time(*f["DTEND"])[0] if "DTEND" in f else None
            except ValueError:
                continue
            if title in SHIFT:
                name, h, n = SHIFT[title]
                if allday:
                    s = s.replace(hour=h)
                if allday or e is None:
                    e = s + timedelta(hours=n)
            elif allday:
                continue
            else:
                name, e = title or "(無題)", e or s + timedelta(hours=1)
            if e >= lo and s <= hi:
                out.append((s, e, name))
    return sorted(out)


# ---------- Recall の言葉 ----------

def load_words(lo, hi):
    """raw の各記録に実際の日時を付ける。音声は埋め込みのファイル名の日時。それ以外はファイルの日付から始め、
    区切り（2026-10-14 から朝6時、それより前は朝4時）より前の時刻なら翌日、途中で時刻が戻ったら翌日に進める"""
    out = []
    day0 = (lo - timedelta(days=1)).date()
    for k in range((hi.date() - day0).days + 1):
        fday = day0 + timedelta(days=k)
        text = get_text(f"{RAW_DIR}/{fday.isoformat()}.md")
        if not text:
            continue
        day, prev = fday, None
        boundary = 6 * 60 if fday >= datetime(2026, 10, 14).date() else 4 * 60
        for block in re.split(r"\n(?=## \d\d:\d\d)", text):
            m = re.match(r"## (\d\d):(\d\d) *(\S*)\n(.*)", block, re.S)
            if not m:
                continue
            hm = int(m.group(1)) * 60 + int(m.group(2))
            if (prev is None and hm < boundary) or (prev is not None and hm < prev):
                day += timedelta(days=1)
            prev = hm
            t = datetime(day.year, day.month, day.day, hm // 60, hm % 60, tzinfo=JST)
            fm = re.search(r"<!-- (\d{4}-\d\d-\d\d)_(\d\d)(\d\d)\d\d\.m4a -->", m.group(4))
            if fm:
                t = datetime.fromisoformat(f"{fm.group(1)}T{fm.group(2)}:{fm.group(3)}:00+09:00")
            body = re.sub(r"\s+", " ", re.sub(r"<!--.*?-->", "", m.group(4), flags=re.S)).strip()
            if lo <= t <= hi and body:
                out.append((t, m.group(3), body[:WORDS_MAX] + ("…" if len(body) > WORDS_MAX else "")))
    return sorted(out)


# ---------- 睡眠.md ----------
LINE_RE = re.compile(r"^- (\d{4}-\d\d-\d\d \d\d:\d\d) → (\d{4}-\d\d-\d\d \d\d:\d\d|\?)")


def parse_sleep_md(text):
    """[(開始, 終了 or None, 行, 手直しか)]"""
    out = []
    for line in (text or "").splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        s = datetime.strptime(m.group(1), FMT).replace(tzinfo=JST)
        e = None if m.group(2) == "?" else datetime.strptime(m.group(2), FMT).replace(tzinfo=JST)
        out.append((s, e, line, "#手直し" in line))
    return out


def fmt_line(s, e, conf, wake, reason):
    if e:
        mins = int((e - s).total_seconds() // 60)
        span = f"{e:{FMT}}（{mins // 60}時間{mins % 60:02d}分）"
    else:
        span = "?（起床まだ不明）"
    clean = lambda x: re.sub(r"[#\n%]", "", str(x or "")).strip()
    extra = f"／ 中途覚醒 {clean(wake)} " if clean(wake) else ""
    return f"- {s:{FMT}} → {span}確からしさ{clean(conf)} {extra}／ {clean(reason)}"


def parse_answer(text, lo, hi, locked):
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0)) if m else {}
    out = []
    for it in data.get("sleeps", []) or []:
        try:
            s = datetime.strptime(str(it["start"]).strip(), FMT).replace(tzinfo=JST)
            e = it.get("end")
            e = datetime.strptime(str(e).strip(), FMT).replace(tzinfo=JST) if e not in (None, "", "null") else None
        except (KeyError, ValueError):
            continue
        if not (lo <= s <= hi) or (e and not (s < e <= hi + timedelta(minutes=1))):
            continue
        if e and e - s > timedelta(hours=20):
            continue
        if any(s < (le or hi) and (e or hi) > ls for ls, le in locked):
            continue   # 本人が直した区間と重なるものは捨てる
        conf = it.get("confidence") if it.get("confidence") in ("高", "中", "低") else "低"
        out.append((s, e, conf, it.get("wake", ""), it.get("reason", "")))
    return sorted(out)


# ---------- 本体 ----------

def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def main():
    force, dry = "--force" in sys.argv, "--dry-run" in sys.argv
    apps, lit = load_screentime()
    iph = [t for t, d, *_ in apps if d == "iphone"]
    if not iph:
        log("睡眠: iPhone の記録がまだない")
        return
    hi = max(iph)
    state = load_state()
    if not (force or dry) and state.get("iphone_latest") == hi.isoformat():
        return   # 新しい記録が届いていない
    lo = hi - timedelta(days=DAYS)

    act = []
    for dev, label in (("iphone", "iPhone"), ("mac", "Mac")):
        for s, e, names in intervals(apps, dev, lit if dev == "mac" else None):
            if e >= lo and s <= hi:
                act.append((max(s, lo), min(e, hi), label, names))
    act.sort()
    busy = []
    for s, e, *_ in act:
        if busy and s <= busy[-1][1]:
            busy[-1][1] = max(busy[-1][1], e)
        else:
            busy.append([s, e])
    edges = [[lo, lo]] + busy + [[hi, hi]]
    cands = [(a[1], b[0]) for a, b in zip(edges, edges[1:]) if b[0] - a[1] >= CAND_GAP]

    existing = parse_sleep_md(get_text(SLEEP_PATH))
    locked = [(s, e) for s, e, _, fixed in existing if fixed]
    cal = load_calendar(lo, hi)
    words = load_words(lo, hi)
    t = lambda x: f"{x:%m/%d %H:%M}"
    prompt = (load_prompt("sleep.md")
              .replace("{{LO}}", f"{lo:{FMT}}").replace("{{HI}}", f"{hi:{FMT}}")
              .replace("{{LOCKED}}", "\n".join(f"{s:{FMT}} 〜 {e:{FMT}}" if e else f"{s:{FMT}} 〜 ?"
                                              for s, e in locked if (e or hi) >= lo) or "(なし)")
              .replace("{{CANDIDATES}}", "\n".join(
                  f"{t(s)} 〜 {t(e)}（{(e - s).total_seconds() / 3600:.1f}時間）"
                  + ("【記録の始まりにかかる】" if s == lo else "") + ("【記録の終わりにかかる】" if e == hi else "")
                  for s, e in cands) or "(なし)")
              .replace("{{CALENDAR}}", "\n".join(f"{t(s)}〜{t(e)} {n}" for s, e, n in cal) or "(なし)")
              .replace("{{USAGE}}", "\n".join(
                  f"{t(s)}〜{e:%H:%M} {lab}（{', '.join(sorted(n)[:6])}）{(e - s).total_seconds() / 60:.0f}分"
                  for s, e, lab, n in act) or "(なし)")
              .replace("{{WORDS}}", "\n".join(f"{t(w)} {k} {x}" for w, k, x in words) or "(なし)"))
    answer = run_codex(prompt, profile="main", task="sleep")
    found = parse_answer(answer, lo, hi, locked)

    if dry:
        print(f"範囲 {lo:{FMT}}〜{hi:{FMT}} / 候補 {len(cands)} / 予定 {len(cal)} / 言葉 {len(words)}")
        for s, e, conf, wake, reason in found:
            print(fmt_line(s, e, conf, wake, reason))
        return

    settle = lo + SETTLE
    with ob_lock():
        existing = parse_sleep_md(get_text(SLEEP_PATH))   # Codex の間に手で直された分も拾い直す
        first = not existing
        keep = [(s, line) for s, e, line, fixed in existing if fixed or s < settle]
        new = [(s, fmt_line(s, e, c, w, r)) for s, e, c, w, r in found
               if (first or s >= settle) and not any(s == ks for ks, _ in keep)]
        lines = [line for _, line in sorted(keep + new)]
        put_text(SLEEP_PATH, HEADER + "\n".join(lines) + "\n")
    state.update(iphone_latest=hi.isoformat(), judged_at=datetime.now(JST).isoformat(timespec="seconds"))
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w"), ensure_ascii=False, indent=1)
    log(f"睡眠: {lo:%m/%d %H:%M}〜{hi:%m/%d %H:%M} を判定、{len(new)}件を書いた（手直し {len(locked)}件はそのまま）")


if __name__ == "__main__":
    main()
