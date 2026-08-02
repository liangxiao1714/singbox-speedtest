"""订阅 HTTP 下载器替身。

:singbox_speedtest:`singbox_speedtest.py` 当前版本直接通过系统 ``curl`` 子进程下载
订阅（Phase 2 / Phase 5 会替换为内置 downloader 模块）。本替身提供一个稳定的、
不触网的下载接口，便于 Phase 0+ 提前编写订阅拉取相关的测试。

接口约定 (返回 :class:`FakeResponse` 或抛 ``TimeoutError`` / ``ConnectionError``)::

    resp = download(url, scenario="ok")
    resp.status_code   # int
    resp.text          # str（解码后的正文）
    resp.content       # bytes（原始正文，优先）
    resp.headers       # dict[str, str]（小写键）
    resp.elapsed       # float（秒，下载耗时模拟值）
    resp.final_url     # str（重定向后的最终 URL）

覆盖的 scenario（与文档 16.9 对齐）：

- ``ok``                : 200，小尺寸合法多 URI 正文
- ``http_error``        : 403/451，HTML 错误页
- ``redirect_to_private``: 200，但 ``final_url`` 指向私网/回环 IP（用于 SSRF 断言）
- ``timeout``           : 抛 ``TimeoutError``
- ``oversized``         : 200，正文大于可配置 max（默认 10 MiB 重复字节）
- ``gzip_bomb``         : 200，小 gzipped payload，``content-encoding: gzip``，
                          解压后远超原始体积
- ``interrupted``       : 抛 ``ConnectionError``

所有 URL/IP/域名均为文档/保留地址 (RFC 5737 / RFC 3849 / loopback)，绝无真实外网请求。
"""

from __future__ import annotations

import gzip
import io
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Union

__all__ = [
    "download",
    "FakeResponse",
    "SCENARIOS",
    "DEFAULT_MAX_BYTES",
]

# Scenario 名单（与文档 16.9 对齐）。导出供调用方做覆盖率自检。
SCENARIOS = (
    "ok",
    "http_error",
    "redirect_to_private",
    "timeout",
    "oversized",
    "gzip_bomb",
    "interrupted",
)

# oversized 场景的默认大小（10 MiB，与 DEFAULT_DL_BYTES 同量级）
DEFAULT_MAX_BYTES = 10 * 1024 * 1024

# 文档保留地址（避免误触真实外网）
_LOOPBACK_URL = "http://127.0.0.1/sub"
_PRIVATE_URL_10 = "http://10.0.0.1/sub"
_PRIVATE_URL_192 = "http://192.168.1.1/sub"


@dataclass
class FakeResponse:
    """轻量响应数据类，模仿 ``requests`` / ``httpx`` 的核心只读字段。

    设计取舍：只暴露 Phase 0~5 真正会用到的字段，避免一次性引入过多耦合。
    ``content`` 为权威正文（bytes），``text`` 在访问时按 utf-8 解码产生（容错）。
    """

    status_code: int
    content: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)
    elapsed: float = 0.0
    final_url: str = ""

    @property
    def text(self) -> str:
        """以 utf-8 容错解码 ``content`` 为字符串。

        用 ``errors="replace"``，确保 gzip 残片 / 二进制不会在测试里抛
        UnicodeDecodeError 掩盖真正断言。
        """
        if isinstance(self.content, str):  # 防御：直接传入了 str
            return self.content
        return self.content.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        """与 requests 一致的便利字段：2xx 视为成功。"""
        return 200 <= self.status_code < 300


# ---- 各 scenario 的工厂 ----------------------------------------------------

def _make_ok(url: str) -> FakeResponse:
    # 小尺寸合法多 URI 正文（极简 vless + vmess + 一个 # 注释行）
    body = (
        "vless://00000000-0000-0000-0000-000000000000@server.example.com:443?encryption=none&type=tcp#fake-vless\n"
        "vmess://eyJ2IjoiMiIsInBzIjoiZmFrZS12bWVzcyJ9\n"  # base64 of {"v":"2","ps":"fake-vmess"}
        "# comment line should be skipped by parser\n"
    ).encode("utf-8")
    return FakeResponse(
        status_code=200,
        content=body,
        headers={
            "content-type": "text/plain; charset=utf-8",
            "subscription-userinfo": "upload=0; download=0; total=10737418240; expire=0",
        },
        elapsed=0.15,
        final_url=url or _LOOPBACK_URL,
    )


