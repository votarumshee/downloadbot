FROM node:24-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 downloadbot
WORKDIR /app
COPY pyproject.toml constraints.txt ./
COPY src ./src
RUN pip install --no-cache-dir -c constraints.txt . \
    && mkdir -p /data && chown downloadbot:downloadbot /data
USER downloadbot
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DOWNLOADBOT_DATA_DIR=/data
EXPOSE 8000
CMD ["python", "-m", "downloadbot", "api", "--host", "0.0.0.0"]
