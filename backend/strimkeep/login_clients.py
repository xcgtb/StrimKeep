"""Explicit client identities supported by the pinned p115client SDK."""
LOGIN_CLIENTS = (
    {'value': 'os_windows', 'label': '115生活 · Windows', 'group': '桌面端'},
    {'value': 'os_mac', 'label': '115生活 · macOS', 'group': '桌面端'},
    {'value': 'os_linux', 'label': '115生活 · Linux', 'group': '桌面端'},
    {'value': 'web', 'label': '115生活 · 网页端', 'group': '桌面端'},
    {'value': 'android', 'label': '115生活 · Android', 'group': '手机和平板'},
    {'value': 'ios', 'label': '115生活 · iOS', 'group': '手机和平板'},
    {'value': 'ipad', 'label': '115生活 · iPad', 'group': '手机和平板'},
    {'value': '115android', 'label': '115 · Android', 'group': '手机和平板'},
    {'value': '115ios', 'label': '115 · iOS', 'group': '手机和平板'},
    {'value': '115ipad', 'label': '115 · iPad', 'group': '手机和平板'},
    {'value': 'harmony', 'label': '115 · 鸿蒙', 'group': '手机和平板'},
    {'value': 'qandroid', 'label': '115管理 · Android', 'group': '115管理'},
    {'value': 'qios', 'label': '115管理 · iOS', 'group': '115管理'},
    {'value': 'qipad', 'label': '115管理 · iPad', 'group': '115管理'},
    {'value': 'tv', 'label': '115生活 · Android TV', 'group': '电视和小程序'},
    {'value': 'apple_tv', 'label': '115生活 · Apple TV', 'group': '电视和小程序'},
    {'value': 'alipaymini', 'label': '115生活 · 支付宝小程序', 'group': '电视和小程序'},
    {'value': 'wechatmini', 'label': '115生活 · 微信小程序', 'group': '电视和小程序'},
)
CLIENT_LABELS = {item['value']: item['label'] for item in LOGIN_CLIENTS}
