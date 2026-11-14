"""Master control for StrimKeep's optional media-info pipeline."""


def media_info_enabled(store):
    # Existing installations keep their previous extraction and cron behavior.
    return bool(store.get('media_info_master_enabled', True))
