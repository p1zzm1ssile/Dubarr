# Building from vanilla alpine base, as the official Python alpine image lags behind on security fixes
FROM alpine:latest

LABEL author="p1zzm1ssile"
LABEL org.opencontainers.image.source="https://github.com/p1zzm1ssile/Dubarr"
LABEL org.opencontainers.image.description='Dubarr: removes audio and subtitle tracks in languages you do not want from your movies and TV, and flags files with none in your language. Works with Radarr, Sonarr and Jellyfin.'

WORKDIR /app

# ffprobe reads the tracks, mkvmerge (MKV) and ffmpeg (MP4) write the cleaned copy.
# Nothing is re-encoded, so no GPU is needed.
RUN apk add --no-cache python3 py3-pip ffmpeg mkvtoolnix
COPY ./src/requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r /app/requirements.txt
COPY ./src .

# Settings, the web UI login, the database and logs live here. Map it to appdata to keep them.
ENV CONFIG_DIR=/config
RUN mkdir -p /config
VOLUME /config
EXPOSE 9892

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD ["python3", "healthcheck.py"]
CMD ["python3", "main.py"]
