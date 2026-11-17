"""端口（Protocol）：外部系统的最小契约。

只定义业务层真正需要的方法。具体实现（115 / Emby / Telegram / Webhook）
留在各自 adapter 里；这一层不引入依赖、不做注册表，避免为了抽象而抽象。
"""
from typing import Any, Mapping, Optional, Protocol


class CloudDrive(Protocol):
    """网盘能力集合。不是所有实现都支持全部能力，用 capabilities 声明。"""
    name: str

    def capabilities(self) -> Mapping[str, bool]: ...

    async def list(self, parent_id: str) -> list[dict[str, Any]]: ...

    async def file(self, file_id: str) -> dict[str, Any]: ...

    async def url(self, file_id: str, user_agent: str) -> tuple[str, Any]: ...

    async def live_file_exists(self, file_id: str) -> bool: ...


class MediaServer(Protocol):
    """媒体服务器最小接口；先包 Emby，后续 Jellyfin 只加 adapter。"""
    name: str

    async def test(self) -> tuple[bool, str]: ...

    async def scan(self, paths: list[str]) -> None: ...

    async def items(self, query: Mapping[str, Any]) -> list[dict[str, Any]]: ...

    async def delete(self, item_id: str) -> bool: ...


class Notifier(Protocol):
    """通知通道：title/message 是统一 DTO，附件由具体通道决定是否使用。"""
    name: str

    async def send(self, title: str, message: str,
                   attachments: Optional[list[str]] = None) -> tuple[str, str]: ...
