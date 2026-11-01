"""CLI 层测试：JSON 输出必须是**合法 JSON**。

这条看着琐碎，但它锁住一个真实踩过的坑：JSON 后面还跟着给人看的
「预览模式：……」摘要，整个 stdout 就不是合法 JSON 了，下游脚本
``json.loads`` 直接炸。README 承诺了「机器可读的计划」，就得守住。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from file_tool.cli import app

RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"


@pytest.fixture(scope="module")
def ruleset():
    from file_tool import engine
    return engine.load_ruleset(RULES)


@pytest.fixture
def library(tmp_path, ruleset):
    """造一个混合形态的小库：有番号目录、有平铺文件、有非番号目录。"""
    (tmp_path / "SDMM028C").mkdir()
    (tmp_path / "SDMM028C" / "SDMM028C.mp4").write_text("x")
    (tmp_path / "aavv38.xyz@MIDE004").mkdir()
    (tmp_path / "some random dir").mkdir()
    (tmp_path / "CLUB810.mp4").write_text("x")
    (tmp_path / "._CLUB810.mp4").write_text("x")
    return tmp_path


@pytest.mark.parametrize(
    "command,extra",
    [
        ("rename", ["--rules", str(RULES)]),
        ("rename-files", ["--rules", str(RULES)]),
        ("merge", ["--rules", str(RULES)]),
        ("archive", []),          # archive 不读规则集
        ("tidy", ["--rules", str(RULES)]),
    ],
)
def test_json_output_is_parseable(library, ruleset, command, extra):
    """每个支持 --json 的命令，stdout 都必须能被 json.loads 吃下。"""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [command, str(library), "--json", *extra],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)  # 解析失败就是测试失败
    assert "root" in payload
    assert "summary" in payload


def test_isolate_json_output_is_parseable(library, ruleset, tmp_path_factory):
    # 隔离目标必须在源目录**外面** —— 放在里面工具会直接拒绝（防目录嵌套）
    target = tmp_path_factory.mktemp("gc")
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["isolate", str(library), "--to", str(target), "--rules", str(RULES), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "items" in payload
    assert "unsafe" in payload


def test_clean_json_output_is_parseable(library):
    runner = CliRunner()
    result = runner.invoke(app, ["clean", str(library), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "items" in payload


def test_json_mode_does_not_apply_changes(library):
    """JSON 模式只是输出计划，绝不能顺手把文件改了。"""
    runner = CliRunner()
    before = sorted(p.name for p in library.iterdir())
    result = runner.invoke(
        app,
        ["rename", str(library), "--rules", str(RULES), "--json"],
    )
    assert result.exit_code == 0
    json.loads(result.stdout)
    assert sorted(p.name for p in library.iterdir()) == before


def test_relpath_in_json_uses_forward_slash(library, ruleset):
    """JSON 里的相对路径统一用 ``/``，跨平台一致。"""
    nested = library / "SDMM028C" / "子目录"
    nested.mkdir()
    (nested / "SDMM028C.mp4").write_text("x")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["rename-files", str(library), "--rules", str(RULES), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    relpaths = [item["relpath"] for item in payload["items"]]
    assert relpaths, "应该有可改名的文件"
    for relpath in relpaths:
        assert "\\" not in relpath, f"相对路径不该带反斜杠：{relpath}"
