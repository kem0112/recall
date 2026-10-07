#!/bin/bash
# cron（毎朝4:15）: 終わった日の日記を作る。--date YYYY-MM-DD で手動実行も可
set -a
. /home/ikm/.config/recall/env
set +a
export NVM_DIR="/home/ikm/.nvm"
. "$NVM_DIR/nvm.sh"
cd /home/ikm/recall
exec flock -n /tmp/recall-summary.lock /home/ikm/whisper-env/bin/python /home/ikm/recall/summarize.py "$@"
