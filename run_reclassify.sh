#!/bin/bash
# 手動用: raw の記録を今の分類でやり直す（使い方は reclassify.py の先頭）
set -a
. /home/ikm/.config/recall/env
set +a
export NVM_DIR="/home/ikm/.nvm"
. "$NVM_DIR/nvm.sh"
cd /home/ikm/recall
exec flock -n /tmp/recall-reclassify.lock /home/ikm/whisper-env/bin/python /home/ikm/recall/reclassify.py "$@"
