#!/usr/bin/env bash
# Isolated real-Emby P1 regression on NAS. Does not modify production containers.
set -euo pipefail
SOURCE=${1:-/vol1/1000/git/StrimKeep-P0-P1-test}
IMAGE=strimkeep:p1-acceptance
cd "$SOURCE"
test -f Dockerfile
test -f dev/checks/verify_p1_nas_readonly.py

echo '=== Build isolated P1 image ==='
docker build -t "$IMAGE" .

echo '=== Live container-mounted media directories (read-only in test) ==='
LOCAL=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/media/local"}}{{.Source}}{{end}}{{end}}' strimkeep)
SHARE=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/media/share"}}{{.Source}}{{end}}{{end}}' strimkeep)
test -d "$LOCAL" && test -d "$SHARE"
printf 'Local media mount: present\nShare media mount: present\n'

echo '=== Isolated HTTP + SSE auth/event test, no credentials ==='
docker run --rm --network none --read-only --tmpfs /tmp:rw,size=128m \
  --memory 512m --pids-limit 80 \
  -v "$SOURCE/dev:/app/dev:ro" \
  --entrypoint python "$IMAGE" /app/dev/checks/verify_p1_stream_http.py

echo '=== Real Emby P1 Series+Movie/update/failure/incremental test ==='
# Secrets stay inside a pipe to the temporary process, never command parameters or output.
docker exec strimkeep python -c \
 'from app import engine; import json; print(json.dumps({"host": engine.EMBY_HOST, "key": engine.EMBY_KEY, "local": engine.EMBY_PATHS.local, "share": engine.EMBY_PATHS.share}))' \
| docker run --rm -i --network host --read-only \
    --tmpfs /data:rw,size=256m --tmpfs /tmp:rw,size=128m \
    --memory 768m --pids-limit 80 \
    -v "$SOURCE/dev:/app/dev:ro" \
    -v "$LOCAL:/media/local:ro" \
    -v "$SHARE:/media/share:ro" \
    --entrypoint python "$IMAGE" /app/dev/checks/verify_p1_nas_readonly.py

echo '=== Production container unchanged ==='
docker inspect -f '{{.Config.Image}} | {{.State.Health.Status}}' strimkeep
