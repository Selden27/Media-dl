FROM python:3.13-slim

# ffmpeg: audio extraction / merging. fpcalc (libchromaprint-tools): AcoustID fingerprints. deno: JS runtime yt-dlp needs for YouTube.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libchromaprint-tools ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh && useradd -m -u 1000 app && mkdir -p /data/tmp && chown -R app /data /srv \
    && chown -R app /usr/local/lib/python3.13/site-packages /usr/local/bin

USER app
ENV PORT=8000 WORK_DIR=/data/tmp YTDLP_AUTO_UPDATE=true PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health')"
ENTRYPOINT ["./entrypoint.sh"]
