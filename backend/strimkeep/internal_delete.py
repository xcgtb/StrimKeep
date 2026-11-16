"""Deep delete is stateless.

历史版本会在本地删除后写 7 天/10 分钟的「内部删除登记」，用来抑制 Emby 的延迟
webhook。但它也把后续的手动清理、重新入库一起挡住了。现在删除就是删除：
不写任何登记、不保留任何窗口。延迟 webhook 只会落到已经不存在的台账上，天然无操作。
"""
KEY = 'internal_deletes'   # 仅保留 key 名，供启动时清理历史遗留数据


def remember(store, paths, kind='managed'):
    """兼容旧调用：不再落任何登记。"""
    return True


def recent(store, window=None, kind=None):
    """兼容旧调用：永远没有「内部删除」记录。"""
    return set()


def forget(store, paths, kind=None):
    """兼容旧调用：没有登记可清。"""
    return 0

