#!/bin/bash
# cron（1時間おき）: iPhone のスクリーンタイムが新しく届いていたら睡眠を判定する。--force / --dry-run も可
set -a
. /home/ikm/.config/recall/env
set +a
export NVM_DIR="/home/ikm/.nvm"
. "$NVM_DIR/nvm.sh"
cd /home/ikm/recall
exec flock -n /tmp/recall-sleep.lock /home/ikm/whisper-env/bin/python /home/ikm/recall/sleep.py "$@"
