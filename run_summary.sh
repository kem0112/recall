#!/bin/bash
set -a
. /home/ikm/.config/voicelog/env
set +a
export NVM_DIR="/home/ikm/.nvm"
. "$NVM_DIR/nvm.sh"
cd /home/ikm/voicelog
exec flock -n /tmp/voicelog-summary.lock /home/ikm/whisper-env/bin/python /home/ikm/voicelog/summarize.py "$@"
