#!/bin/bash
set -a
. /home/ikm/.config/voicelog/env
set +a
exec flock -n /tmp/voicelog.lock /home/ikm/whisper-env/bin/python /home/ikm/voicelog/voicelog.py
