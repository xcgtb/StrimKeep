# -*- coding: utf-8 -*-
"""Explicit legacy engine exports; runtime state remains owned by engine.

Package and standalone CLI imports use one deterministic route. Dependency
ImportError is propagated instead of retrying an unrelated top-level module.
Bind the original objects, including mutable caches, without copying/wrapping.
"""
from importlib import import_module

# (owner module, exported names, optional module alias). Order matches the
# previous engine imports; private names are intentionally part of this contract.
ENGINE_EXPORTS = (
    ('core',
     'esc parse_season_dir get_ep title_key governance_title_key analyze_season_episodes '
     'parse_emby_library quality_label RE_SXXEXX', None),
    ('logger',
     '', 'logger'),
    ('tg',
     'tg_title tg_row tg_stamp fmt_scan_text split_telegram_html notify_telegram', None),
    ('lib',
     'Lib _get_lib _invalidate_lib_cache _lib_cache _disk_tmdb_lookup _disk_eps_by_tmdb '
     '_match_governance_key _normalize_title _find_dir_fuzzy _under_tv_category', None),
    ('emby',
     'emby_request container_to_emby_path emby_path_to_container notify_emby_deleted notify_emby_refresh '
     'parse_dt _fetch_all_episodes _alive_dir_map _dir_has_media _paged_items _src _recent', None),
    ('tmdb',
     'TmdbError Tmdb _load_emby_index_disk _save_emby_index_disk _build_emby_library_index '
     '_emby_index_bg_refresh emby_library_index action_explore classify_series_by_tmdb '
     '_emby_series_ids_by_tmdb _emby_series_live_eps _tmdb_aired_set_from_info _tmdb_series_info', None),
    ('ingest',
     '_fetch_ingest refresh_ingest_cache _write_ingest_cache read_ingest_cache get_ingest action_stats '
     'action_played action_search action_logs', None),
    ('subscribe',
     '_load_sub_state _save_sub_state _subscription_report_file _save_subscription_report '
     '_load_subscription_report _emby_series_latest_ep _disk_series_eps _ep_key_num _ep_key _parse_ep_key '
     '_eps_to_keys _keys_to_eps _fmt_ep_ranges check_subscriptions load_subscription_check_status '
     'record_subscription_check_failure', None),
    ('wash',
     'cloud_videos _inside MutationBusy mutation_lock _is_sidecar_of _load_wash_residuals '
     '_save_wash_residuals _record_wash_residuals _remove_strm _classify_dir '
     '_has_confirmed_residual_in_dir _dir_cleanable _prune_up _unlink_with_timeout safe_delete_files '
     'find_movie_strms_by_tmdb _clamp_depth scan_orphans _confirmed_wash_residuals _parse_residual_ts '
     'scan_orphan_dirs clean_orphan_dirs _clean_orphan_dirs action_scan_orphans action_clean_orphan_dirs '
     'purge_old scan_empty_dirs clean_empty_dirs', None),
    ('governance',
     '_strategy _exempt_keywords _ep _best _exempt_hit _nat_key _split_root _exempt_group_of _share_wins '
     '_compare_meta _season_compare _side_gap_desc Act _media_title_folder _tmdb_ids_from_files '
     '_governance_evidence _attach_governance_evidence _act_to_dict _group_exempt_acts '
     '_ingest_quiet_minutes _QuietGate _identity_conflicts _build_plan_with_libs build_plan '
     '_dedupe_lib_versions _emit_season_act _emit_season_acts_full _current_rule_snapshot save_plan '
     'load_plan save_plan_state save_latest_scan load_latest_scan action_inter_check action_inter_clean '
     '_action_inter_clean_locked _confirmed_files _run_inter_clean write_audit_log', None),
    ('morning',
     '_save_overview_disk _load_overview_disk _overview_bg_refresh emby_library_overview '
     '_build_emby_library_overview read_manual_done _apply_manual_done_to_series '
     'build_library_health_snapshot save_library_snapshot load_library_snapshot '
     'refresh_library_snapshot_background unified_health cached_library_view daily_consistency_snapshot '
     'gap_report action_emby_library build_morning_report send_morning_report _live_series_episodes '
     '_resync_series_entry _patch_all_caches refresh_mapping_cache_after_ingest '
     'patch_emby_lib_cache_after_series_delete patch_emby_lib_cache_after_movie_delete save_emby_lib_cache '
     'read_emby_lib_cache get_tmdb_scan_progress refresh_tmdb_scan _refresh_tmdb_scan scan_exempt_matches', None),
    ('stats',
     '_recompute_all_stats action_library_stats _load_strm_count_disk _save_strm_count_disk '
     '_strm_count_bg_refresh _get_strm_counts invalidate_stats_cache invalidate_media_caches', None),
    ('storage',
     'db_save_plan db_load_plan db_save_plan_state db_list_plans db_delete_plan db_purge_plans '
     'db_sync_plans_from_disk db_add_audit db_recent_audit db_get_audit db_media_index_load '
     'db_media_index_upsert db_media_index_delete_missing db_media_index_stats db_load_sub_state '
     'db_save_sub_state db_clear_sub_state db_kv_get db_kv_set db_dedup_add db_dedup_seen db_migrate '
     'db_doc_get db_doc_set db_doc_mirror_file', None),
)


def bind_engine_exports(namespace, package):
    """Publish only the allowlisted aliases after all imports resolve."""
    exports = {}
    for owner, names, alias in ENGINE_EXPORTS:
        module = import_module('.' + owner, package) if package else import_module(owner)
        if alias:
            exports[alias] = module
        else:
            for name in names.split():
                if name in exports:
                    raise ValueError('duplicate engine export: ' + name)
                exports[name] = getattr(module, name)
    namespace.update(exports)
