"""Phase 0 冒烟测试基线。

目的：验证项目根模块可被导入、关键公开 API 行为符合预期，并确保两个
fake 替身 (sing-box / downloader) 的关键 scenario 行为正确，为后续阶段
(Phase 1+ 真正接入订阅转换、配置生成等) 提供回归基线。

执行方式（项目根目录）::

    python -m unittest discover -s tests -v

不依赖任何第三方包；仅使用 ``unittest`` + 标准库。
"""

from __future__ import annotations

import os
import sys
import unittest

# --- 把项目根目录加入 sys.path，保证可 ``import singbox_speedtest`` ----------
# 即便测试由 ``python -m unittest discover -s tests`` 启动（CWD=项目根，
# 已在 sys.path），也显式兜底，确保从任意 CWD 运行均可导入。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import singbox_speedtest  # noqa: E402  (sys.path 已显式处理)

# 测试替身：用绝对导入保证 ``python -m unittest discover`` 与 IDE 两种入口都能跑通
from fakes import fake_downloader, fake_singbox  # noqa: E402

_FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class TestProjectRootModule(unittest.TestCase):
    """对 ``singbox_speedtest`` 模块本身的冒烟校验。"""

    def test_module_importable_and_has_version_compat(self):
        # 模块成功导入即视为通过；额外断言几个核心符号确实存在，
        # 防止未来重构把对外 API 名字改掉而测试基线没更新。
        self.assertTrue(hasattr(singbox_speedtest, "make_id"))
        self.assertTrue(hasattr(singbox_speedtest, "PROXY_TYPES"))
        self.assertTrue(hasattr(singbox_speedtest, "SpeedTest"))

    def test_proxy_types_contains_expected_protocols(self):
        # Section 16.x：PROXY_TYPES 至少覆盖首期承诺的六个协议
        for proto in ("vless", "vmess", "trojan", "hysteria2", "shadowsocks", "tuic"):
            self.assertIn(
                proto,
                singbox_speedtest.PROXY_TYPES,
                msg=f"PROXY_TYPES 缺少协议: {proto}",
            )

    def test_proxy_types_is_set_like_collection(self):
        # 仅断言可做成员判断；不强依赖 set 类型，允许未来切换为 frozenset / tuple
        self.assertTrue(hasattr(singbox_speedtest.PROXY_TYPES, "__contains__"))


class TestMakeId(unittest.TestCase):
    """``singbox_speedtest.make_id`` —— 节点稳定关联键。"""

    @staticmethod
    def _base_outbound():
        """最小可识别 vless outbound（不含真实凭据）。"""
        return {
            "type": "vless",
            "server": "server.example.com",
            "server_port": 443,
            # uuid 属于凭据，按设计不应进入 node_id；这里给假值仅用于证明
            # 改 uuid 不会改变 id（凭据不变性）。
            "uuid": "00000000-0000-0000-0000-000000000000",
        }

    def test_stable_for_same_outbound(self):
        ob = self._base_outbound()
        # 调用多次应得到完全相同的字符串
        self.assertEqual(singbox_speedtest.make_id(ob), singbox_speedtest.make_id(ob))

    def test_credential_change_does_not_change_id(self):
        # Section 17.1：凭据 (uuid/password) 变化不应改变 node_id
        ob_a = self._base_outbound()
        ob_b = self._base_outbound()
        ob_b["uuid"] = "11111111-2222-3333-4444-555555555555"
        self.assertEqual(singbox_speedtest.make_id(ob_a), singbox_speedtest.make_id(ob_b))

    def test_transport_difference_changes_id(self):
        # 同 type/server/port，但 transport.path 不同 → 视为不同节点
        ob_a = self._base_outbound()
        ob_a["transport"] = {"type": "ws", "path": "/path-a"}

        ob_b = self._base_outbound()
        ob_b["transport"] = {"type": "ws", "path": "/path-b"}

        id_a = singbox_speedtest.make_id(ob_a)
        id_b = singbox_speedtest.make_id(ob_b)
        self.assertNotEqual(id_a, id_b)

        # 进一步：两者应共享同样的 'type:server:port' 前缀，仅在 '#hash' 后缀处区分
        prefix = "vless:server.example.com:443"
        self.assertTrue(id_a.startswith(prefix), msg=f"id_a={id_a!r}")
        self.assertTrue(id_b.startswith(prefix), msg=f"id_b={id_b!r}")
        self.assertIn("#", id_a)
        self.assertIn("#", id_b)


