#!/bin/bash
# systemd（recall-bot.service）から起動する Discord bot
set -a
. /home/ikm/.config/recall/env
set +a
export NVM_DIR="/home/ikm/.nvm"
. "$NVM_DIR/nvm.sh"
cd /home/ikm/recall
exec /home/ikm/whisper-env/bin/python -u /home/ikm/recall/bot.py
