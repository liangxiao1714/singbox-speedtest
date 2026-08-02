"""``sing-box check`` 子进程替身。

:singbox_speedtest:`singbox_speedtest.py` 通过 ``subprocess.run([..., "check", "-c", cfg_path],
capture_output=True, text=True, timeout=...)`` 调用 sing-box 进行配置校验，
消费返回对象的 ``.returncode`` / ``.stdout`` / ``.stderr`` 字段（即
:class:`subprocess.CompletedProcess` 接口）。

本模块在不真实启动 sing-box 二进制的前提下，按 scenario 返回相同形状的结果对象，
便于 Phase 0+ 测试覆盖：

- ``ok``     : check 通过 (returncode=0, stderr 为空)
- ``fail``   : check 失败 (returncode!=0, stderr 含真实风格字段错误信息)
- ``crash``  : 异常退出 (returncode=1, stderr 为空 / garbled)
- ``leak``   : stderr 含敏感凭据 (假 UUID / 假密码)，用于验证调用方不会
                原样回显 stderr

所有敏感数据均为 RFC 文档 IP / 零 UUID / 明确标注 "fake" 的字符串，绝不含真实凭据。
"""

from __future__ import annotations

import subprocess
from typing import Optional, Union

__all__ = ["run_check", "FakeCheckResult", "SCENARIOS"]


# Scenario 名单（与文档 16.9 对齐）。导出供调用方做覆盖率自检。
SCENARIOS = ("ok", "fail", "crash", "leak")

# 假敏感常量（明确以 "fake" / 全零 UUID 标记，避免任何真实凭据误判）
_FAKE_UUID = "00000000-0000-0000-0000-000000000000"
_FAKE_PASSWORD = "fake-secret-123"


class FakeCheckResult(subprocess.CompletedProcess):
    """与 :class:`subprocess.CompletedProcess` 完全兼容的结果对象。

    ``singbox_speedtest.py`` 中的典型用法::

        chk = subprocess.run([self.singbox, "check", "-c", cfg_path],
                             capture_output=True, text=True, timeout=10)
        if chk.returncode != 0:
            err = (chk.stderr.strip().split("\\n")[-1][:100]) if chk.stderr else "配置错误"

    因此本类只需要 ``returncode`` / ``stdout`` / ``stderr`` 三个属性即可被透明消费。
    继承 ``subprocess.CompletedProcess`` 是为了让 ``isinstance`` 校验也通过。
    """

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        # subprocess.CompletedProcess 的 args 字段对消费方透明，可留空 list
        super().__init__(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---- 各 scenario 的工厂 ----------------------------------------------------

def _make_ok() -> FakeCheckResult:
    return FakeCheckResult(returncode=0, stdout="sing-box check ok\n", stderr="")


def _make_fail() -> FakeCheckResult:
    # 真实风格的字段错误信息（非敏感），用于验证调用方截取最后一行的逻辑
    stderr = (
        "FATAL[2026-01-01T00:00:00Z] invalid config: "
        "outbounds[0].server_port is required "
        "field 'server_port' is missing\n"
    )
    return FakeCheckResult(returncode=1, stdout="", stderr=stderr)


def _make_crash() -> FakeCheckResult:
    # 模拟 SIGSEGV / panic 异常退出：空或乱码 stderr
    return FakeCheckResult(returncode=1, stdout="", stderr="")


def _make_leak() -> FakeCheckResult:
    # 关键：stderr 内含假敏感凭据，用于断言调用方不会原样回显
    stderr = (
        "FATAL[2026-01-01T00:00:00Z] dial failed: "
        f"uuid={_FAKE_UUID} "
        f"password={_FAKE_PASSWORD} "
        "transport handshake error\n"
    )
    return FakeCheckResult(returncode=1, stdout="", stderr=stderr)


_FACTORIES = {
    "ok": _make_ok,
    "fail": _make_fail,
    "crash": _make_crash,
    "leak": _make_leak,
}


def run_check(
    config_path: Optional[Union[str, "os.PathLike[str]"]] = None,
    *,
    scenario: str = "ok",
):
    """返回按 ``scenario`` 选定的 ``CompletedProcess`` 兼容结果对象。

    参数 ``config_path`` 与真实 ``subprocess.run`` 调用形态对齐（接受
    sing-box 的 ``-c <path>`` 参数对应的配置路径），但本替身不会读取该文件，
    仅做形参兼容，便于未来直接 monkey-patch 替换。

    传入未知 ``scenario`` 会抛 ``ValueError``，避免静默落到默认分支
    导致测试无法区分 scenario。
    """
    if scenario not in _FACTORIES:
        raise ValueError(
            f"unknown fake_singbox scenario: {scenario!r}. "
            f"expected one of {SCENARIOS}"
        )
    # 每次返回全新对象，避免调用方意外 mutate 影响后续 scenario
    return _FACTORIES[scenario]()
