#!/bin/sh
# 发布前回归门禁：只用实测通过的组合，任何一组失败立即停止。
#
# 用法：
#   sh scripts/verify.sh                      # 镜像取自 docker-compose.yml
#   sh scripts/verify.sh strimkeep:0.7.66     # 指定镜像
#
# 为什么分组而不是一次全跑：NAS 内存有限，两个测试容器并行会 OOM（退出码 137）。
# 组间留间隔让上一个容器的内存释放干净；每组开始前先清掉遗留的 sk_test。
#
# 当前按功能分组。组名与文件清单必须和 tests/ 目录一致：新增或删除测试文件时同步这里，
# 否则会像 0.8.2 那样因为「清单里的文件不存在」让门禁整组直接失败
# （详见 docs/quality/known-issues.md）。
# tests/test_transfer.py 与 tests/test_upgrade.py 已在「转存与洗版」组内：
# 它们历史上曾在容器里挂住（test_incomplete_receipt_is_reported_as_accepted_not_lost
# 之后的状态泄漏），现在由本脚本的单组硬超时（GROUP_TIMEOUT）兜住，不再单独排除。
#
# 本脚本会 `docker rm -f sk_test` 并清空 sk_tdata / sk_tstrm 两个测试专用卷，
# 不要在还有别的测试任务在跑时执行。

set -u

PROJECT="${STRIMKEEP_HOME:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
cd "$PROJECT" || exit 1

IMAGE="${1:-}"
if [ -z "$IMAGE" ]; then
    # compose 里还有 emby 侧车，"第一行 image:" 不一定是本项目；按名字挑 strimkeep 那一行。
    IMAGE=$(grep -m1 -E '^ *image: *[^ ]*strimkeep' docker-compose.yml | awk '{print $2}')
fi
if [ -z "$IMAGE" ]; then
    echo '无法从 docker-compose.yml 判断本项目镜像，请显式传入：sh scripts/verify.sh strimkeep:<版本>' >&2
    exit 2
fi

# 组间间隔；机器紧张时可以调大。
GAP="${VERIFY_GAP:-30}"
# 单组硬超时：门禁的第一职责是「总会给出结论」，其次才是「结论准确」。
# 没有超时的话，某组一旦卡住，整个门禁就永远不返回，等于门禁自身失效。
GROUP_TIMEOUT="${VERIFY_TIMEOUT:-900}"

run_group() {
    name="$1"
    shift
    printf '\n=== %s ===\n' "$name"
    # 上一个测试容器必须先清掉：两个容器并行抢内存，表现为退出码 137。
    docker rm -f sk_test >/dev/null 2>&1
    timeout "$GROUP_TIMEOUT" sh scripts/test_in_container.sh "$IMAGE" "$@"
    status=$?
    # 124 = timeout 杀掉的：明确记为「卡住」，不要和断言失败混为一谈。
    if [ "$status" -eq 124 ]; then
        printf '!! 超时：%s 超过 %s 秒未结束（已终止，避免门禁整体卡死）\n' "$name" "$GROUP_TIMEOUT"
        docker rm -f sk_test >/dev/null 2>&1
        return 1
    fi
    if [ "$status" -ne 0 ]; then
        printf '!! 失败：%s（退出码 %s）\n' "$name" "$status"
        return 1
    fi
    printf 'OK %s\n' "$name"
    sleep "$GAP"
    return 0
}

# 清空测试专用卷：sk_tdata / sk_tstrm 是跨轮共享的，上一轮中断残留会让个别用例
# 变慢甚至卡住（表现为偶发挂死）。这些卷只服务测试，清空无副作用。
clean_test_volumes() {
    docker rm -f sk_test >/dev/null 2>&1
    docker run --rm -v sk_tdata:/data -v sk_tstrm:/strm --entrypoint sh "$IMAGE" \
        -c 'rm -rf /data/* /data/.[!.]* /strm/* /strm/.[!.]* 2>/dev/null; exit 0' >/dev/null 2>&1
}

