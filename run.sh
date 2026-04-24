#!/bin/bash
set -e

cd /opt/vibe-projects/yt-saver-bot
git reset --hard
git pull

cd /opt/vibe-projects/deployment
docker compose stop yt-saver || true
docker compose rm -f yt-saver || true
docker stop yt-saver-bot-yt-saver-1 || true
docker rm yt-saver-bot-yt-saver-1 || true
docker stop yt-saver || true
docker rm yt-saver || true

docker compose up -d --build yt-saver
docker compose restart nginx
