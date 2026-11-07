#!/bin/sh
set -e
# Sites change often: keep yt-dlp current on each start (disable with YTDLP_AUTO_UPDATE=false).
if [ "$YTDLP_AUTO_UPDATE" = "true" ]; then
  pip install --no-cache-dir -q -U "yt-dlp[default]" || echo "yt-dlp update failed, keeping bundled version"
fi
# Optional cookies (Netscape format) for sites that require a login or block bots.
if [ -n "$YTDLP_COOKIES" ]; then
  printf '%s\n' "$YTDLP_COOKIES" > /data/cookies.txt
  export YTDLP_COOKIES_FILE=/data/cookies.txt
fi
# One worker process (jobs live in memory), several threads.
exec gunicorn -w 1 --threads 8 -b "0.0.0.0:${PORT}" --timeout 120 app:app
