"""pytest 全局配置。

其中一个钩子把失败摘要写成 **GitHub Actions 注释**。

GitHub 自带的 pytest problem matcher 认不出 ``FAILED tests/test_x.py::test_y``
这种带 ``::`` 的格式，结果就是 CI 页面上只有一句
「Process completed with exit code 1」—— 得翻日志才知道是哪个测试挂了。
写成注释之后，失败直接显示在 run 页面和 PR 的检查列表上。
"""
from __future__ import annotations

import os
import sys


def _first_line(text: str) -> str:
    """取第一行 —— 注释的值不能带换行，否则 workflow 命令会被截断。"""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


# 同一个 nodeid 只报一次 —— 终端摘要在一次运行里可能被触发多遍
_seen: set[str] = set()


def _sanitize(value: str) -> str:
    """把会破坏 workflow 命令格式的字符换掉。

    - ``::`` 是命令与消息之间的分隔符，出现在标题里会把消息切开
    - ``,`` 是属性的分隔符，出现在标题里会被当成下一个属性
    - 换行会把命令截断
    """
    return value.replace("::", " > ").replace(",", " ").replace("\n", " ").replace("\r", " ")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """失败 / 错误时输出 GitHub Actions 注释。

    只在 CI 里生效 —— 本地跑不需要这些噪音。
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return

def _failure_message(report) -> str:
    """取失败的原因（异常那行）。

    ``longreprtext`` 开头是源码和分隔线，不是原因；异常那行以 ``E `` 开头。
    ``reprcrash.message`` 更直接，但可能带换行，得截第一行。
    """
    longrepr = getattr(report, "longrepr", None)
    crash = getattr(longrepr, "reprcrash", None)
    message = getattr(crash, "message", "") if crash is not None else ""
    if message:
        return _first_line(message)
    for line in (getattr(report, "longreprtext", "") or "").splitlines():
        if line.startswith("E "):
            return line[2:].strip()
    return _first_line(getattr(report, "longreprtext", "") or "")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """失败 / 错误时输出 GitHub Actions 注释。

    只在 CI 里生效 —— 本地跑不需要这些噪音。

    直接写 ``sys.stdout`` 而不是用 ``terminalreporter.write``：
    后者会按终端宽度折行，折断的 workflow 命令不会被 runner 解析。
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return

    for kind, title in (("failed", "pytest"), ("error", "pytest error")):
        for report in terminalreporter.getreports(kind):
            nodeid = _sanitize(report.nodeid)
            if nodeid in _seen:
                continue
            _seen.add(nodeid)
            message = _sanitize(_failure_message(report))[:200]
            sys.stdout.write(f"::error title={title} {nodeid}::{message}\n")
    sys.stdout.flush()
