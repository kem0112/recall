#!/usr/bin/env python3
"""試験: 睡眠を「1日」の枠なしで推定する（Codex に判定させる）。記録はどこにも書かない（表示だけ）。

連続した記録（iPhone/Mac の使用 + Recall の言葉）から、両方の端末が使われていない空白を睡眠の候補として拾い、
Codex に「睡眠か・起きていたか・外出か」と、入眠・起床の時刻を判定させる。
  python3 ~/dev/recall/tools/sleep_trial.py            # iPhone の記録が届いている最後の4日間
  python3 ~/dev/recall/tools/sleep_trial.py 6          # 最後の6日間
  python3 ~/dev/recall/tools/sleep_trial.py 4 --no-words   # 言葉なし（スクリーンタイムだけ）で比べる
Mac で動かす（Mac の画面点灯の記録と、Nextcloud 同期フォルダの ob を読むため）。
"""
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
ST_DIRS = [os.path.expanduser("~/Library/Application Support/recall-screentime/out"),
           os.path.expanduser("~/recall-data/screentime")]
RAW_DIRS = glob.glob(os.path.expanduser("~/Library/CloudStorage/Nextcloud-*/ob/Recall/raw"))
KNOWLEDGE = os.path.expanduser("~/Library/Application Support/Knowledge/knowledgeC.db")
MERGE_GAP = timedelta(minutes=3)      # これより短い切れ目は「ずっと使っていた」とみなす
CAND_GAP = timedelta(minutes=60)      # これ以上の空白を睡眠の候補にする
WORDS_MAX = 400                       # 言葉1件あたりの最大文字数
NAME = {"com.hammerandchisel.discord": "Discord", "com.apple.mobilesafari": "Safari",
        "com.brave.Browser": "Brave", "com.apple.Terminal": "ターミナル", "com.atebits.Tweetie2": "X",
        "com.anthropic.claudefordesktop": "Claude", "com.openai.codex": "Codex",
        "com.apple.springboard.today-view": "ウィジェット(ロック画面)", "com.apple.ShortcutsUI": "ショートカット"}


# ---------- スクリーンタイム ----------

def load_events():
    d = next((x for x in ST_DIRS if os.path.isdir(x)), None)
    if not d:
        sys.exit("スクリーンタイムのデータが見つかりません")
    evs = []
    for f in sorted(glob.glob(f"{d}/20*.jsonl")):
        for line in open(f, encoding="utf-8"):
            e = json.loads(line)
            if e.get("start") is None or not e.get("app"):
                continue
            evs.append((datetime.fromisoformat(e["t"]), e["device"], e["app"], e["start"]))
    return sorted(evs)


def mac_backlit():
    """Mac の画面が点いていた区間。アプリが前面のまま放置された時間を除くのに使う（Mac でだけ読める）"""
    try:
        con = sqlite3.connect(f"file:{KNOWLEDGE}?mode=ro", uri=True)
        rows = con.execute("SELECT ZSTARTDATE, ZENDDATE FROM ZOBJECT WHERE ZSTREAMNAME = '/display/isBacklit' "
                           "AND ZVALUEINTEGER = 1 ORDER BY ZSTARTDATE").fetchall()
    except sqlite3.Error:
        return None
    f = lambda x: datetime.fromtimestamp(x + 978307200, JST)
    return [(f(s), f(e)) for s, e in rows if s and e]


def intervals(evs, device, on=None):
    """アプリの開いた〜閉じたを組にし、端末が使われていた区間にまとめる（Mac のロック中・画面消灯中は除く）"""
    open_, raw = {}, []
    for t, dev, app, start in evs:
        if dev != device:
            continue
        if start:
            open_[app] = t
        elif app in open_:
            s = open_.pop(app)
            if app != "com.apple.loginwindow" and t >= s:
                raw.append((s, t, app))
    if on is not None:
        raw = [(max(s, a), min(e, b), app) for s, e, app in raw for a, b in on if max(s, a) < min(e, b)]
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


