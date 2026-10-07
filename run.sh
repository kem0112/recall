#!/bin/bash
# cron（10分おき）: ショートカット経由の音声を処理する
set -a
. /home/ikm/.config/recall/env
set +a
export NVM_DIR="/home/ikm/.nvm"
. "$NVM_DIR/nvm.sh"
cd /home/ikm/recall
exec flock -n /tmp/recall-voicelog.lock /home/ikm/whisper-env/bin/python /home/ikm/recall/voicelog.py