class TestFakeSingbox(unittest.TestCase):
    """对 :mod:`tests.fakes.fake_singbox` 自身的健壮性校验。"""

    def test_ok_returns_zero_returncode_and_empty_stderr(self):
        r = fake_singbox.run_check(scenario="ok")
        # 必须暴露 subprocess.CompletedProcess 兼容的三元字段
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")
        # stdout 字段存在（内容不强约束）
        self.assertIsNotNone(r.stdout)

    def test_fail_returns_nonzero_with_field_error(self):
        r = fake_singbox.run_check(scenario="fail")
        self.assertNotEqual(r.returncode, 0)
        # stderr 含真实风格的字段错误描述
        self.assertIn("server_port", r.stderr)

    def test_crash_returns_nonzero_and_garbled_or_empty_stderr(self):
        r = fake_singbox.run_check(scenario="crash")
        self.assertNotEqual(r.returncode, 0)
        # crash 场景典型特征：无可读 stderr
        self.assertEqual(r.stderr.strip(), "")

    def test_leak_stderr_contains_fake_secret(self):
        # 这是给后续"调用方不得原样回显 stderr"用例准备的源数据；
        # 这里只断言 leak 场景确实投递了假敏感凭据。
        r = fake_singbox.run_check(scenario="leak")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("00000000-0000-0000-0000-000000000000", r.stderr)
        self.assertIn("fake-secret-123", r.stderr)

    def test_unknown_scenario_raises(self):
        # 防御：未知 scenario 不应静默回落到 ok
        with self.assertRaises(ValueError):
            fake_singbox.run_check(scenario="not-a-scenario")

    def test_completed_process_compatible_instance(self):
        # 调用方如用 isinstance 校验，也应当通过
        import subprocess
        r = fake_singbox.run_check(scenario="ok")
        self.assertIsInstance(r, subprocess.CompletedProcess)


class TestFakeDownloader(unittest.TestCase):
    """对 :mod:`tests.fakes.fake_downloader` 自身的健壮性校验。"""

    def test_ok_returns_200_with_uri_body(self):
        resp = fake_downloader.download(scenario="ok")
        self.assertEqual(resp.status_code, 200)
        # 正文应至少包含一个 URI scheme 行
        self.assertIn("vless://", resp.text)
        self.assertIsInstance(resp.content, bytes)
        # 必备字段存在
        self.assertTrue(hasattr(resp, "headers"))
        self.assertTrue(hasattr(resp, "final_url"))
        self.assertGreaterEqual(resp.elapsed, 0.0)

    def test_http_error_returns_4xx_with_html_body(self):
        resp = fake_downloader.download(scenario="http_error")
        self.assertGreaterEqual(resp.status_code, 400)
        self.assertLess(resp.status_code, 600)
        # HTML 错误页便于后续验证"非订阅正文"判定
        self.assertIn("<html", resp.text.lower())

    def test_redirect_to_private_final_url_is_private(self):
        resp = fake_downloader.download(scenario="redirect_to_private")
        # 落点必须是私网 / 回环地址，用于 SSRF 守卫测试
        private_markers = ("127.0.0.1", "10.0.0.1", "192.168.1.1")
        self.assertTrue(
            any(m in resp.final_url for m in private_markers),
            msg=f"final_url={resp.final_url!r} 未指向私网地址",
        )

    def test_timeout_raises_timeout_error(self):
        with self.assertRaises(TimeoutError):
            fake_downloader.download(scenario="timeout")

    def test_interrupted_raises_connection_error(self):
        with self.assertRaises(ConnectionError):
            fake_downloader.download(scenario="interrupted")

    def test_oversized_body_exceeds_threshold(self):
        # 默认阈值 10 MiB；body 应严格大于该阈值
        threshold = fake_downloader.DEFAULT_MAX_BYTES
        resp = fake_downloader.download(scenario="oversized")
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(len(resp.content), threshold)

    def test_oversized_respects_custom_max_bytes(self):
        # 自定义阈值：验证 body 严格超过该阈值
        custom = 1024
        resp = fake_downloader.download(scenario="oversized", max_bytes=custom)
        self.assertGreater(len(resp.content), custom)

    def test_gzip_bomb_has_gzip_encoding_header_and_small_payload(self):
        # gzip_bomb 场景：投递小 gzipped payload + content-encoding 提示
        resp = fake_downloader.download(scenario="gzip_bomb")
        self.assertEqual(resp.status_code, 200)
        # 头键应小写
        self.assertEqual(resp.headers.get("content-encoding"), "gzip")
        # 压缩载荷本身较小（解压后才膨胀），便于 Phase 2/5 验证解压大小守卫
        self.assertLess(len(resp.content), 1024, msg="gzip bomb 原始压缩载荷应较小")
        # 解压验证（不依赖被测代码，仅校验 fake 自洽）
        import gzip
        decompressed = gzip.decompress(resp.content)
        self.assertGreater(len(decompressed), 64 * 1024)

    def test_unknown_scenario_raises(self):
        with self.assertRaises(ValueError):
            fake_downloader.download(scenario="not-a-scenario")


class TestFixturesOptional(unittest.TestCase):
    """对 fixtures 数据文件的可选测试。

    fixtures 目录由另一个 agent 生成；缺失时整体 skip，绝不阻塞基线绿。
    """

    def _fixture_path(self, *parts):
        return os.path.join(_FIXTURES_DIR, *parts)

    @unittest.skipIf(
        not os.path.exists(_FIXTURES_DIR),
        "fixtures 目录尚未生成（由另一 agent 创建）；跳过 fixture 相关测试。",
    )
    def test_minimal_singbox_fixture_loadable(self):
        path = self._fixture_path("singbox", "minimal_vless.json")
        if not os.path.exists(path):
            self.skipTest(f"fixture 不存在: {path}")
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIsInstance(data, dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
