# Recall

思いついたこと・その日のことを、文字でも声でも投げておけば、pk-206 が記録・仕分け・日記化して Obsidian（ob）に入れる仕組み。

## 流れ

```
入口①  Discord #recall に投げる（文字 / 音声メッセージ）     → bot.py（常駐, systemd）
入口②  iPhoneショートカット「Voice log」→ /VoiceLog/Inbox      → bot.py が30秒おきに確認
          │ 音声は Whisper（large-v3-turbo, CPU）で文字起こし。元の音声は /VoiceLog/audio/YYYY-MM/ に保存
          ▼
/ob/Recall/raw/YYYY-MM-DD.md   その日の全記録（唯一の元データ。🎙️ 音声 / 💬 文字）
          │
          ├ すぐに: Codex で仕分け → 種類のノートに1行追記 → #recall に結果（①②とも）
          │     /ob/Recall/短期タスク.md /開発タスク.md /アイデア.md /心を動かされたもの.md /人間関係.md /体調.md /所感.md （その他は raw と日記のみ。1件から複数の項目を取り出す）
          │
          └ 毎朝6:15: summarize.py → /ob/Recall/日記/YYYY-MM-DD.md（要約・できごと・気づき・TODO・清書）
```

- 日付の区切りは朝6時（`nc.py` の `DAY_START_HOUR`。concierge の `nc.py` も同じ値にそろえる）。
- 種類は8つ固定（短期タスク・開発タスク・アイデア・心を動かされたもの・人間関係・体調・所感・その他）。2026-10-14 に長期タスク→アイデア、所感から人間関係を分離。2026-10-15 に体調を追加（あとで睡眠などと突き合わせる材料）。
- タグは `prompts/tags.txt` の一覧からだけ選ばせる（一覧外は捨てる）。一覧を編集して push すれば次の仕分けから反映。raw が元データなので、後から全件仕分け直せる（やり直しでも完了 `- [x]` は見出しが近ければ引き継ぐ）。
- Nextcloud への読み書きはすべて WebDAV。同じ md への同時書き込みは `/tmp/recall-ob.lock` で直列化。
- Codex は読み取り専用・空フォルダで動かし、資格情報もファイルも渡さない。返ってきたテキストを Python が書く。
- 照明・鍵の bot（home-control）とは別アプリ・別プロセス。Recall が落ちても家の制御には影響しない。

## タスクの確認

- `!tasks` と投稿すると、短期・開発タスク（と以前の タスク.md）の「今日・期限切れ・明日」を出す。`!` で始まる投稿は記録されない
- 拾うのは「未完了 `- [ ]`」かつ「期日 `📅 YYYY-MM-DD`」の行だけ（`digest.py`、Codex は使わない）。
  Obsidian で完了 `- [x]` にすれば出なくなる。手で書き足したタスクも同じ書き方なら拾う
- 毎朝・毎夕のブリーフは **Concierge**（別リポジトリ・別 bot, #concierge）が Recall のタスク・日記・アイデアを読んで作る（Concierge からは書かない）

## 取り消し（誤送信したとき）

- 自分の Discord の投稿を**削除する** → 記録からも消える
- bot の返信（または自分の投稿）に **🗑️ でリアクション** → 記録から消える。ショートカットの分もこれで消せる

消えるもの: raw の該当部分・種類ノートの行・保存した音声（Nextcloud のゴミ箱へ）・bot の返信。
種類ノートの各行の末尾にある `%%discord:…%%` は、この削除のための目印（Obsidian では表示されない）。

## スクリーンタイムの収集（Mac 側, `mac/`）

睡眠や1日の時間の使い方を後から判断する材料。Mac で launchd が1時間おき（と Mac が起きた時）に動かす。

- 元データ: Biome の `App.InFocus`。iPhone の分は iCloud 経由で Mac に同期されてくる（`remote/<端末ID>`、届くのは充電中などにまとめてで、1日くらい遅れる）。Mac 自身の分は `local`
- 項目の意味（実データで確認）: `3`=開いた1/閉じた0, `4`=時刻, `6`=アプリID, `1`=切り替えの理由（iPhone）。`com.apple.loginwindow` は Mac のロック中
- 前回からの新しい分だけを `~/Library/Application Support/recall-screentime/out/YYYY-MM-DD.jsonl`（JST の暦日）に足し、pk-206 の `~/recall-data/screentime/` へ rsync。送れなかった分は次の回にまとめて送る
- `_arrivals.jsonl` に「いつ・どの端末の・何時までの記録が届いたか」を残す（同期の遅れを見る）
- 入れ方: Mac で `mac/install.sh` を1回 → 表示された Python 本体にフルディスクアクセスを与える
- 確認: `python3 mac/collect_screentime.py --dump`（送らない・状態も進めない） / ログ `~/Library/Logs/recall-screentime.log`

## ファイル

| ファイル | 役割 |
|---|---|
| bot.py | Discord の入口。投稿 → raw → 仕分け → 返信 |
| voicelog.py | Inbox 処理の定数・関数（bot.py が使う）。bot 停止時の手動用（run.sh） |
| summarize.py | 日次の日記（`--date YYYY-MM-DD` で作り直し） |
| classify.py | 1件の仕分けと、種類ノートへの追記 |
| reclassify.py | raw の記録を今の分類でやり直す（`run_reclassify.sh 日付 [ID|--all]`） |
| digest.py | タスクのノートから期日の近いタスクを拾う（`!tasks`） |
| nc.py | WebDAV・raw 追記・ロックなどの共通部品 |
| ai.py | Codex CLI の呼び出し |
| transcribe.py | Whisper（bot からは別プロセスで呼ぶ。モデルを常駐させないため） |
| prompts/ | classify.md（仕分け）, tags.txt（タグ一覧）, summary.md（日記）, glossary.txt（用語集） |
| run*.sh | cron / systemd からの起動（env と nvm を読み込む） |
| deploy/ | post-receive フック、systemd ユニット |
| crontab.txt / env.example | pk-206 側の設定の控え |

## pk-206 側（リポジトリ外）

- `~/.config/recall/env`（600）: `env.example` 参照
- `~/whisper-env/`: faster-whisper, requests, discord.py の venv
- `~/.nvm/` + `@openai/codex`（`codex login --device-auth` 済み）
- `~/recall/recall.log`: 日記（cron）のログ / bot のログは `journalctl --user -u recall-bot -f`
- 文字起こしできなかった音声は `/VoiceLog/failed/` に退避される

## 更新のしかた

Mac の `~/dev/recall` で編集 → commit → `git push`（push 先 `pk` = `pk-206:repos/recall.git`）。
フックが `~/recall` に展開し、bot を再起動する。pk-206 上では直接編集しない。
GitHub（控え）へは `git push github main`。

## 今後

- 画像・URL の中身を読む（今はファイル名・URL文字列のみ記録）
- 仕分けの訂正（Discord で返信して種類を変える）
- 共有シート経由の音声は送信時刻がファイル名になる → m4a 埋め込みの録音日時を優先
- Codex を専用 Linux ユーザーで動かす
