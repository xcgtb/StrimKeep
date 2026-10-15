"""Regression check for the two invisible mobile save labels. No browser required."""
from pathlib import Path
import re


def test_mobile_save_button_contrast():
    root = Path(__file__).resolve().parents[2]
    css = (root/'static/css/cloud-v2-layout.css').read_text(encoding='utf-8')
    workbench = (root/'static/js/cloud-workbench.js').read_text(encoding='utf-8')
    v2 = (root/'static/js/cloud-v2-layout.js').read_text(encoding='utf-8')
    for selector in (
        '#tab-cloud.cw-v2-active .cw-config-toolbar .cw-config-save',
        '#tab-cloud.cw-v2-active .cw-directory-field button[data-save-directories]',
    ):
        assert selector in css
    assert 'color:#fff;' in css and '-webkit-text-fill-color:#fff;' in css
    assert "save.textContent='保存修改'" in v2
    assert 'data-save-directories>保存修改</button>' in workbench
    # The old higher-specificity selector remains and must now be beaten explicitly.
    base = (root/'static/css/cloud-workbench.css').read_text(encoding='utf-8')
    assert '#tab-cloud .cw-config-save{font-weight:600;color:var(--brand)' in base
