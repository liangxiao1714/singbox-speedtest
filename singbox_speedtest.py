#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sing-box 多节点测速工具
========================
读取 sing-box 格式的配置文件（service_core.json），为每个代理节点单独启动
临时 sing-box 实例进行真实下载测速。兼容 Karing / clash-verge / 自建 等任何
生成 sing-box 配置的工具。

功能:
  - 多节点批量下载测速（串行，不影响当前使用的节点）
  - 订阅分组标识与筛选（自动识别订阅源）
  - 批量 ping（sing-box 并发延迟测试 + TCP 端口探测，两种）
  - 分阶段实时状态（解析→连接→握手→下载进度）
  - 流量统计（总量 + 单节点 下载/上传字节）
  - 出口真实 IP + 地理位置/ISP 获取
  - 测速历史持久化（含失败记录，趋势对比）
  - Web 界面：行内状态 + 点击详情面板（阶段日志）

用法:
    python singbox_speedtest.py                       # Web 界面
    python singbox_speedtest.py --port 8088           # 指定端口
    python singbox_speedtest.py --cli --filter 日本   # 命令行
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ======================== 默认配置 ========================
# 项目目录约定：配置源放在 config/，历史放在 data/，sing-box 二进制放在 bin/
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(PROJECT_DIR, "config")
DATA_DIR = os.path.join(PROJECT_DIR, "data")
HISTORY_JSON = "singbox_speedtest_history.json"
CORE_JSON = "service_core.json"
SUB_JSON = "karing_subscribe.json"
DEFAULT_SINGBOX = ""  # 留空则自动查找（PATH 环境变量 / v2rayN 常见安装位置）
DEFAULT_DL_BYTES = 20 * 1024 * 1024   # 20 MB
DEFAULT_DL_BYTES = 10 * 1024 * 1024   # 10 MB（默认；Cloudflare 对大文件可能限流，失败会自动换源）
DEFAULT_TIMEOUT = 25
BASE_PORT = 19000
# 测速源列表（按优先级；某源返回截断/失败时自动尝试下一个）
SPEEDTEST_URLS = [
    "https://speed.cloudflare.com/__down?bytes={bytes}",
    "https://cachefly.cachefly.net/10mb.test",
    "https://cachefly.cachefly.net/50mb.test",
    "https://prooforbit.com/10mb.bin",
]
LATENCY_URL = "https://www.gstatic.com/generate_204"
IP_URL = "http://ip-api.com/json/?fields=query,country,regionName,city,isp,org"
PROXY_TYPES = {"vless", "vmess", "trojan", "hysteria", "hysteria2", "shadowsocks", "ssh", "wireguard", "tuic"}