# ---------- Recall の言葉 ----------

def load_words(lo, hi):
    """raw の各記録に実際の日時を付ける。ファイルの日付から始め、時刻が戻ったら翌日に進める
    （1日の区切りは途中で4時→6時に変わったが、この方法ならどちらでも正しい）"""
    if not RAW_DIRS:
        return None
    out = []
    for f in sorted(glob.glob(f"{RAW_DIRS[0]}/20*.md")):
        day = datetime.fromisoformat(os.path.basename(f)[:10]).date()
        if not (lo.date() - timedelta(days=2) <= day <= hi.date()):
            continue
        prev, cur = None, None
        for block in re.split(r"\n(?=## \d\d:\d\d)", open(f, encoding="utf-8").read()):
            m = re.match(r"## (\d\d):(\d\d) *(\S*)\n(.*)", block, re.S)
            if not m:
                continue
            hm = int(m.group(1)) * 60 + int(m.group(2))
            if prev is not None and hm < prev:
                day += timedelta(days=1)
            prev = hm
            t = datetime(day.year, day.month, day.day, hm // 60, hm % 60, tzinfo=JST)
            text = re.sub(r"<!--.*?-->", "", m.group(4), flags=re.S)
            text = re.sub(r"\s+", " ", text).strip()
            if lo <= t <= hi and text:
                out.append((t, m.group(3), text[:WORDS_MAX] + ("…" if len(text) > WORDS_MAX else "")))
    return out


# ---------- 組み立て ----------

def build(days, use_words):
    evs = load_events()
    on = mac_backlit()
    last_iphone = max(t for t, d, *_ in evs if d == "iphone")
    hi = last_iphone
    lo = hi - timedelta(days=days)
    act = []
    for dev, label in (("iphone", "iPhone"), ("mac", "Mac")):
        for s, e, apps in intervals(evs, dev, on if dev == "mac" else None):
            if e >= lo and s <= hi:
                act.append((max(s, lo), min(e, hi), label, apps))
    act.sort()

    # 両方の端末をまとめた「使っていた」区間と、その間の空白（候補）
    busy = []
    for s, e, *_ in act:
        if busy and s <= busy[-1][1]:
            busy[-1][1] = max(busy[-1][1], e)
        else:
            busy.append([s, e])
    # 範囲の最初・最後の空白も候補にする（端が範囲外にかかることは Codex に伝える）
    edges = [[lo, lo]] + busy + [[hi, hi]]
    cands = [(a[1], b[0]) for a, b in zip(edges, edges[1:]) if b[0] - a[1] >= CAND_GAP]

    usage = "\n".join(f"{s:%m/%d %H:%M}〜{e:%H:%M} {lab}（{', '.join(sorted(apps)[:6])}）{(e - s).total_seconds() / 60:.0f}分"
                      for s, e, lab, apps in act)
    cand = "\n".join(f"候補{i + 1}: {s:%m/%d %H:%M} 〜 {e:%m/%d %H:%M}（{(e - s).total_seconds() / 3600:.1f}時間 何も使っていない）"
                     + ("【範囲の始まりにかかる】" if s == lo else "") + ("【範囲の終わりにかかる】" if e == hi else "")
                     for i, (s, e) in enumerate(cands))
    words = load_words(lo, hi) if use_words else None
    wtext = ("\n".join(f"{t:%m/%d %H:%M} {kind} {txt}" for t, kind, txt in words) or "(なし)") if words is not None else None
    return lo, hi, on is not None, usage, cand, wtext


PROMPT = """あなたは睡眠記録の判定係です。私（イケマ, 夜型の大学院生）は睡眠がとても不規則で、昼も夜も関係なく寝ます。
「1日」や「夜」という枠は考えず、{lo:%m/%d %H:%M}〜{hi:%m/%d %H:%M} の連続した記録から、寝ていた区間をすべて見つけてください。
ツールやコマンドは使わないでください。

# 材料
- 使用記録: iPhone と Mac を使っていた時間（使っている間は起きている）。{mac_note}
- 候補: 両方の端末が1時間以上使われていない空白。睡眠かもしれないし、外出・移動・スマホを見ずに起きていた時間かもしれない
{words_note}

# 判断のしかた
- 候補ごとに「睡眠 / 起きていた / 外出・移動 / 不明」を決める
- 睡眠なら、入眠と起床の時刻を決める。ふつうは空白の両端だが、言葉（「今から寝る」「寝られへん」「今起きた」など）があればそれで補正する
- 夜中に数分だけの使用（ロック画面のウィジェット、時計など）で空白が2つに割れている場合は、1つの睡眠＋中途覚醒として扱う（候補をまたいでよい）
- 言葉の中の予定（「4時間しか寝られへん」「10時に起きる」）は予定にすぎない。実際の使用記録と食い違えば使用記録を優先
- 記録の最初・最後にかかる部分は「範囲外」と書く

# 出力（日本語、この形だけ）
## 睡眠
- MM/DD HH:MM 〜 MM/DD HH:MM（X時間Y分）確からしさ 高/中/低 ／ 根拠1行（中途覚醒があれば書く）
## 睡眠ではないと判断した候補
- 候補N: 起きていた/外出・移動/不明 ／ 根拠1行
## 気づいたこと
- 1〜3行（記録から見える睡眠の傾向・判定に足りない情報）

# 候補
{cand}

# 使用記録（3分以内の切れ目はつなげてある）
{usage}
{words}"""


def ask_codex(prompt):
    if not shutil.which("codex"):
        return None
    with tempfile.TemporaryDirectory() as work:
        out = os.path.join(work, "out.md")
        p = subprocess.run(["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                            "--output-last-message", out, "-"], input=prompt, text=True,
                           cwd=work, capture_output=True, timeout=900)
        if p.returncode != 0:
            return f"(codex 失敗: {p.stderr[-300:]})"
        return open(out, encoding="utf-8").read().strip()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = int(args[0]) if args else 4
    use_words = "--no-words" not in sys.argv
    lo, hi, backlit, usage, cand, words = build(days, use_words)
    mac_note = ("Mac は画面が点いていた時間だけに絞ってある" if backlit else
                "Mac はアプリが前面にあった時間で、開いたまま放置した時間も含む（Mac だけの使用は起きていた証拠として弱い）")
    if words is None:
        words_note = "- 言葉: なし（使用記録だけで判断する）"
        wblock = ""
    else:
        words_note = "- 言葉: Recall に話した・書いた記録（時刻つき）。寝る・起きた・寝られない等の発言は強い手がかり"
        wblock = f"\n# 言葉（Recall の記録）\n{words}\n"
    prompt = PROMPT.format(lo=lo, hi=hi, mac_note=mac_note, words_note=words_note, cand=cand or "(なし)",
                           usage=usage or "(なし)", words=wblock)
    print(f"範囲 {lo:%m/%d %H:%M}〜{hi:%m/%d %H:%M}（iPhone の記録が届いている所まで）"
          f" / Mac の画面点灯: {'使う' if backlit else 'なし'} / 言葉: {'使う' if words is not None else 'なし'}")
    print(f"--- 候補（1時間以上の空白）\n{cand or '(なし)'}")
    ans = ask_codex(prompt)
    if ans is None:
        path = "/tmp/sleep_prompt.md"
        open(path, "w", encoding="utf-8").write(prompt)
        print(f"\n(codex が見つからないので、頼む文章を {path} に保存しました)")
    else:
        print(f"\n--- Codex の判定\n{ans}")
    print(f"\n(Codex に渡した文章: {len(prompt)}字)")


if __name__ == "__main__":
    main()
