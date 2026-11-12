#!/bin/sh
# 在 NAS 上用真实镜像跑测试目标集（一条命令，跑完自动清理容器）。
#
# 用法：
#   sh scripts/test_in_container.sh                 # 默认目标集
#   sh scripts/test_in_container.sh 0.7.57          # 指定镜像
#   sh scripts/test_in_container.sh "" tests/test_deep_delete.py   # 指定镜像 + 指定测试
#
# 注意：
# - 必须在项目根目录执行；镜像默认取 docker-compose.yml 里的 image:
# - pytest 装在 sk_pylibs 卷里；首次或卷被删后跑一次：
#   docker run --rm -v sk_pylibs:/opt/pylibs <镜像> pip install -q -t /opt/pylibs pytest
# - 只挂仓库读文件：测试需要 docker-compose.yml / scripts / docs，必须挂整个项目
# - 追加 pytest 参数用环境变量，例如定位挂住的用例：
#   PYTEST_EXTRA="-v" sh scripts/test_in_container.sh strimkeep:0.7.65 tests/test_transfer.py

PROJECT="${STRIMKEEP_HOME:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
IMAGE="$1"
[ -n "$IMAGE" ] || IMAGE=$(grep -m1 '^ *image:' "$PROJECT/docker-compose.yml" | awk '{print $2}')
shift 2>/dev/null
TESTS="$*"
if [ -z "$TESTS" ]; then
  TESTS="tests/test_iteration1.py tests/test_iteration2.py tests/test_deep_delete.py
   tests/test_app.py tests/test_organize.py tests/test_transfer.py tests/test_upgrade.py
   tests/test_cleanup_candidates.py tests/test_empty_directories.py tests/test_recycle_readback.py
   tests/test_recycle_small_videos.py tests/test_emby_delivery_states.py
   tests/test_emby_scan_fallback.py tests/test_children_cache.py tests/test_cover_service.py
   tests/test_native_media_info.py tests/test_sweep_offline.py tests/test_phase5.py"
fi

echo "镜像  : $IMAGE"
echo "项目  : $PROJECT"
echo "用例  : $(echo $TESTS | wc -w) 个文件"

docker rm -f sk_test >/dev/null 2>&1
for vol in sk_pylibs sk_tdata sk_tstrm; do docker volume create "$vol" >/dev/null 2>&1; done

docker run -d --name sk_test \
  -e PYTHONPATH=/app/repo/backend:/opt/pylibs \
  -e STRIMKEEP_DATA_DIR=/data -e STRIMKEEP_STRM_DIR=/strm \
  -v "$PROJECT:/app/repo" -v sk_pylibs:/opt/pylibs \
  -v sk_tdata:/data -v sk_tstrm:/strm \
  -w /app/repo/backend --entrypoint sleep "$IMAGE" infinity >/dev/null || exit 1

docker exec sk_test python -m pytest $TESTS -q --no-header -p no:cacheprovider --tb=line ${PYTEST_EXTRA:-}
STATUS=$?
docker rm -f sk_test >/dev/null 2>&1
echo "退出码: $STATUS"
exit $STATUS