def _make_http_error(url: str) -> FakeResponse:
    body = (
        "<!doctype html><html><head><title>403 Forbidden</title></head>"
        "<body><h1>403 Forbidden</h1><p>Access to this resource is blocked.</p></body></html>"
    ).encode("utf-8")
    return FakeResponse(
        status_code=403,
        content=body,
        headers={"content-type": "text/html; charset=utf-8"},
        elapsed=0.08,
        final_url=url or _LOOPBACK_URL,
    )


def _make_redirect_to_private(url: str) -> FakeResponse:
    # 模拟：最终落点 IP 为 10.0.0.1（私网），用于 SSRF 守卫单测
    body = (
        "vless://00000000-0000-0000-0000-000000000000@internal.example.com:443?type=tcp#fake\n"
    ).encode("utf-8")
    return FakeResponse(
        status_code=200,
        content=body,
        headers={
            "content-type": "text/plain; charset=utf-8",
            "location": _PRIVATE_URL_10,  # 兼容个别调用方读取 location 头
        },
        elapsed=0.20,
        final_url=_PRIVATE_URL_10,
    )


def _make_oversized(url: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> FakeResponse:
    # 重复单字节生成超过阈值的 body（不必恰好等于阈值；保证 > max_bytes 即可）
    # 多生成 1 KiB 以便断言 "strictly greater than max"。
    content = (b"A" * 1024) * (max_bytes // 1024 + 1)
    return FakeResponse(
        status_code=200,
        content=content,
        headers={
            "content-type": "application/octet-stream",
            "content-length": str(len(content)),
        },
        elapsed=2.5,
        final_url=url or _LOOPBACK_URL,
    )


def _make_gzip_bomb(url: str) -> FakeResponse:
    # 把一段大重复文本压缩成小 payload；调用方在 Phase 2/5 自行按
    # content-encoding 解压，本替身只负责"投递小 gzipped 字节 + 头提示"
    raw_text = ("vless://fake@example.com:443?type=tcp#node\n" * 4096)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(raw_text.encode("utf-8"))
    compressed = buf.getvalue()
    return FakeResponse(
        status_code=200,
        content=compressed,
        headers={
            "content-type": "application/octet-stream",
            "content-encoding": "gzip",
            "content-length": str(len(compressed)),
        },
        elapsed=0.30,
        final_url=url or _LOOPBACK_URL,
    )


def _raise_timeout(url: str):
    raise TimeoutError(f"fake_downloader: timed out fetching {url or _LOOPBACK_URL}")


def _raise_interrupted(url: str):
    raise ConnectionError(
        f"fake_downloader: connection interrupted fetching {url or _LOOPBACK_URL}"
    )


def download(
    url: Optional[str] = None,
    *,
    scenario: str = "ok",
    max_bytes: int = DEFAULT_MAX_BYTES,
):
    """按 ``scenario`` 返回 :class:`FakeResponse` 或抛 ``TimeoutError`` / ``ConnectionError``。

    ``url`` 参数仅用于回填 ``final_url`` 和异常消息，不会被实际请求。
    ``max_bytes`` 仅 ``oversized`` 场景使用，允许调用方自定义阈值。

    未知 ``scenario`` 抛 ``ValueError``，避免测试静默落到默认分支。
    """
    target = url or _LOOPBACK_URL

    if scenario == "ok":
        return _make_ok(target)
    if scenario == "http_error":
        return _make_http_error(target)
    if scenario == "redirect_to_private":
        return _make_redirect_to_private(target)
    if scenario == "timeout":
        return _raise_timeout(target)
    if scenario == "oversized":
        return _make_oversized(target, max_bytes=max_bytes)
    if scenario == "gzip_bomb":
        return _make_gzip_bomb(target)
    if scenario == "interrupted":
        return _raise_interrupted(target)

    raise ValueError(
        f"unknown fake_downloader scenario: {scenario!r}. "
        f"expected one of {SCENARIOS}"
    )
