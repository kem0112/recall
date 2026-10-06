# voicelog

iPhoneで録った音声メモを、pk-206で自動で文字起こし・要約して、Obsidian（ob）に入れる仕組み。

## 全体の流れ

```
iPhone ショートカット「Voice log」（録音 or 共有シート）
  → WebDAV直送（Nextcloudのファイルリクエスト共有）→ /VoiceLog/Inbox/YYYY-MM-DD_HHMMSS.m4a
  → [10分おき] run.sh → voicelog.py（faster-whisper large-v3-turbo）
      ├ /ob/ボイスログ/raw/YYYY-MM-DD.md に時刻見出し付きで追記
      └ 音声を /VoiceLog/audio/YYYY-MM/ へ移動
  → [毎朝4:15] run_summary.sh → summarize.py（Codex CLI, read-only）
      └ /ob/ボイスログ/YYYY-MM-DD.md（要約・できごと・気づき・TODO・清書）
```

- 日付の区切りは朝4時（`DAY_START_HOUR`）。深夜の録音は前日扱い。
- Nextcloudへの読み書きはすべてWebDAV経由（直接ファイルを置くとNextcloudのDBに載らず同期されないため）。
- Codexには資格情報を渡さない。rawをテキストで渡し、返ってきた要約をPython側が書き込む。

## ファイル

| ファイル | 役割 |
|---|---|
| voicelog.py | Inbox監視 → 文字起こし → raw追記 → 音声アーカイブ |
| summarize.py | 終わった日のrawを要約（`--date YYYY-MM-DD` で強制実行） |
| prompt.md | Codexへの指示文（出力フォーマット） |
| glossary.txt | 誤変換を直すための用語集。よく話す単語を足していく |
| run.sh / run_summary.sh | cronから呼ぶ起動スクリプト（env読み込み・二重起動防止） |
| crontab.txt | pk-206に登録しているcronの控え |
| deploy/post-receive | push時に ~/voicelog へ展開するgitフック |
| tools/test_whisper.py | モデル比較用 |

## pk-206 側（リポジトリ外）

- `~/.config/voicelog/env`（600）: `NC_USER` / `NC_PASS`（Nextcloudアプリパスワード `pk-206-voicelog`）
- `~/whisper-env/`: faster-whisper, requests の venv
- `~/.nvm/`: Node LTS + `@openai/codex`（`codex login --device-auth` でChatGPTログイン済み）
- `~/voicelog/voicelog.log`: 実行ログ

## 更新のしかた

Macの `~/voicelog` で編集 → commit → `git push pk main`。
pk-206の post-receive フックが `~/voicelog` に自動で展開する。pk-206上では直接編集しない。

## 今後

- 共有シート経由のファイルは送信時刻がファイル名になる → m4a埋め込みの録音日時（creation_time）を優先する
- Codexを専用Linuxユーザーで動かして読み取り範囲も隔離する
- Discordからの会話窓口
