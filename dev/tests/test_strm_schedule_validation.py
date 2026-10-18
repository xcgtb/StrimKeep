"""Reject bad scan settings before they can trigger repeated scheduled writes."""
import copy
import pytest
from app.cloud import settings
from app.cloud.errors import CloudError


@pytest.mark.parametrize('patch', [
    {'strm_scan_cron': 'not a cron'},
    {'strm_scan_cron': 123},
    {'strm_scan_cron': '0 0 31 2 *'},
    {'strm_scan_timezone': 'invalid/timezone'},
    {'strm_block_feiniu_ua': 'false'},
    {'strm_scan_last_run': -1},
    {'strm_scan_last_run': float('nan')},
])
def test_invalid_scan_settings_are_rejected(patch):
    with pytest.raises(CloudError):
        settings.validate({**copy.deepcopy(settings.DEFAULTS), **patch})


def test_valid_schedule_and_opt_in_probe_block():
    value = settings.validate(copy.deepcopy(settings.DEFAULTS))
    assert value['strm_block_feiniu_ua'] is False
    value.update(strm_scan_cron='0 0 * * *', strm_block_feiniu_ua=True)
    assert settings.validate(value)['strm_scan_cron'] == '0 0 * * *'
