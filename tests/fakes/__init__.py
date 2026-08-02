"""测试替身 (fakes) 子包。

提供两个纯 Python 的替身模块，避免在单测中真实调用 sing-box 二进制
或访问网络：

- :mod:`tests.fakes.fake_singbox`   —— 模拟 ``sing-box check`` 子进程
- :mod:`tests.fakes.fake_downloader` —— 模拟订阅 HTTP 下载器

两者均为确定性实现 (给定相同 scenario 返回相同结果)，便于编写可重复测试。
"""