def find_singbox():
    # 按优先级查找 sing-box：脚本目录（自带）→ PATH → v2rayN 常见安装位置
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        # 脚本自带（项目 bin 子目录 / 项目根目录），实现自包含部署
        os.path.join(_script_dir, "bin", "sing-box.exe"),
        os.path.join(_script_dir, "sing-box.exe"),
        "sing-box",  # 系统 PATH（Linux/macOS/已加入PATH的Windows）
        "sing-box.exe",
        # v2rayN 常见安装位置（Windows）
        r"D:\v2rayN-windows-64\bin\sing_box\sing-box.exe",
        r"C:\v2rayN\bin\sing_box\sing-box.exe",
        r"C:\Program Files\v2rayN\bin\sing_box\sing-box.exe",
        os.path.expanduser(r"~\v2rayN\bin\sing_box\sing-box.exe"),
    ]
    for c in candidates:
        try:
            r = subprocess.run([c, "version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 or "sing-box" in (r.stdout + r.stderr).lower():
                return c
        except Exception:
            continue
    return DEFAULT_SINGBOX


def make_id(outbound):
    """节点稳定关联键（用于测速历史跨订阅更新关联）。
    基础 = '{type}:{server}:{port}'；若节点含传输层/TLS 结构性差异字段（CDN 前置、
    多 SNI / Reality / WS路径等共用同一 server:port 的情况），追加这些字段的指纹
    哈希后缀以消歧。**不含密钥/密码/uuid/tag**：改密、改名、订阅重组不影响关联，
    仅在协议/服务器/端口/传输结构变化时才视为新节点。"""
    t = str(outbound.get("type", "")).lower()
    server = str(outbound.get("server", "")).lower()
    port = outbound.get("server_port") or outbound.get("server_ports") or ""
    base = f"{t}:{server}:{port}"
    fp = _structural_fingerprint(outbound, t)
    if fp:
        h = hashlib.md5(fp.encode("utf-8")).hexdigest()[:8]
        return f"{base}#{h}"
    return base


def _structural_fingerprint(outbound, t):
    """提取用于区分'共用 type:server:port'节点的结构性字段（不含密钥/密码/tag）。
    返回排序后的 'k=v|k=v' 串；无差异字段时返回 ''。"""
    parts = []
    tp = outbound.get("transport") or {}
    if isinstance(tp, dict):
        for k in ("type", "path", "service_name", "server_name"):
            v = tp.get(k)
            if v:
                parts.append(f"tp{k}={v}")
    tls = outbound.get("tls") or {}
    if isinstance(tls, dict):
        if tls.get("server_name"):
            parts.append(f"sni={tls.get('server_name')}")
        if tls.get("insecure"):
            parts.append("insec=1")
        alpn = tls.get("alpn")
        if alpn:
            parts.append("alpn=" + ("+".join(alpn) if isinstance(alpn, list) else str(alpn)))
        reality = tls.get("reality") or {}
        if isinstance(reality, dict) and reality.get("enabled"):
            if reality.get("public_key"):
                parts.append(f"rpk={reality.get('public_key')}")
            if reality.get("short_id"):
                parts.append(f"rsi={reality.get('short_id')}")
        utls = tls.get("utls") or {}
        if isinstance(utls, dict) and utls.get("enabled") and utls.get("fingerprint"):
            parts.append(f"utls={utls.get('fingerprint')}")
    if t == "shadowsocks":
        if outbound.get("method"):
            parts.append(f"method={outbound.get('method')}")
    elif t == "hysteria2":
        obfs = outbound.get("obfs")
        if isinstance(obfs, dict) and obfs.get("type"):
            parts.append(f"obfs={obfs.get('type')}")
        if outbound.get("up"):
            parts.append(f"up={outbound.get('up')}")
        if outbound.get("down"):
            parts.append(f"down={outbound.get('down')}")
    parts.sort()
    return "|".join(parts)


def _resolve_in_order(name, cli_arg, env_key, extra_dirs):
    """按 (cli_arg → 环境变量 → 各候选目录) 顺序查找名为 name 的文件，
    返回第一个存在的绝对路径；全部不存在则返回最后一个候选字符串（用于上层
    做存在性容错或友好报错）。"""
    candidates = []
    if cli_arg:
        candidates.append(cli_arg)
    env_val = os.environ.get(env_key, "")
    if env_val:
        candidates.append(env_val)
    for d in extra_dirs:
        candidates.append(os.path.join(d, name))
    # 第一个存在的直接返回
    for c in candidates:
        if c and os.path.exists(c):
            return c
    # 全部不存在：返回最后一个候选字符串（若空则返回 name 本身）
    return candidates[-1] if candidates else name


def resolve_core_path(cli_arg, config_dir=None):
    """按优先级定位 service_core.json：(1) cli_arg → (2) env SINGBOX_SPEEDTEST_CORE
    → (3) config_dir 下 → (4) CONFIG_DIR → (5) PROJECT_DIR。全不存在返回 None。"""
    dirs = [d for d in [config_dir, CONFIG_DIR, PROJECT_DIR] if d]
    found = _resolve_in_order(CORE_JSON, cli_arg, "SINGBOX_SPEEDTEST_CORE", dirs)
    return found if (found and os.path.exists(found)) else None


def resolve_sub_path(cli_arg, config_dir=None):
    """按优先级定位 karing_subscribe.json：结构同 resolve_core_path（env
    SINGBOX_SPEEDTEST_SUB）。sub 不存在不致命，返回最终候选路径字符串即可
    （_scan_nodes 内部对不存在做容错）。"""
    dirs = [d for d in [config_dir, CONFIG_DIR, PROJECT_DIR] if d]
    return _resolve_in_order(SUB_JSON, cli_arg, "SINGBOX_SPEEDTEST_SUB", dirs)


def resolve_history_path(cli_arg=None):
    """按优先级定位历史文件：(1) cli_arg → (2) env SINGBOX_SPEEDTEST_HISTORY
    → (3) DATA_DIR/HISTORY_JSON。函数内确保 DATA_DIR 存在。返回路径字符串。"""
    candidates = []
    if cli_arg:
        candidates.append(cli_arg)
    env_val = os.environ.get("SINGBOX_SPEEDTEST_HISTORY", "")
    if env_val:
        candidates.append(env_val)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass
    candidates.append(os.path.join(DATA_DIR, HISTORY_JSON))
    return candidates[0] if candidates else os.path.join(DATA_DIR, HISTORY_JSON)


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _is_loopback(host):
    """判断 host 是否为回环地址（127.0.0.1 / ::1 / localhost / 127.*）。"""
    if not host:
        return False
    h = host.strip().lower()
    return h in ("127.0.0.1", "::1", "localhost") or h.startswith("127.")


def parse_args():
    p = argparse.ArgumentParser(description="sing-box 多节点测速工具")
    p.add_argument("--core", default=None, help="service_core.json 路径")
    p.add_argument("--subscribe", default=None, help="karing_subscribe.json 路径（用于订阅分组）")
    p.add_argument("--config-dir", default=None, dest="config_dir",
                   help="配置文件目录，下含 service_core.json / karing_subscribe.json")
    p.add_argument("--history", default=None, help="测速历史文件路径")
    p.add_argument("--singbox", default=None, help="sing-box.exe 路径")
    p.add_argument("--bytes", type=int, default=DEFAULT_DL_BYTES, help=f"测速下载字节数（默认 {DEFAULT_DL_BYTES}）")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"单节点测速超时（默认 {DEFAULT_TIMEOUT}s）")
    p.add_argument("--port", type=int, default=8088, help="Web 端口（默认 8088）")
    # 阶段 0（Section 16.1）：默认监听回环地址，避免暴露未认证的 Web 接口
    p.add_argument("--host", default="127.0.0.1", help="Web 监听地址（默认 127.0.0.1；阶段 5 前仅允许回环地址）")
    p.add_argument("--cli", action="store_true", help="命令行模式")
    p.add_argument("--filter", default="", help="节点名筛选")
    return p.parse_args()


# ======================== 核心逻辑 ========================
class SpeedTest:
    def __init__(self, core_path, sub_path, singbox, dl_bytes, timeout, history_path):
        self.core_path = core_path
        self.sub_path = sub_path
        self.singbox = singbox
        self.dl_bytes = dl_bytes
        self.timeout = timeout
        self.ping_count = 5   # 默认 ping 次数（可前端配置）
        self.nodes = []         # {id, tag, type, server, port, subgroup, outbound}
        self.results = {}       # id -> result dict
        self.lock = threading.RLock()
        self.state = "idle"     # idle/pinging/testing/done/stopping
        self.current = None
        self.progress = {"done": 0, "total": 0}
        self.total_dl = 0       # 累计下载字节
        self.total_ul = 0       # 累计上传字节
        self.logs = defaultdict(list)   # id -> [log lines]
        self.subgroups = {}     # groupid -> remark
        self.tmp_dir = os.path.join(os.environ.get("TEMP", "/tmp"), "singbox_speedtest")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.history_path = history_path
        # 先做历史文件位置迁移（旧位置 = 配置同目录），再加载历史
        self._migrate_history_location(core_path)
        self.history = self._load_history()
        # 历史关联键迁移（旧 tag-keyed → 新 stable_id-keyed）
        self._apply_nodes(self._scan_nodes())
        self._migrate_history_keys()
        self._save_history()  # 确保首次启动即落盘（初始 {}），避免历史文件缺位

    def _load_history(self):
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_history(self):
        try:
            with open(self.history_path, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False)
        except Exception:
            pass

    def _migrate_history_location(self, core_path):
        """历史文件位置迁移：旧位置 = 配置文件同目录下的 singbox_speedtest_history.json。
        若 self.history_path 已存在则无需迁移；若旧位置存在则复制（不动原件）到新位置。"""
        try:
            if self.history_path and os.path.exists(self.history_path):
                return
            old = os.path.join(os.path.dirname(core_path), "singbox_speedtest_history.json")
            if os.path.exists(old):
                os.makedirs(os.path.dirname(self.history_path), exist_ok=True)
                shutil.copy2(old, self.history_path)
                print(f"已迁移历史文件: {old} -> {self.history_path}（原件保留）")
        except Exception as e:
            print(f"历史位置迁移跳过: {e}")

    def _migrate_history_keys(self):
        """历史关联键迁移：旧格式 history 以节点 tag 为 key，新格式以 stable_id
        （'{type}:{server}:{port}'）为 key。判定逻辑：旧 key 能匹配当前某节点 tag
        且不等于该节点 id，则迁移；已是新格式的原样保留；无法匹配的存为孤儿备份。"""
        old = self.history
        if not old or not isinstance(old, dict):
            return
        try:
            cur_ids = {n["id"] for n in self.nodes}
            tag_to_id = {n["tag"]: n["id"] for n in self.nodes}
            new, orphans = {}, {}
            for key, recs in old.items():
                if key in cur_ids:
                    new[key] = recs  # 已是新格式
                elif key in tag_to_id:
                    new.setdefault(tag_to_id[key], []).extend(recs)
                else:
                    orphans[key] = recs  # 无法匹配 → 孤儿
            if orphans:
                try:
                    backup = self.history_path + ".orphan.bak"
                    with open(backup, "w", encoding="utf-8") as f:
                        json.dump(orphans, f, ensure_ascii=False, indent=2)
                    print(f"警告: {len(orphans)} 条历史无法匹配当前节点，已备份到 {backup}")
                except Exception:
                    pass
            # 迁移前必须先备份原件（绝不静默丢弃用户数据）
            try:
                shutil.copy2(self.history_path, self.history_path + ".v1.bak")
            except Exception:
                pass
            self.history = new
            self._save_history()
        except Exception as e:
            print(f"历史键迁移跳过: {e}")

    # subscribe 的 server 项里 Karing 元数据字段（sing-box 不识别，需剔除）
    _SUB_DROP_KEYS = {"attach", "groupid", "latency", "outlet_ip", "outlet_region", "domain_resolver"}

    def _scan_nodes(self):
        """纯读盘 + 去重，返回节点列表。每个节点：{id, tag, type, server, port, subgroup, outbound}。
        优先从订阅文件（karing_subscribe.json）的 items[].servers 提取——其 server 项含完整
        sing-box 连接字段且自带订阅分组，能拿到全部订阅的全部节点（即使 service_core.json
        滞后未含某些订阅）；无订阅文件或其无 servers 时 fallback 到 service_core.json 的
        outbounds（兼容非 Karing 来源，如 clash-verge）。"""
        self.subgroups = {}
        nodes = self._scan_from_subscribe()
        if not nodes:
            nodes = self._scan_from_core()
        return nodes

    def _scan_from_subscribe(self):
        """从 karing_subscribe.json 的 items[].servers 提取节点（主路径）。
        server 项去掉 Karing 元数据字段后即为合法 sing-box outbound（已验证）。
        subgroup 直接取所在 item 的 remark，无需 tag 反查。按 tag 去重。"""
        if not (self.sub_path and os.path.exists(self.sub_path)):
            return []
        try:
            with open(self.sub_path, "r", encoding="utf-8") as f:
                sub = json.load(f)
        except Exception:
            return []
        nodes = []
        seen = set()
        for item in sub.get("items", []):
            gid = item.get("groupid", "")
            remark = item.get("remark", "") or gid or "未知"
            self.subgroups[gid] = remark
            for s in item.get("servers", []):
                outbound = {k: v for k, v in s.items() if k not in self._SUB_DROP_KEYS}
                t = outbound.get("type", "")
                if t not in PROXY_TYPES:
                    continue
                tag = s.get("tag", "") or t
                if tag in seen:
                    continue
                seen.add(tag)
                nodes.append({
                    "id": make_id(outbound),
                    "tag": tag,
                    "type": t,
                    "server": outbound.get("server", ""),
                    "port": outbound.get("server_port") or outbound.get("server_ports") or "",
                    "subgroup": remark,
                    "outbound": outbound,
                })
        return nodes

    def _scan_from_core(self):
        """fallback：从 service_core.json 的 outbounds 提取（非 Karing 来源）。
        订阅分组通过 tag 反查 subscribe（若有）；无则为'未知'。按 tag 去重。"""
        try:
            with open(self.core_path, "r", encoding="utf-8") as f:
                core = json.load(f)
        except Exception:
            return []
        tag_to_sub = {}
        if self.sub_path and os.path.exists(self.sub_path):
            try:
                with open(self.sub_path, "r", encoding="utf-8") as f:
                    sub = json.load(f)
                for item in sub.get("items", []):
                    gid = item.get("groupid", "")
                    remark = item.get("remark", "") or gid or "未知"
                    self.subgroups[gid] = remark
                    for s in item.get("servers", []):
                        tag_to_sub[s.get("tag", "")] = remark
            except Exception:
                pass
        nodes = []
        seen = set()
        for ob in core.get("outbounds", []):
            t = ob.get("type", "")
            if t not in PROXY_TYPES:
                continue
            tag = ob.get("tag", t)
            if tag in seen:
                continue
            seen.add(tag)
            nodes.append({
                "id": make_id(ob),
                "tag": tag,
                "type": t,
                "server": ob.get("server", ""),
                "port": ob.get("server_port") or ob.get("server_ports") or "",
                "subgroup": tag_to_sub.get(tag, "未知"),
                "outbound": ob,
            })
        return nodes

    def _apply_nodes(self, new_nodes):
        """合并新节点列表：保留同 tag 的旧 result、新增 _blank、丢弃消失的 tag。
        必须在 self.lock 内调用。logs 不裁剪（按 tag 保留旧的）。"""
        with self.lock:
            old_results = self.results
            self.nodes = new_nodes
            new_results = {}
            for n in new_nodes:
                new_results[n["tag"]] = old_results.get(n["tag"]) or self._blank()
            self.results = new_results

    def reload(self):
        """重新扫描配置源并合并节点列表。必须在非测速状态调用（HTTP 层守卫）。"""
        with self.lock:
            self._apply_nodes(self._scan_nodes())

    def _blank(self):
        return {"status": "pending", "phase": "", "speed_mbps": 0, "speed_MBps": 0,
                "peak_speed": 0, "avg_speed": 0,
                "latency_ms": 0, "latency_samples": [], "tcp_ping": 0, "tcp_samples": [],
                "ip": "", "ip_geo": "", "dl_bytes": 0, "ul_bytes": 0,
                "dl_progress": 0, "cur_speed": 0, "error": "", "log": []}

    def _log(self, tag, msg):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self.lock:
            self.logs[tag].append(line)
            r = self.results.get(tag)
            if r is not None:
                r["log"] = list(self.logs[tag])[-30:]

    def _set(self, tag, **kw):
        with self.lock:
            r = self.results.get(tag)
            if r is not None:
                r.update(kw)

    def _gen_cfg(self, node, port):
        ob = dict(node["outbound"])
        ob["tag"] = "__t__"
        ob.pop("domain_resolver", None)
        return {
            "log": {"level": "warn"},
            "dns": {"servers": [
                {"type": "udp", "tag": "dd", "server": "223.6.6.6"},
                {"type": "udp", "tag": "dp", "server": "1.1.1.1", "detour": "__t__"}],
                "final": "dd", "strategy": "ipv4_only"},
            "inbounds": [{"type": "mixed", "tag": "m", "listen": "127.0.0.1", "listen_port": port}],
            "outbounds": [ob, {"type": "direct", "tag": "direct_out"}],
            "route": {"final": "__t__", "default_domain_resolver": {"server": "dd"}},
        }

    def _alloc_port(self):
        global BASE_PORT
        for _ in range(300):
            p = BASE_PORT
            BASE_PORT += 1
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(("127.0.0.1", p))
                    return p
            except OSError:
                continue
        BASE_PORT += 1
        return BASE_PORT

    def _curl(self, proxy, url, timeout, write_out=True):
        """返回 (speed_Bps, latency_ms, http_code, dl_bytes, ul_bytes)"""
        w = "%{http_code}|%{time_total}|%{speed_download}|%{size_download}|%{size_upload}" if write_out else \
            "%{http_code}|%{time_total}|%{speed_download}"
        try:
            r = subprocess.run(
                ["curl", "-s", "-x", proxy, "-o", os.devnull, "-w", w, "--max-time", str(timeout), url],
                capture_output=True, text=True, timeout=timeout + 5)
            parts = r.stdout.strip().split("|")
            code = parts[0]
            t = float(parts[1]) if len(parts) > 1 else 0
            spd = float(parts[2]) if len(parts) > 2 else 0
            dl = int(float(parts[3])) if len(parts) > 3 and parts[3] else 0
            ul = int(float(parts[4])) if len(parts) > 4 and parts[4] else 0
            return spd, int(t * 1000), code, dl, ul
        except Exception:
            return 0, -1, "000", 0, 0

    def _curl_stream(self, proxy, url, total_bytes, timeout, node_id):
        """流式下载：curl 后台下载到临时文件，主线程周期采样文件大小计算实时速度。
        返回 (speed_Bps, http_code, dl_bytes, ul_bytes)。"""
        import hashlib
        out_path = os.path.join(self.tmp_dir, "dl_" + hashlib.md5(node_id.encode()).hexdigest()[:8] + ".bin")
        try:
            os.remove(out_path)
        except OSError:
            pass
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            ["curl", "-s", "-x", proxy, "-o", out_path, "--max-time", str(timeout), url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags,
        )
        t0 = time.time()
        last_logged = 0.0
        last_size = 0
        last_t = t0
        peak_Bps = 0.0
        spd_samples = []
        while True:
            time.sleep(0.3)
            rc = proc.poll()
            try:
                cur_size = os.path.getsize(out_path)
            except OSError:
                cur_size = last_size
            now = time.time()
            pct = min(100.0, cur_size / total_bytes * 100) if total_bytes else 0
            # 瞬时速度（本次采样间隔）
            dt = now - last_t
            inst_spd = (cur_size - last_size) / dt if dt > 0 else 0
            if inst_spd > peak_Bps:
                peak_Bps = inst_spd
            if inst_spd > 0 and cur_size > last_size:
                spd_samples.append(inst_spd)
            if now - last_logged >= 0.4 or rc is not None:
                self._set(node_id, phase=f"下载中 {pct:.0f}% ({inst_spd/1024/1024:.1f}MB/s)",
                          dl_progress=round(pct, 1), cur_speed=round(inst_spd * 8 / 1_000_000, 1),
                          peak_speed=round(peak_Bps * 8 / 1_000_000, 1))
                last_logged = now
            last_size = cur_size
            last_t = now
            if rc is not None:
                break
            if now - t0 > timeout:
                proc.terminate()
                break
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        dl_size = 0
        try:
            dl_size = os.path.getsize(out_path)
        except OSError:
            pass
        try:
            os.remove(out_path)
        except OSError:
            pass
        elapsed = max(0.1, time.time() - t0)
        spd = dl_size / elapsed if dl_size else 0
        peak_mbps = round(peak_Bps * 8 / 1_000_000, 1) if peak_Bps else 0
        avg_mbps = round((sum(spd_samples) / len(spd_samples) * 8 / 1_000_000), 1) if spd_samples else round(spd * 8 / 1_000_000, 1)
        code = "200" if (proc.returncode == 0 and dl_size > 0) else ("000" if dl_size == 0 else "200")
        return spd, code, dl_size, 0, peak_mbps, avg_mbps

    # -------- TCP 端口探测 --------
    def _tcp_probe(self, node, timeout=4):
        host = node["server"]
        port = node["port"]
        if isinstance(port, str) and ":" in port:
            port = int(port.split(":")[0])
        try:
            port = int(port)
        except Exception:
            return -1
        # 解析域名
        try:
            ip = socket.gethostbyname(host)
        except Exception:
            return -1
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        t0 = time.time()
        try:
            s.connect((ip, port))
            return int((time.time() - t0) * 1000)
        except Exception:
            return -1
        finally:
            s.close()

    # -------- 单节点延迟测试（sing-box）--------
    def _ping_one(self, node, count=5):
        """sing-box 延迟测试，测多次取最小值，记录所有样本到 latency_samples"""
        tag = node["tag"]
        port = self._alloc_port()
        cfg = self._gen_cfg(node, port)
        cfg_path = os.path.join(self.tmp_dir, f"p_{port}.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        chk = subprocess.run([self.singbox, "check", "-c", cfg_path], capture_output=True, text=True, timeout=8)
        if chk.returncode != 0:
            self._set(tag, latency_ms=-1, latency_samples=[])
            try: os.remove(cfg_path)
            except OSError: pass
            return
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        proc = subprocess.Popen([self.singbox, "run", "-c", cfg_path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=si)
        try:
            time.sleep(1.2)
            samples = []
            for _ in range(count):
                if self.state == "stopping":
                    break
                _, lat, code, _, _ = self._curl(f"http://127.0.0.1:{port}", LATENCY_URL, 8)
                if code in ("200", "204") and lat > 0:
                    samples.append(lat)
                time.sleep(0.15)
            if samples:
                best = min(samples)
                self._set(tag, latency_ms=best, latency_samples=samples)
            else:
                self._set(tag, latency_ms=-1, latency_samples=[])
        finally:
            proc.terminate()
            try: proc.wait(timeout=3)
            except Exception: proc.kill()
            try: os.remove(cfg_path)
            except OSError: pass

    def ping_all(self, nodes, mode="sb", count=None):
        """mode: 'sb'=sing-box延迟, 'tcp'=TCP探测, 'both'=两者。count: 测试次数"""
        if count is None:
            count = self.ping_count
        with self.lock:
            self.state = "pinging"
            self.progress = {"done": 0, "total": len(nodes)}
            for n in nodes:
                if mode != "tcp":
                    self.results[n["tag"]]["latency_ms"] = 0
                    self.results[n["tag"]]["latency_samples"] = []
                if mode != "sb":
                    self.results[n["tag"]]["tcp_ping"] = 0
                    self.results[n["tag"]]["tcp_samples"] = []
        if mode == "tcp":
            self._tcp_batch(nodes, count)
        elif mode == "sb":
            self._sb_ping_batch(nodes, count)
        else:  # both
            self._sb_ping_batch(nodes, count)
            self._tcp_batch(nodes, count)
        with self.lock:
            self.state = "idle"

    def _sb_ping_batch(self, nodes, count=5):
        max_workers = min(16, len(nodes)) if nodes else 1
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(self._ping_one, n, count): n for n in nodes}
            for fut in as_completed(futs):
                if self.state == "stopping":
                    break
                with self.lock:
                    self.progress["done"] += 1

    def _tcp_batch(self, nodes, count=5):
        max_workers = min(32, len(nodes)) if nodes else 1
        def do(n):
            if self.state == "stopping":
                return
            samples = []
            for _ in range(count):
                if self.state == "stopping":
                    break
                ms = self._tcp_probe(n)
                if ms > 0:
                    samples.append(ms)
            if samples:
                self._set(n["tag"], tcp_ping=min(samples), tcp_samples=samples)
            else:
                self._set(n["tag"], tcp_ping=-1, tcp_samples=[])
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(do, nodes))

    def _record_fail(self, node, reason):
        """记录失败到历史（用于判断节点稳定性）。history key 用 node["id"]，
        访问当前 results 用 tag；rec 附带节点快照字段（id/tag/type/server/port）
        便于历史展示。"""
        nid = node["id"]
        tag = node["tag"]
        rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "speed_mbps": 0,
               "speed_MBps": 0, "latency_ms": self.results.get(tag, {}).get("latency_ms", 0),
               "ip": self.results.get(tag, {}).get("ip", ""), "dl_bytes": 0,
               "source": "fail", "result": "fail", "error": reason,
               "id": nid, "tag": node["tag"], "type": node["type"],
               "server": node["server"], "port": node["port"]}
        with self.lock:
            self.history.setdefault(nid, []).append(rec)
            if len(self.history[nid]) > 50:
                self.history[nid] = self.history[nid][-50:]
            self._save_history()

    # -------- 单节点测速 --------
    def _test_one(self, node):
        tag = node["tag"]
        self._set(tag, status="testing", phase="解析中…", error="")
        self._log(tag, f"开始测速: {node['server']}:{node['port']} ({node['type']})")
        port = self._alloc_port()
        cfg = self._gen_cfg(node, port)
        cfg_path = os.path.join(self.tmp_dir, f"s_{port}.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        proc = None
        try:
            chk = subprocess.run([self.singbox, "check", "-c", cfg_path], capture_output=True, text=True, timeout=10)
            if chk.returncode != 0:
                err = (chk.stderr.strip().split("\n")[-1][:100]) if chk.stderr else "配置错误"
                self._set(tag, status="error", phase="", error=err)
                self._log(tag, f"配置错误: {err}")
                return
            self._set(tag, phase="启动代理…")
            self._log(tag, "启动临时 sing-box 实例…")
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            proc = subprocess.Popen([self.singbox, "run", "-c", cfg_path],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=si)
            time.sleep(1.5)
            proxy = f"http://127.0.0.1:{port}"
            # 连通
            self._set(tag, phase="连接中…")
            self._log(tag, "测试连通性 (generate_204)…")
            _, lat, code, _, _ = self._curl(proxy, LATENCY_URL, min(10, self.timeout))
            if code not in ("200", "204"):
                reason = "节点不可用" if code == "000" else f"连接被拒(http={code})"
                self._set(tag, status="error", phase="", latency_ms=-1, error=reason)
                self._log(tag, f"连接失败: {reason}")
                self._record_fail(node, reason)
                return
            # 测速连通延迟不覆盖 ping 多次最小值（ping 更准），仅 ping 未测过时才设
            cur_lat = self.results.get(tag, {}).get("latency_ms", 0)
            if cur_lat <= 0 or not self.results.get(tag, {}).get("latency_samples"):
                self._set(tag, latency_ms=lat)
            self._log(tag, f"连通 OK, 延迟 {lat}ms")
            # 获取出口IP + 地理位置/ISP（ip-api.com 免费、无需 key）
            self._set(tag, phase="获取出口IP…")
            try:
                r = subprocess.run(["curl", "-s", "-x", proxy, "--max-time", "8", IP_URL],
                                   capture_output=True, text=True, timeout=12)
                if r.returncode == 0 and r.stdout.strip():
                    data = json.loads(r.stdout)
                    ip = data.get("query") or data.get("ip") or ""
                    if ip:
                        geo_parts = []
                        for k in ("country", "regionName", "city"):
                            v = data.get(k, "")
                            if v:
                                geo_parts.append(v)
                        isp = data.get("isp", "") or data.get("org", "")
                        geo = " ".join(geo_parts)
                        if isp:
                            geo = (geo + " · " + isp) if geo else isp
                        self._set(tag, ip=ip, ip_geo=geo)
                        self._log(tag, f"出口IP: {ip} ({geo})")
            except Exception:
                pass
            # 下载测速（多源自动降级 + 流式实时进度）
            self._set(tag, phase=f"下载中 0%", dl_progress=0, cur_speed=0)
            self._log(tag, f"下载测速 ({self.dl_bytes // 1048576}MB)…")
            min_valid = max(500_000, self.dl_bytes // 4)  # 至少下到 1/4 才算有效（防截断）
            spd, code, dl, ul = 0, "000", 0, 0
            peak_mbps, avg_mbps = 0, 0
            used_url = ""
            for tpl in SPEEDTEST_URLS:
                if "{bytes}" in tpl:
                    url = tpl.format(bytes=self.dl_bytes)
                else:
                    url = tpl  # 固定大小源
                src_name = url.split("/")[2]
                self._log(tag, f"尝试测速源: {src_name}")
                spd, code, dl, ul, peak_mbps, avg_mbps = self._curl_stream(proxy, url, self.dl_bytes, self.timeout, tag)
                self._log(tag, f"  {src_name}: code={code} dl={dl//1024}KB 均{avg_mbps}M/峰{peak_mbps}M")
                # 有效判定：下到足够数据（非 1 字节截断）
                if dl >= min_valid and spd > 0:
                    used_url = src_name
                    break
                self._set(tag, phase=f"下载中 0%", dl_progress=0, cur_speed=0)
            if dl < min_valid or spd == 0:
                # 失败也记录历史（便于判断节点稳定性）
                fail_reason = "所有测速源均被截断/失败"
                if dl > 0 and dl < min_valid:
                    fail_reason = f"下载被截断(仅{dl//1024}KB/{self.dl_bytes//1024}KB)"
                elif code == "000":
                    fail_reason = "下载连接超时/无响应"
                self._set(tag, status="error", phase="", error=fail_reason)
                self._log(tag, f"测速失败: {fail_reason}")
                self._record_fail(node, fail_reason)
                return
            with self.lock:
                self.total_dl += dl
                self.total_ul += ul
            mbps = spd * 8 / 1_000_000
            MBps = spd / (1024 * 1024)
            self._set(tag, status="done", phase="", speed_mbps=round(mbps, 1),
                      speed_MBps=round(MBps, 2), peak_speed=peak_mbps, avg_speed=avg_mbps,
                      dl_bytes=dl, ul_bytes=ul, dl_progress=100)
            self._log(tag, f"完成: 均{avg_mbps} 峰{peak_mbps} Mbps ({MBps:.2f} MB/s) [{used_url}], 流量 ↓{dl//1024}KB")
            # 记录历史（成功）—— history key 用 node["id"]，访问当前 results 用 tag
            nid = node["id"]
            rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "speed_mbps": round(mbps, 1),
                   "speed_MBps": round(MBps, 2), "avg_speed": avg_mbps, "peak_speed": peak_mbps,
                   "latency_ms": self.results[tag].get("latency_ms", 0),
                   "ip": self.results[tag].get("ip", ""), "ip_geo": self.results[tag].get("ip_geo", ""),
                   "dl_bytes": dl, "source": used_url, "result": "ok",
                   "id": nid, "tag": node["tag"], "type": node["type"],
                   "server": node["server"], "port": node["port"]}
            with self.lock:
                self.history.setdefault(nid, []).append(rec)
                if len(self.history[nid]) > 50:
                    self.history[nid] = self.history[nid][-50:]
                self._save_history()
        except Exception as e:
            self._set(tag, status="error", phase="", error=str(e)[:100])
            self._log(tag, f"异常: {e}")
        finally:
            if proc:
                proc.terminate()
                try: proc.wait(timeout=3)
                except Exception: proc.kill()
            try: os.remove(cfg_path)
            except OSError: pass
            with self.lock:
                self.progress["done"] += 1

    def run_test(self, nodes):
        with self.lock:
            self.state = "testing"
            self.progress = {"done": 0, "total": len(nodes)}
            for n in nodes:
                self.results[n["tag"]]["status"] = "pending"
        for n in nodes:
            if self.state == "stopping":
                break
            with self.lock:
                self.current = n["tag"]
            self._test_one(n)
        with self.lock:
            self.state = "done" if self.state != "stopping" else "idle"
            self.current = None

    def stop(self):
        with self.lock:
            self.state = "stopping"


# ======================== Web 服务 ========================
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>sing-box 测速</title>
<style>
:root{--pri:#4f46e5;--pri-l:#6366f1;--pri-bg:#eef2ff;--ok:#10b981;--ok-bg:#ecfdf5;--warn:#f59e0b;--warn-bg:#fffbeb;--err:#ef4444;--err-bg:#fef2f2;--txt:#1e293b;--txt2:#64748b;--txt3:#94a3b8;--bd:#e2e8f0;--bg:#f1f5f9;--card:#fff;--sh:0 1px 3px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04);--sh-lg:0 10px 30px rgba(0,0,0,.1)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--txt);padding:16px;font-size:13px;line-height:1.5}
.hd{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.hd h1{font-size:20px;font-weight:700;background:linear-gradient(135deg,var(--pri),var(--pri-l));-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-.3px}
.hd .sub{font-size:11px;color:var(--txt3);background:var(--card);padding:3px 9px;border-radius:20px;border:1px solid var(--bd)}
.card{background:var(--card);border-radius:12px;padding:12px 16px;margin-bottom:12px;box-shadow:var(--sh);border:1px solid var(--bd)}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.bar input[type=text],input[type=number],select{padding:7px 11px;border:1px solid var(--bd);border-radius:8px;font-size:13px;background:#fff;transition:border-color .15s,box-shadow .15s}
.bar input[type=text]:focus,input[type=number]:focus,select:focus{outline:none;border-color:var(--pri);box-shadow:0 0 0 3px var(--pri-bg)}
.bar input[type=text]{width:170px}
.bar input[type=number]{width:62px}
.sep{width:1px;height:24px;background:var(--bd);margin:0 2px}
.lbl{font-size:12px;color:var(--txt2);display:inline-flex;align-items:center;gap:5px;cursor:pointer;font-weight:500}
.btn{padding:7px 16px;border:none;border-radius:8px;font-size:13px;cursor:pointer;font-weight:600;transition:all .15s;display:inline-flex;align-items:center;gap:5px}
.btn:active{transform:scale(.97)}
.btn-pri{background:var(--pri);color:#fff}.btn-pri:hover{background:#4338ca}
.btn-ok{background:var(--ok);color:#fff}.btn-ok:hover{background:#059669}
.btn-warn{background:var(--warn);color:#fff}.btn-warn:hover{background:#d97706}
.btn-danger{background:var(--err);color:#fff}.btn-danger:hover{background:#dc2626}
.btn-ghost{background:#f1f5f9;color:var(--txt2)}.btn-ghost:hover{background:#e2e8f0}
.btn-sm{padding:4px 10px;font-size:11px;border-radius:6px}
.btn:disabled{background:#cbd5e1!important;color:#fff!important;cursor:not-allowed;transform:none}
.stats{font-size:12px;color:var(--txt2)}.stats b{color:var(--pri)}
.flow{font-size:12px;color:var(--txt3);margin-left:auto;display:inline-flex;gap:10px}
.flow b{color:var(--ok);font-weight:700}
.cfg-tip{font-size:11px;color:var(--txt3)}
.prog-bar{height:5px;background:#e2e8f0;border-radius:3px;margin-top:10px;overflow:hidden}
.prog-bar>div{height:100%;background:linear-gradient(90deg,var(--pri),var(--pri-l));width:0;transition:width .3s;border-radius:3px}
.tbl-wrap{background:var(--card);border-radius:12px;overflow:auto;box-shadow:var(--sh);border:1px solid var(--bd);max-height:calc(100vh - 200px)}
table{width:100%;min-width:1220px;border-collapse:collapse}
thead{position:sticky;top:0;z-index:2}
th{background:#f8fafc;font-weight:600;color:var(--txt2);text-align:left;padding:10px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.4px;border-bottom:2px solid var(--bd);white-space:nowrap;cursor:pointer;user-select:none}
th:hover{background:#f1f5f9}
th.sort-asc::after{content:' ▲';color:var(--pri)}th.sort-desc::after{content:' ▼';color:var(--pri)}
td{padding:9px 12px;border-bottom:1px solid #f1f5f9;font-size:12.5px;vertical-align:middle}
tbody tr{transition:background .1s;cursor:default}
tbody tr:hover{background:#f8fafc}
tbody tr.sel{background:var(--pri-bg)}
tbody tr.sel:hover{background:#e0e7ff}
td.tag-cell{max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tag-name{font-weight:500;color:var(--txt);cursor:pointer}
.tag-name:hover{color:var(--pri)}
/* 行级勾选：整行可点+复选框放大 */
.ck-cell{width:36px;text-align:center}
.ck-box{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:5px;border:2px solid #cbd5e1;cursor:pointer;transition:all .15s;background:#fff;vertical-align:middle}
.ck-box.on{background:var(--pri);border-color:var(--pri)}
.ck-box.on::after{content:'';width:6px;height:11px;border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg);margin-top:-2px}
.badge{font-size:10px;padding:2px 7px;border-radius:6px;font-weight:600;white-space:nowrap;display:inline-block}
.b-pri{background:var(--pri-bg);color:var(--pri)}.b-warn{background:var(--warn-bg);color:#b45309}.b-ok{background:var(--ok-bg);color:#047857}.b-gray{background:#f1f5f9;color:var(--txt2)}
.pill{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;white-space:nowrap}
.p-testing{background:var(--pri-bg);color:var(--pri)}.p-pending{background:#f1f5f9;color:var(--txt3)}
.p-done{background:var(--ok-bg);color:var(--ok)}.p-error{background:var(--err-bg);color:var(--err)}
.phase{font-size:10px;color:var(--pri);margin-top:3px;font-weight:500}
/* 延迟颜色分级 */
.lat{font-weight:600;font-variant-numeric:tabular-nums}
.lat-0{color:var(--txt3)}      /* 未测 */
.lat-fast{color:var(--ok)}     /* <200 */
.lat-good{color:#0ea5e9}       /* <500 */
.lat-slow{color:var(--warn)}   /* <1000 */
.lat-bad{color:var(--err)}     /* >=1000 */
.lat-fail{color:var(--err)}    /* 失败 */
.spd-bar{background:#e2e8f0;border-radius:4px;height:18px;min-width:80px;overflow:hidden;position:relative}
.spd-bar>i{display:block;height:100%;background:linear-gradient(90deg,#34d399,#10b981);border-radius:4px;font-style:normal;font-size:10px;color:#fff;text-align:center;line-height:18px;padding:0 3px;min-width:22px;transition:width .3s;font-weight:600}
.spd-bar.fast>i{background:linear-gradient(90deg,var(--pri),var(--pri-l))}
.spd-bar.fail>i{background:var(--err)}
.spin{display:inline-block;width:12px;height:12px;border:2.5px solid var(--pri-bg);border-top-color:var(--pri);border-radius:50%;animation:sp .7s linear infinite;vertical-align:middle}
@keyframes sp{to{transform:rotate(360deg)}}
.ip-cell{font-family:Consolas,monospace;font-size:11.5px;color:var(--txt2);white-space:nowrap}
.ip-geo{font-size:10px;color:var(--txt3);margin-top:2px;max-width:160px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;line-height:1.3;word-break:break-word;cursor:help}
.lat[data-samples]{cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.modal-bg{position:fixed;inset:0;background:rgba(15,23,42,.5);backdrop-filter:blur(2px);display:none;z-index:100;align-items:center;justify-content:center;padding:20px}
.modal-bg.show{display:flex}
.modal{background:var(--card);border-radius:16px;width:620px;max-width:100%;max-height:90vh;display:flex;flex-direction:column;box-shadow:var(--sh-lg);animation:pop .2s}
@keyframes pop{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
.modal-h{padding:16px 20px;border-bottom:1px solid var(--bd);font-weight:700;font-size:15px;display:flex;justify-content:space-between;align-items:center}
.modal-b{padding:16px 20px;overflow-y:auto;flex:1}
.logline{font-family:Consolas,Monaco,monospace;font-size:11.5px;color:var(--txt2);line-height:1.8;white-space:pre-wrap}
.logline.err{color:var(--err)}.logline.ok{color:var(--ok)}
.kv{display:flex;gap:10px;margin-bottom:8px;font-size:12.5px;align-items:baseline}
.kv span{color:var(--txt3);min-width:76px;flex-shrink:0}.kv b{color:var(--txt);font-weight:600;word-break:break-all}
.hist-tbl{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
.hist-tbl th{background:#f8fafc;padding:8px 10px;font-size:10px;text-align:left;border-bottom:1px solid var(--bd);text-transform:none;letter-spacing:0;cursor:default}
.hist-tbl th:hover{background:#f8fafc}
.hist-tbl td{padding:8px 10px;border-bottom:1px solid #f1f5f9}
.hist-tbl tr:hover td{background:#f8fafc}
.hist-now{background:var(--pri-bg)!important}
.hist-fail td{color:var(--err)}
.empty{text-align:center;color:var(--txt3);padding:24px;font-size:13px}
.tag-row{display:flex;align-items:center;gap:8px}
</style></head><body>
<div class="hd"><h1>sing-box 测速</h1><span class="sub">实时下载 · 多源降级 · 历史对比 · 出口定位</span></div>
<div class="card bar">
  <input type="text" id="f" placeholder="🔍 筛选节点名" oninput="rend()">
  <select id="sf" onchange="rend()"><option value="">全部订阅</option></select>
  <select id="ty" onchange="rend()"><option value="">全部协议</option><option>vless</option><option>trojan</option><option>hysteria2</option><option>shadowsocks</option><option>vmess</option></select>
  <label class="lbl"><input type="checkbox" id="os" onchange="rend()" style="width:auto">仅选中</label>
  <span class="sep"></span>
  <label class="lbl">下载<input type="number" id="cfgMB" value="10" min="1" max="500" onchange="saveCfg()">MB</label>
  <label class="lbl">超时<input type="number" id="cfgTO" value="25" min="5" max="120" onchange="saveCfg()">s</label>
  <label class="lbl">Ping<input type="number" id="cfgPC" value="5" min="1" max="20" onchange="saveCfg()">次</label>
  <span class="cfg-tip" id="cfgUrl"></span>
  <span class="sep"></span>
  <button class="btn btn-ghost" id="bReload" onclick="reloadCfg()">↻ 重新载入</button>
  <button class="btn btn-ok" id="bPing" onclick="doPing('sb')">⚡ Ping延迟</button>
  <button class="btn btn-warn" id="bTcp" onclick="doPing('tcp')">🔌 TCP探测</button>
  <button class="btn btn-pri" id="bGo" onclick="startTest()">🚀 开始测速</button>
  <button class="btn btn-danger" id="bStop" onclick="stopAll()" disabled>⏹ 停止</button>
  <span class="stats" id="st"></span>
  <span class="flow">本次流量 <b>↓<span id="fdl">0</span></b> <b>↑<span id="ful">0</span></b></span>
</div>
<div class="prog-bar"><div id="pg"></div></div>
<div class="tbl-wrap">
<table><thead><tr>
<th class="ck-cell"><span class="ck-box" id="all" onclick="tglAll()"></span></th>
<th onclick="sort('tag')">节点</th>
<th onclick="sort('subgroup')">订阅</th>
<th onclick="sort('type')">协议</th>
<th onclick="sort('latency_ms')">延迟</th>
<th onclick="sort('tcp_ping')">TCP</th>
<th onclick="sort('speed_mbps')">下载速度</th>
<th onclick="sort('ip')">出口IP / 位置</th>
<th>操作</th>
<th>状态</th>
</tr></thead><tbody id="tb"></tbody></table>
</div>
<div class="modal-bg" id="mbg" onclick="if(event.target===this)closeModal()">
<div class="modal"><div class="modal-h"><span id="mtitle">详情</span><button class="btn btn-ghost btn-sm" onclick="closeModal()">✕ 关闭</button></div>
<div class="modal-b" id="mbody"></div></div></div>
<script>
let nodes=[],results={},selected=new Set(),state='idle',cur=null,prog={done:0,total:0},sortKey='',sortDir='desc',userSorted=false,flow={dl:0,ul:0},cfg={dl_bytes:10485760,timeout:25,ping_count:5};
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtSpd=m=>m>0?m.toFixed(1)+' Mbps':'-';
const fmtBytes=b=>{if(!b)return '0';if(b<1048576)return (b/1024).toFixed(0)+'KB';return (b/1048576).toFixed(2)+'MB'};
// 延迟颜色分级（未测灰/快绿/中蓝/慢黄/差红/失败红）
function latCls(m){if(m<0)return 'lat-fail';if(m===0)return 'lat-0';if(m<200)return 'lat-fast';if(m<500)return 'lat-good';if(m<1000)return 'lat-slow';return 'lat-bad';}
// 延迟显示：有样本时点击可查看多次结果（用 data 属性，事件委托，避免拼接注入）
function fmtLat(m,samples){if(m<0)return '<span class="lat lat-fail">失败</span>';if(m===0)return '<span class="lat lat-0">—</span>';
  const hasSm=samples&&samples.length>1;
  const attrs=hasSm?(' data-samples="'+samples.join(',')+'"'):'';return '<span class="lat '+latCls(m)+'"'+attrs+'>'+(hasSm?'<u>':'')+m+' ms'+(hasSm?'</u>':'')+'</span>';}
function latDetail(csv){const arr=csv.split(',').map(Number);const mn=Math.min(...arr),mx=Math.max(...arr),avg=Math.round(arr.reduce((a,b)=>a+b,0)/arr.length);
  const bars=arr.map((v)=>{const h=Math.max(4,Math.min(56,v/mx*56));const cls=latCls(v);return '<div style="display:inline-flex;flex-direction:column;align-items:center;margin-right:10px"><div style="height:56px;display:flex;align-items:flex-end"><div class="lat '+cls+'" style="width:22px;height:'+h+'px;background:currentColor;opacity:.65;border-radius:3px"></div></div><div style="font-size:11px;margin-top:4px;color:var(--txt2)">'+v+'ms</div></div>';}).join('');
  showTip('延迟样本（'+arr.length+'次）','最小 <b class="lat '+latCls(mn)+'">'+mn+'ms</b> · 最大 <b class="lat '+latCls(mx)+'">'+mx+'ms</b> · 平均 <b>'+avg+'ms</b><div style="margin-top:14px;display:flex;align-items:flex-end">'+bars+'</div>');}
function showTip(title,body){$('mtitle').textContent=title;$('mbody').innerHTML=body;$('mbg').classList.add('show');}
const subBadge=s=>{const cls=['b-pri','b-warn','b-ok','b-gray'];let h=0;for(let i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))>>>0;return '<span class="badge '+cls[h%4]+'">'+esc(s)+'</span>';};
function vis(){const f=$('f').value.toLowerCase(),sf=$('sf').value,ty=$('ty').value;
  return nodes.filter(n=>(!f||n.tag.toLowerCase().includes(f))&&(!sf||n.subgroup===sf)&&(!ty||n.type===ty));}
function tglAll(){const vs=vis();const allSel=vs.length>0&&vs.every(n=>selected.has(n.tag));vs.forEach(n=>{if(allSel)selected.delete(n.tag);else selected.add(n.tag);});rend();}
function sort(k){if(sortKey===k)sortDir=sortDir==='asc'?'desc':'asc';else{sortKey=k;sortDir=(k==='tag'||k==='subgroup'||k==='type'||k==='ip')?'asc':'desc';}userSorted=true;rend();}
function rend(){
  // 表头排序标记
  document.querySelectorAll('th').forEach(th=>{th.classList.remove('sort-asc','sort-desc');});
  if(sortKey){const thIdx={tag:1,subgroup:2,type:3,latency_ms:4,tcp_ping:5,speed_mbps:6,ip:7}[sortKey];if(thIdx){const th=document.querySelectorAll('th')[thIdx];th.classList.add(sortDir==='asc'?'sort-asc':'sort-desc');}}
  const os=$('os').checked;let list=vis();
  if(os)list=list.filter(n=>selected.has(n.tag));
  // 仅当用户主动点表头排序时才排序，测速中不自动重排
  if(userSorted&&sortKey){
  list.sort((a,b)=>{const ra=results[a.tag]||{},rb=results[b.tag]||{};let va,vb;
    if(sortKey==='speed_mbps'){va=ra.speed_mbps||0;vb=rb.speed_mbps||0;}
    else if(sortKey==='latency_ms'){va=ra.latency_ms>0?ra.latency_ms:99999;vb=rb.latency_ms>0?rb.latency_ms:99999;}
    else if(sortKey==='tcp_ping'){va=ra.tcp_ping>0?ra.tcp_ping:99999;vb=rb.tcp_ping>0?rb.tcp_ping:99999;}
    else if(sortKey==='ip'){va=ra.ip||'';vb=rb.ip||'';}
    else if(sortKey==='subgroup'){va=a.subgroup;vb=b.subgroup;}
    else if(sortKey==='type'){va=a.type;vb=b.type;}
    else{va=a.tag;vb=b.tag;}
    if(typeof va==='string')return sortDir==='asc'?va.localeCompare(vb):vb.localeCompare(va);
    return sortDir==='asc'?va-vb:vb-va;});
  }
  const mx=Math.max(1,...Object.values(results).map(r=>r.speed_mbps||0));
  $('tb').innerHTML=list.map((n,idx)=>{const r=results[n.tag]||{status:'pending'};
    const sel=selected.has(n.tag);
    const testing=r.status==='testing';
    let spdPct=r.speed_mbps?Math.min(100,r.speed_mbps/mx*100):0;
    let dlPct=r.dl_progress||0;
    const fast=r.speed_mbps>mx*0.5?' fast':'';
    const failBar=r.status==='error'?' fail':'';
    let barW=testing?(dlPct||0):spdPct;
    let barTxt=testing?(r.cur_speed>0?r.cur_speed+'M':(dlPct>0?dlPct.toFixed(0)+'%':'…')):(r.speed_mbps?spdPct.toFixed(0)+'%':(r.status==='error'?'✕':''));
    let stTxt=r.status,pill='p-'+r.status,phs='';
    if(stTxt==='testing'){stTxt='<span class="spin"></span>测速中';if(r.phase)phs='<div class="phase">'+esc(r.phase)+'</div>';}
    else if(stTxt==='pending')stTxt='待测';else if(stTxt==='done')stTxt='✓ 完成';else if(stTxt==='error')stTxt='✕ 失败';
    // 速度格：测试中显瞬时，完成显 均/峰/流量
    let spdTxt;
    if(testing){spdTxt=r.cur_speed>0?'<b style="color:var(--pri)">'+r.cur_speed+'</b> Mbps':(r.phase||'…');}
    else if(r.speed_mbps>0){spdTxt='<b>'+r.speed_mbps+'</b> Mbps';if(r.peak_speed||r.avg_speed){spdTxt+=' <span style="color:var(--txt3);font-size:10px">(均'+(r.avg_speed||r.speed_mbps)+'/峰'+(r.peak_speed||r.speed_mbps)+')</span>';}if(r.dl_bytes)spdTxt+=' <span style="color:var(--ok);font-size:10px">↓'+fmtBytes(r.dl_bytes)+'</span>';}
    else{spdTxt=r.status==='error'?'<span style="color:var(--err);font-size:11px">'+esc(r.error||'失败').slice(0,20)+'</span>':'—';}
    // IP格：不截断，允许换行，点击/悬停看完整信息
    const ipHtml=r.ip?'<div class="ip-cell">'+esc(r.ip)+'</div>'+(r.ip_geo?'<div class="ip-geo" title="'+esc(r.ip_geo)+'">'+esc(r.ip_geo)+'</div>':''):'<span style="color:var(--txt3)">—</span>';
    return '<tr class="'+(sel?'sel':'')+'" data-tag="'+esc(n.tag)+'">'+
      '<td class="ck-cell"><span class="ck-box'+(sel?' on':'')+'" data-act="ck"></span></td>'+
      '<td class="tag-cell"><span class="tag-name" data-act="detail" title="'+esc(n.tag)+'">'+esc(n.tag)+'</span></td>'+
      '<td>'+subBadge(n.subgroup)+'</td>'+
      '<td><span class="badge b-gray">'+n.type+'</span></td>'+
      '<td>'+fmtLat(r.latency_ms||0,r.latency_samples)+'</td><td>'+fmtLat(r.tcp_ping||0,r.tcp_samples)+'</td>'+
      '<td><div class="spd-bar'+fast+failBar+'"><i style="width:'+barW+'%">'+barTxt+'</i></div>'+
      '<div style="font-size:10px;color:var(--txt3);margin-top:3px">'+spdTxt+'</div></td>'+
      '<td>'+ipHtml+'</td>'+
      '<td><button class="btn btn-ghost btn-sm" data-act="detail">详情</button> '+
      '<button class="btn btn-ghost btn-sm" data-act="history">历史</button></td>'+
      '<td><span class="pill '+pill+'">'+stTxt+'</span>'+phs+'</td></tr>';}).join('')||'<tr><td colspan="10" class="empty">无匹配节点</td></tr>';
  // 全选框状态
  const vs=vis();const allSel=vs.length>0&&vs.every(n=>selected.has(n.tag));
  $('all').classList.toggle('on',allSel);
  const dn=Object.values(results).filter(r=>r.status==='done').length;
  const er=Object.values(results).filter(r=>r.status==='error').length;
  const avg=dn?(Object.values(results).filter(r=>r.status==='done').reduce((s,r)=>s+(r.speed_mbps||0),0)/dn).toFixed(1):0;
  $('st').innerHTML='共 '+nodes.length+' · 选 <b>'+selected.size+'</b> · 成功 <b>'+dn+'</b> · 失败 '+er+' · 均 <b>'+avg+'</b>Mbps'+(prog.total?' · '+prog.done+'/'+prog.total:'');
  $('bGo').disabled=(state==='testing'||state==='pinging');$('bPing').disabled=(state==='testing'||state==='pinging');$('bTcp').disabled=(state==='testing'||state==='pinging');$('bReload').disabled=(state==='testing'||state==='pinging');
  $('bStop').disabled=(state==='idle'||state==='done'||state==='stopping');
  $('pg').style.width=prog.total?(prog.done/prog.total*100)+'%':'0%';
}
// 统一事件委托：处理行内所有交互（勾选/详情/历史/延迟样本）
document.getElementById('tb').addEventListener('click',function(e){
  const latEl=e.target.closest('.lat[data-samples]');
  if(latEl){latDetail(latEl.getAttribute('data-samples'));return;}
  const actEl=e.target.closest('[data-act]');
  if(!actEl)return;
  const tr=actEl.closest('tr');if(!tr)return;
  const tag=tr.getAttribute('data-tag');if(!tag)return;
  const act=actEl.getAttribute('data-act');
  if(act==='ck'){if(selected.has(tag))selected.delete(tag);else selected.add(tag);rend();}
  else if(act==='detail'){detail(tag);}
  else if(act==='history'){const nd=nodes.find(x=>x.tag===tag);history(nd?nd.id:'');}
});
async function saveCfg(){
  const mb=parseInt($('cfgMB').value)||10,to=parseInt($('cfgTO').value)||25,pc=parseInt($('cfgPC').value)||5;
  cfg.dl_bytes=mb*1048576;cfg.timeout=to;cfg.ping_count=pc;
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dl_bytes:cfg.dl_bytes,timeout:to,ping_count:pc})});
}
function detail(tag){const r=results[tag]||{};const n=nodes.find(x=>x.tag===tag)||{};
  $('mtitle').textContent=n.tag||tag;
  const logs=(r.log||[]).map(l=>{const cls=l.includes('失败')||l.includes('错误')||l.includes('异常')?'err':(l.includes('完成')||l.includes('OK')?'ok':'');return '<div class="logline '+cls+'">'+esc(l)+'</div>';}).join('');
  $('mbody').innerHTML='<div class="kv"><span>节点</span><b>'+esc(n.tag||tag)+'</b></div>'+
    '<div class="kv"><span>服务器</span><b>'+esc(n.server||'-')+':'+esc(n.port||'-')+'</b></div>'+
    '<div class="kv"><span>订阅</span><b>'+esc(n.subgroup||'-')+'</b></div>'+
    '<div class="kv"><span>协议</span><b>'+(n.type||'-')+'</b></div>'+
    '<div class="kv"><span>延迟</span><b>'+fmtLat(r.latency_ms||0)+'</b></div>'+
    '<div class="kv"><span>TCP探测</span><b>'+fmtLat(r.tcp_ping||0)+'</b></div>'+
    '<div class="kv"><span>下载速度</span><b>'+fmtSpd(r.speed_mbps||0)+(r.speed_MBps?' ('+r.speed_MBps+' MB/s)':'')+'</b></div>'+
    ((r.peak_speed||r.avg_speed)?'<div class="kv"><span>均速/峰值</span><b>'+(r.avg_speed||r.speed_mbps||0)+' / '+(r.peak_speed||0)+' Mbps</b></div>':'')+
    '<div class="kv"><span>出口IP</span><b>'+(r.ip||'-')+'</b></div>'+
    (r.ip_geo?'<div class="kv"><span>位置/ISP</span><b>'+esc(r.ip_geo)+'</b></div>':'')+
    '<div class="kv"><span>流量</span><b>↓'+fmtBytes(r.dl_bytes||0)+' ↑'+fmtBytes(r.ul_bytes||0)+'</b></div>'+
    '<div class="kv"><span>状态</span><b>'+(r.status||'-')+(r.error?' · <span style="color:var(--err)">'+esc(r.error)+'</span>':'')+'</b></div>'+
    '<hr style="margin:10px 0;border:none;border-top:1px solid var(--bd)"><div style="font-weight:700;margin-bottom:8px">📋 阶段日志</div>'+logs;
  $('mbg').classList.add('show');}
async function history(id){const n=nodes.find(x=>x.id===id)||{};const r=results[n.tag]||{};
  $('mtitle').textContent='历史 · '+(n.tag||id);
  const rr=await(await fetch('/api/history?id='+encodeURIComponent(id))).json();
  const recs=rr.records||[];
  // 节点快照副标题（若有 type/server/port）
  const snap=(n.type||n.server||n.port)?((n.type||'')+' · '+(n.server||'')+':'+(n.port||'')):'';
  let html='<div class="kv"><span>总记录</span><b>'+recs.length+' 次</b> <span style="color:var(--ok)">成功 '+recs.filter(x=>x.result!=='fail').length+'</span> <span style="color:var(--err)">失败 '+(recs.length-recs.filter(x=>x.result!=='fail').length)+'</span></div>';
  if(snap)html+='<div class="kv"><span>节点</span><b style="font-size:11px;color:var(--txt3)">'+esc(snap)+'</b></div>';
  const okRecs=recs.filter(x=>x.result!=='fail');
  if(okRecs.length){
    const speeds=okRecs.map(x=>x.speed_mbps);
    const avg=(speeds.reduce((a,b)=>a+b,0)/speeds.length).toFixed(1);
    const mx=Math.max(...speeds),mn=Math.min(...speeds);
    html+='<div class="kv"><span>速度范围</span><b>'+mn+' ~ '+mx+' Mbps（均 '+avg+'）</b></div>';
    if(okRecs.length>=2){const last=okRecs[okRecs.length-1].speed_mbps,prev=okRecs[okRecs.length-2].speed_mbps;const diff=(last-prev).toFixed(1);
      html+='<div class="kv"><span>较上次</span><b style="color:'+(diff>=0?'var(--ok)':'var(--err)')+'">'+(diff>=0?'↑ +':'↓ ')+diff+' Mbps</b></div>';}
  }
  html+='<hr style="margin:10px 0;border:none;border-top:1px solid var(--bd)">';
  if(recs.length){
    html+='<table class="hist-tbl"><thead><tr><th>时间</th><th>结果</th><th>速度</th><th>延迟</th><th>出口IP</th><th>位置</th></tr></thead><tbody>';
    recs.slice().reverse().forEach((x,i)=>{
      const isNow=(i===0&&(r.status==='done'||r.status==='error'));
      const fail=x.result==='fail';
      html+='<tr'+(isNow?' class="hist-now"':'')+(fail?' class="hist-fail"':'')+'>'+
        '<td>'+esc(x.time).slice(5)+'</td>'+
        '<td>'+(fail?'<span style="color:var(--err)">✕ 失败</span>':'<span style="color:var(--ok)">✓ 成功</span>')+'</td>'+
        '<td>'+(fail?'-':'<b>'+x.speed_mbps+'</b> Mbps')+'</td>'+
        '<td>'+(x.latency_ms||'-')+'ms</td>'+
        '<td style="font-family:monospace;font-size:11px">'+(x.ip||'-')+'</td>'+
        '<td style="font-size:10px;color:var(--txt3)">'+(fail?esc((x.error||'').slice(0,16)):(x.ip_geo||esc((x.source||'').slice(0,12))))+'</td></tr>';
    });
    html+='</tbody></table>';
  }else{html+='<div class="empty">暂无历史记录</div>';}
  $('mbody').innerHTML=html;
  $('mbg').classList.add('show');}
function closeModal(){$('mbg').classList.remove('show');}
async function api(path,opts){const r=await fetch(path,opts);return r.json();}
async function load(){const d=await api('/api/init');nodes=d.nodes;results=d.results;state=d.state;
  const sf=$('sf');while(sf.options.length>1)sf.remove(1);(d.subgroups||[]).forEach(s=>{const o=document.createElement('option');o.value=s;o.textContent=s;sf.appendChild(o);});
  flow=d.flow;
  const c=await api('/api/config');cfg=c;$('cfgMB').value=Math.round(c.dl_bytes/1048576);$('cfgTO').value=c.timeout;$('cfgPC').value=c.ping_count||5;
  $('cfgUrl').textContent='🔄 '+(c.speedtest_urls||[]).length+'源自动降级';
  rend();if(state!=='idle')poll();}
let pt=null;
async function poll(){clearInterval(pt);pt=setInterval(async()=>{const d=await api('/api/state');results=d.results;state=d.state;cur=d.current;prog=d.progress;flow=d.flow;$('fdl').textContent=fmtBytes(flow.dl);$('ful').textContent=fmtBytes(flow.ul);rend();if(state==='idle'||state==='done'){clearInterval(pt);pt=null;}},400);}
async function doPing(m){const ts=vis().filter(n=>selected.has(n.tag)).map(n=>n.tag);if(!ts.length){alert('请先勾选节点');return;}await saveCfg();const pc=parseInt($('cfgPC').value)||5;await api('/api/ping',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tags:ts,mode:m,count:pc})});poll();}
async function startTest(){const ts=vis().filter(n=>selected.has(n.tag)).map(n=>n.tag);if(!ts.length){alert('请先勾选要测速的节点');return;}await saveCfg();await api('/api/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tags:ts})});poll();}
async function stopAll(){await api('/api/stop',{method:'POST'});}
async function reloadCfg(){if(state==='testing'||state==='pinging'){alert('测速进行中，请先停止');return;}const r=await api('/api/reload',{method:'POST'});if(r&&r.ok){selected=new Set();await load();}else{alert((r&&r.error)||'重载失败');}}
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    tester = None  # type: ignore[assignment]

    def log_message(self, format, *args):
        pass

    def _send(self, code, ct, data):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        t = self.tester
        if self.path == "/":
            self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
        elif self.path == "/api/init":
            subs = sorted(set(n["subgroup"] for n in t.nodes))
            self._json(200, {
                "nodes": [{"id": n["id"], "tag": n["tag"], "type": n["type"], "subgroup": n["subgroup"],
                           "server": n["server"], "port": n["port"]} for n in t.nodes],
                "results": t.results, "state": t.state,
                "subgroups": subs,
                "flow": {"dl": t.total_dl, "ul": t.total_ul},
            })
        elif self.path == "/api/state":
            self._json(200, {
                "results": t.results, "state": t.state, "current": t.current,
                "progress": t.progress, "flow": {"dl": t.total_dl, "ul": t.total_ul},
            })
        elif self.path == "/api/config":
            self._json(200, {"dl_bytes": t.dl_bytes, "timeout": t.timeout,
                             "ping_count": t.ping_count, "speedtest_urls": SPEEDTEST_URLS})
        elif self.path.startswith("/api/history"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            node_id = q.get("id", [""])[0]
            if node_id:
                self._json(200, {"records": t.history.get(node_id, [])})
            else:
                self._json(200, {"history": t.history})
        else:
            self.send_error(404)

    def do_POST(self):
        t = self.tester
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        if self.path == "/api/config":
            try:
                if "dl_bytes" in data:
                    t.dl_bytes = max(1_000_000, int(data["dl_bytes"]))
                if "timeout" in data:
                    t.timeout = max(5, int(data["timeout"]))
                if "ping_count" in data:
                    t.ping_count = max(1, min(20, int(data["ping_count"])))
            except Exception:
                pass
            self._json(200, {"ok": True, "dl_bytes": t.dl_bytes, "timeout": t.timeout, "ping_count": t.ping_count})
            return
        if self.path == "/api/ping":
            if t.state in ("pinging", "testing"):
                self._json(409, {"error": "忙碌中"}); return
            tags = data.get("tags", [])
            mode = data.get("mode", "sb")
            count = data.get("count") or t.ping_count
            ns = [n for n in t.nodes if n["tag"] in tags]
            if not ns:
                self._json(400, {"error": "未选中节点"}); return
            threading.Thread(target=t.ping_all, args=(ns, mode, count), daemon=True).start()
            self._json(200, {"ok": True, "count": len(ns)})
            return
        elif self.path == "/api/test":
            if t.state in ("pinging", "testing"):
                self._json(409, {"error": "忙碌中"}); return
            tags = data.get("tags", [])
            ns = [n for n in t.nodes if n["tag"] in tags]
            if not ns:
                self._json(400, {"error": "未选中节点"}); return
            threading.Thread(target=t.run_test, args=(ns,), daemon=True).start()
            self._json(200, {"ok": True, "count": len(ns)})
            return
        elif self.path == "/api/stop":
            t.stop()
            self._json(200, {"ok": True})
            return
        elif self.path == "/api/reload":
            if t.state in ("pinging", "testing"):
                self._json(409, {"error": "测速进行中，请先停止"}); return
            try:
                t.reload()
            except Exception as e:
                self._json(500, {"error": f"重载失败: {e}"}); return
            self._json(200, {"ok": True, "count": len(t.nodes)})
            return
        else:
            self.send_error(404)


def run_cli(tester, flt):
    ns = tester.nodes
    if flt:
        kw = flt.lower()
        ns = [n for n in ns if kw in n["tag"].lower()]
    if not ns:
        print("无匹配节点"); return
    print(f"\n先对 {len(ns)} 个节点做 ping 测试…\n")
    tester.ping_all(ns, "sb")
    print(f"\n{'节点':<40} {'订阅':<8} {'延迟':>7}")
    print("-" * 62)
    for n in ns:
        r = tester.results[n["tag"]]
        print(f"{n['tag'][:38]:<40} {n['subgroup'][:6]:<8} {fmt_cli(r['latency_ms'])}")
    print("\n开始下载测速…\n")
    tester.run_test(ns)
    done = [(n["tag"], n["subgroup"], tester.results[n["tag"]]) for n in ns if tester.results[n["tag"]]["status"] == "done"]
    done.sort(key=lambda x: x[2]["speed_mbps"], reverse=True)
    print("\n" + "=" * 80 + "\n下载速度排名:\n" + "=" * 80)
    for i, (tag, sub, r) in enumerate(done, 1):
        print(f"{i:>3}. {tag[:38]:<40} {r['speed_mbps']:>7.1f} Mbps  {r['ip'] or '':<15} {sub}")
    print(f"\n本次总流量: ↓{tester.total_dl // 1048576}MB ↑{tester.total_ul // 1048576}MB")


def fmt_cli(ms):
    return f"{ms}ms" if ms > 0 else ("失败" if ms < 0 else "-")


def main():
    args = parse_args()
    if sys.platform == "win32":
        for s in (sys.stdout, sys.stderr):
            try: s.reconfigure(encoding="utf-8")
            except Exception: pass
    core = resolve_core_path(args.core, args.config_dir)
    if not core or not os.path.exists(core):
        print("找不到配置文件。请将 service_core.json 放入项目 config/ 目录，或用 --core / --config-dir 指定。")
        sys.exit(1)
    sub = resolve_sub_path(args.subscribe, args.config_dir)
    sb = args.singbox or find_singbox()
    if not sb or not os.path.exists(sb):
        print(f"找不到 sing-box: {sb}"); sys.exit(1)
    history_path = resolve_history_path(args.history)
    print(f"配置: {core}\n订阅: {sub}\nsing-box: {sb}\n历史: {history_path}")
    t = SpeedTest(core, sub, sb, args.bytes, args.timeout, history_path)
    print(f"已加载 {len(t.nodes)} 个节点，订阅分组: {sorted(set(n['subgroup'] for n in t.nodes))}")
    if args.cli:
        run_cli(t, args.filter); return
    Handler.tester = t
    # 阶段 0 / Section 16.1 & 17.6：在引入阶段 5 的 CSRF/Origin/token/敏感字段权限前，
    # 仅允许回环地址监听，避免将未认证的 Web 接口暴露到局域网。
    # TODO(阶段 5): 实现认证与安全设计后，可解除此限制。
    if not _is_loopback(args.host):
        print("拒绝启动：非回环监听地址 '%s' 需要阶段 5 的认证与安全设计（CSRF/Origin/token/敏感字段权限），"
              "当前版本仅允许 127.0.0.1。" % args.host)
        sys.exit(1)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    # Section 16.1：启动日志只显示实际绑定地址，不打印带敏感信息的 URL（不再展示 LAN 地址）
    print(f"\n✓ Web 界面: http://{args.host}:{args.port}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        t.stop(); srv.shutdown()


if __name__ == "__main__":
    main()
