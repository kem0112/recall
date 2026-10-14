#!/bin/bash
# スクリーンタイム収集係を launchd に登録する（Mac で1回だけ実行）。やり直しても大丈夫。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.kem0112.recall-screentime
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(python3 -c 'import os,sys;print(os.path.realpath(sys.executable))')"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$HERE/collect_screentime.py</string></array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/recall-screentime.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/recall-screentime.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "登録しました: $LABEL（1時間おき・Mac が起きたときにも）"
echo
echo "★ フルディスクアクセスをこの Python 本体に与えてください（1回だけ）:"
echo "   $PY"
echo "   システム設定 > プライバシーとセキュリティ > フルディスクアクセス > ＋ で上のファイルを追加"
echo "   （Finder で ⌘⇧G に貼り付けると選べます）"
echo
echo "ログ: tail -f ~/Library/Logs/recall-screentime.log"
echo "今すぐ動かす: launchctl kickstart gui/$(id -u)/$LABEL"
echo
if ssh -o BatchMode=yes -o ConnectTimeout=10 pk-206 true 2>/dev/null; then
  echo "pk-206 へのパスワードなし接続: OK"
else
  echo "⚠️ pk-206 へパスワードなしで繋がりません（鍵にパスフレーズがある場合は ssh-add --apple-use-keychain で登録してください）"
fi