printf '镜像: %s\n' "$IMAGE"
printf '项目: %s\n' "$PROJECT"

clean_test_volumes

run_group "核心流水线" tests/test_iteration1.py tests/test_iteration2.py tests/test_deep_delete.py tests/test_core_contract.py tests/test_version_consistency.py tests/test_app.py tests/test_jobs_repo.py || exit 1
run_group "归档与写入" tests/test_archive.py tests/test_console060.py || exit 1
run_group "同步与巡检" tests/test_sync_notice.py tests/test_sync_sweep.py tests/test_sync_orphans.py tests/test_sync_stale.py tests/test_sync_trigger_parity.py tests/test_sync_isolation.py tests/test_sync_cleanup_revision.py tests/test_sync_subtitle_adoption.py || exit 1
run_group "识别与命名" tests/test_organize.py tests/test_audio_representative.py tests/test_review_trace.py tests/test_multilang_identify.py tests/test_identify_alignment.py tests/test_tv_year_policy.py tests/test_unique_fallback.py tests/test_numeral_fallback.py tests/test_region_classification.py tests/test_scan_scope_authority.py || exit 1
run_group "身份与上下文" tests/test_identity_real_names.py tests/test_folder_context.py tests/test_play85.py tests/test_flow058.py || exit 1
run_group "未识别与字幕清理" tests/test_unrecognized063.py tests/test_empty_directories.py tests/test_cleanup_candidates.py tests/test_subtitle_release_names.py tests/test_duplicate_subtitles.py tests/test_merged_episode_copy.py tests/test_recycle_readback.py || exit 1
run_group "通知卡片" tests/test_cards062.py tests/test_message_templates.py tests/test_tg_notification_flow.py tests/test_telegram_error_redaction.py || exit 1
run_group "媒体信息" tests/test_native_media_info.py tests/test_share_task_details.py tests/test_phase2_share_pipeline.py tests/test_share_parent_hint.py tests/test_share_unidentified_layout.py tests/test_share_cleanup_async.py tests/test_share_schedule.py tests/test_emby_delivery_states.py tests/test_emby_prewarm.py tests/test_emby_scan_fallback.py tests/test_emby061.py || exit 1
run_group "转存与洗版" tests/test_transfer.py tests/test_upgrade.py || exit 1
run_group "离线与探索" tests/test_offline_throttle.py tests/test_explore_badge.py tests/test_sweep_offline.py tests/test_channel_skip_offline.py || exit 1
run_group "自检与计数" tests/test_ops0819.py tests/test_ops0820.py tests/test_ops0821.py tests/test_ops0822.py tests/test_ops0830.py tests/test_ops0831.py tests/test_ops0832.py tests/test_ops0833.py tests/test_ops0837.py tests/test_ops0838.py tests/test_ops0839.py tests/test_ops0840.py tests/test_ops0841.py || exit 1
run_group "反代与媒体信息" tests/test_emby_proxy.py tests/test_media_inject.py tests/test_orphan_registry.py tests/test_housekeeping.py tests/test_output_reconcile.py tests/test_media_info_log.py tests/test_media_info_fallback.py || exit 1
run_group "库存与治理" tests/test_inventory.py tests/test_inventory_audit.py tests/test_inventory_stale.py tests/test_inventory_rules.py tests/test_inventory_preview.py tests/test_inventory_operations.py tests/test_inventory_operations_api.py tests/test_inventory_sources.py tests/test_inventory_snapshot.py tests/test_inventory_plans.py tests/test_inventory_executor.py tests/test_inventory_delete_adapter.py tests/test_inventory_job_gate.py tests/test_inventory_orphans.py tests/test_inventory_mapping.py tests/test_governance_parity_1212.py || exit 1

printf '\n全部通过：%s\n' "$IMAGE"
exit 0
