import os

nginx_path = "/opt/vibe-projects/deployment/nginx/nginx.conf"
with open(nginx_path, "r") as f:
    config = f.read()

if "upstream yt-saver" not in config:
    config = config.replace("upstream main-site", "upstream yt-saver { server yt-saver:8000; }\n    upstream main-site")

if "location /yt/" not in config:
    insert_str = """
        location /yt/ {
            rewrite ^/yt/?(.*) /$1 break;
            proxy_pass http://yt-saver;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 86400;
            client_max_body_size 0;
        }
"""
    config = config.replace("        location /vless-ws {", insert_str + "\n        location /vless-ws {")

with open(nginx_path, "w") as f:
    f.write(config)

dc_path = "/opt/vibe-projects/deployment/docker-compose.yml"
with open(dc_path, "r") as f:
    dc = f.read()

if "yt-saver:" not in dc:
    dc_insert = """
  yt-saver:
    build: ../yt-saver-bot
    container_name: yt-saver
    restart: always
    networks:
      - vibe-network
"""
    dc = dc.replace("  main-site:", dc_insert + "\n  main-site:")

with open(dc_path, "w") as f:
    f.write(dc)

print("Config updated.")
