"""file-tool CLI。

常用命令：
  file-tool rename D:\\Media\\R              # 预览改名计划（dry-run）
  file-tool rename D:\\Media\\R --apply      # 真正执行改名
  file-tool scan D:\\Media\\R                # 只看识别结果统计
  file-tool rules show                      # 打印当前规则集
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
from pathlib import Path
from typing import Optional

import typer

from file_tool import engine
from file_tool import archive as archive_mod
from file_tool import clean as clean_mod
from file_tool import fscompat
from file_tool import isolate as isolate_mod
from file_tool import merge as merge_mod
from file_tool import rename_files as rename_files_mod
from file_tool import tidy as tidy_mod
from file_tool.adcut import boundaries as adcut_boundaries
from file_tool.adcut import cache as adcut_cache
from file_tool.adcut import cut as adcut_cut
from file_tool.adcut import detect as adcut_detect
from file_tool.adcut import fingerprint as adcut_fingerprint
from file_tool.adcut import templates as adcut_templates
from file_tool.adcut.models import Verdict
from file_tool.merge import MergeAction, MergeVerdict
from file_tool.models import Action, RuleSet
from file_tool.tidy import StepKind

DEFAULT_RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"
DEFAULT_AD_TEMPLATES = Path(__file__).resolve().parent.parent / "rules" / "ad_templates.yaml"
DEFAULT_AD_BOUNDARIES = Path(__file__).resolve().parent.parent / "rules" / "ad_boundaries.yaml"

app = typer.Typer(
    help="本地文件整理 CLI：批量重命名目录、番号规范化",
    no_args_is_help=True,
)
rules_app = typer.Typer(help="规则集管理", no_args_is_help=True)
app.add_typer(rules_app, name="rules")
adcut_app = typer.Typer(help="片头广告识别与裁剪", no_args_is_help=True)
app.add_typer(adcut_app, name="ad-cut")


def _force_utf8_output() -> None:
    """把标准输出/错误流切到 UTF-8。

    Windows 的控制台默认代码页是 GBK / cp437 / cp1252，中文输出会直接
    ``UnicodeEncodeError`` 崩掉整个 CLI（英文系统上是 cp437，中文根本
    编不出去）。这里强制 UTF-8，并对无法表示的字符降级为替代符 ——
    宁可显示成 ``?``，也不要让工具崩在输出阶段。

    **降级只在 Windows 上做。** POSIX（macOS / Linux）默认就是 UTF-8，
    而且那里的编码错误是真实问题（文件名里有无法表示的字符），
    静默替换成 ``?`` 会把问题藏起来 —— 用户看到的是一个合法的文件名，
    实际磁盘上却是另一个字节序列。因此 POSIX 上一个字节都不改。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if fscompat.on_windows():
                stream.reconfigure(encoding="utf-8", errors="replace")
            elif (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
                # POSIX 上只兜底编码，不动错误策略
                stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            # 被重定向到不支持 reconfigure 的流 —— 忽略，不影响主流程
            pass


def _report_unsafe(entries, noun: str) -> None:
    """报告「在 Windows 上碰不了」的文件名。

    这段话必须分平台说，因为两个平台上的事实不同：

    - **Windows 上**：这些条目**确实被跳过了** —— 名字非法，连 stat 都失败。
    - **macOS 上**：它们**照常处理了** —— 这些名字在 macOS 上完全合法，
      只是插到 Windows 上会变成碰不了的僵尸文件。

    早期版本两边都用同一句「已原地保留」，于是 macOS 上会一边把目录
    挪走了、一边告诉用户它留在原地 —— 报告和磁盘状态自相矛盾，
    用户据此判断「哪些文件能安全带去 Windows」会得到相反的结论。
    """
    if not entries:
        return
    sample = ", ".join(entries[:3])
    if fscompat.on_windows():
        typer.secho(
            f"Windows 上无法访问，已跳过（{len(entries)} 个）：{sample}",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.secho(
            f"另有 {len(entries)} 个{noun}在 Windows 上无法访问"
            "（本平台已照常处理；插到 Windows 上前建议先改名）：" + sample,
            fg=typer.colors.YELLOW,
        )


def _die(message: str) -> None:
    typer.secho(f"错误：{message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _load_rules(path: Optional[Path]) -> RuleSet:
    rules_path = path or DEFAULT_RULES
    if not rules_path.exists():
        _die(f"规则文件不存在：{rules_path}")
    try:
        return engine.load_ruleset(rules_path)
    except engine.RuleError as exc:
        _die(str(exc))


def _list_dirs(root: Path) -> list[str]:
    try:
        return sorted(p.name for p in root.iterdir() if p.is_dir())
    except OSError as exc:
        _die(f"无法读取目录 {root}: {exc}")
        return []


def _resolve_root(value: str) -> Path:
    root = Path(value).expanduser()
    if not root.exists():
        _die(f"目录不存在：{root}")
    if not root.is_dir():
        _die(f"不是目录：{root}")
    return root


@app.command()
def rename(
    root: str = typer.Argument(..., help="要整理的父目录"),
    apply: bool = typer.Option(False, "--apply", help="真正执行改名（默认只预览）"),
    rules: Optional[Path] = typer.Option(None, "--rules", help="规则文件路径"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出计划"),
) -> None:
    """生成并（可选）执行目录改名计划。"""
    root_dir = _resolve_root(root)
    ruleset = _load_rules(rules)
    names = _list_dirs(root_dir)
    plan = engine.build_plan(root_dir, ruleset, names)

    if json_out:
        typer.echo(json.dumps(
            {"root": plan.root, "summary": plan.summary(),
             "items": [i.model_dump(mode="json") for i in plan.items]},
            ensure_ascii=False, indent=2,
        ))
    else:
        _print_plan(plan)

    if not apply:
        if not json_out:
            counts = plan.summary()
            typer.secho(
                f"\n预览模式：{counts['rename']} 个待改名 / {counts['keep']} 个已是规范名 / "
                f"{counts['skip']} 个未匹配 / {counts['conflict']} 个冲突。"
                f" 确认无误后加 --apply 执行。",
                fg=typer.colors.YELLOW,
            )
        return

    done = engine.apply_plan(root_dir, plan)
    typer.secho(f"已改名 {len(done.renamed)} 个目录。", fg=typer.colors.GREEN)
    for old, new in done.renamed:
        typer.echo(f"  {old}  ->  {new}")
    if done.failed:
        typer.secho(
            f"失败 {len(done.failed)} 个（目录被占用或无权限）：", fg=typer.colors.RED
        )
        for old, reason in done.failed[:10]:
            typer.secho(f"  {old}  —— {reason}", fg=typer.colors.RED)


@app.command("rename-files")
def rename_files(
    root: str = typer.Argument(..., help="要整理的父目录"),
    apply: bool = typer.Option(False, "--apply", help="真正执行改名（默认只预览）"),
    rules: Optional[Path] = typer.Option(None, "--rules", help="规则文件路径"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出计划"),
) -> None:
    """把文件名里带的番号规范化成所在目录的番号。

    目录名已经规范过了，但文件还留着下载时的原样
    （``kfa11.com@ACHJ038.jpg``）。本命令把它改成 ``ACHJ-038.jpg``。

    - 番号取**顶层目录**的番号（往下 2~5 层是原标题子目录、分类分组、
      解压残留，不承载番号）
    - 只处理文件名里**本来就有番号**的；中文标题、``001.jpg`` 序列、
      原始导出名一律不动 —— 那些名字本身就是内容标识
    - 冲突绝不覆盖：目标已存在就跳过并报告
    """
    root_dir = _resolve_root(root)
    ruleset = _load_rules(rules)
    plan = rename_files_mod.build_file_plan(root_dir, ruleset)

    if json_out:
        typer.echo(json.dumps(
            {"root": plan.root, "summary": plan.summary(),
             "items": [i.model_dump(mode="json") for i in plan.items],
             "inaccessible": plan.inaccessible},
            ensure_ascii=False, indent=2,
        ))
    else:
        _print_file_plan(plan)

    if not apply:
        if not json_out:
            counts = plan.summary()
            typer.secho(
                f"\n预览模式：{counts['rename']} 个文件待改名、"
                f"{counts['keep']} 个已是规范名、{counts['skip']} 个判定不动、"
                f"{counts['conflict']} 个冲突。"
                f" 另有 {counts['no_code_files']} 个文件名里没有番号，保持原样。"
                f" 确认无误后加 --apply 执行。",
                fg=typer.colors.YELLOW,
            )
        return

    result = rename_files_mod.apply_file_plan(root_dir, plan)
    typer.secho(
        f"改名完成：{len(result['renamed'])} 个文件已规范化，"
        f"{len(result['conflicts'])} 个冲突跳过，{len(result['failed'])} 个失败。",
        fg=typer.colors.GREEN,
    )
    for old, new in result["renamed"][:50]:
        typer.echo(f"  {old}  ->  {new}")
    if len(result["renamed"]) > 50:
        typer.echo(f"  … 另有 {len(result['renamed']) - 50} 个")
    if result["conflicts"]:
        typer.secho(
            f"冲突（目标已存在，未覆盖）{len(result['conflicts'])} 个："
            f"{', '.join(result['conflicts'][:3])}",
            fg=typer.colors.YELLOW,
        )
    if result["failed"]:
        typer.secho(f"失败 {len(result['failed'])} 个。", fg=typer.colors.RED)


def _print_file_plan(plan) -> None:
    typer.secho(f"目录：{plan.root}", bold=True)
    counts = plan.summary()
    typer.secho(
        f"番号目录 {counts['code_dirs']} 个 | "
        f"待改名 {counts['rename']} | 已规范 {counts['keep']} | "
        f"判定不动 {counts['skip']} | 冲突 {counts['conflict']}",
        fg=typer.colors.CYAN,
    )
    typer.secho(
        f"另有 {counts['no_code_files']} 个文件名里没有番号（中文标题、图片序列、"
        f"原始导出名等），保持原样。",
        fg=typer.colors.CYAN,
    )

    if plan.rename_items:
        typer.secho("\n将改名：", bold=True)
        for item in plan.rename_items[:20]:
            typer.echo(f"  {item.relpath}  ->  {item.target}")
        if len(plan.rename_items) > 20:
            typer.echo(f"  … 另有 {len(plan.rename_items) - 20} 个")

    if plan.skip_items:
        typer.secho("\n判定不动（文件名是别的番号）：", fg=typer.colors.YELLOW)
        for item in plan.skip_items[:10]:
            typer.echo(f"  {item.relpath}  —— {item.reason}")
        if len(plan.skip_items) > 10:
            typer.echo(f"  … 另有 {len(plan.skip_items) - 10} 个")

    if plan.conflict_items:
        typer.secho("\n冲突（目标已存在，不会覆盖）：", fg=typer.colors.RED)
        for item in plan.conflict_items[:10]:
            typer.echo(f"  {item.relpath}  ->  {item.target}")

    if plan.inaccessible:
        typer.secho(
            f"\nWindows 上无法访问，已跳过（{len(plan.inaccessible)} 个）：",
            fg=typer.colors.YELLOW,
        )
        for entry in plan.inaccessible[:5]:
            typer.echo(f"  {entry}")


@app.command()
def scan(
    root: str = typer.Argument(..., help="要扫描的父目录"),
    rules: Optional[Path] = typer.Option(None, "--rules", help="规则文件路径"),
    show_unmatched: bool = typer.Option(True, "--unmatched/--no-unmatched", help="列出未匹配项"),
    limit: int = typer.Option(0, help="未匹配项最多显示几条（0 = 全部）"),
) -> None:
    """扫描目录，统计番号识别情况（不改动任何文件）。"""
    root_dir = _resolve_root(root)
    ruleset = _load_rules(rules)
    names = _list_dirs(root_dir)
    plan = engine.build_plan(root_dir, ruleset, names)

    by_rule: dict[str, int] = {}
    for item in plan.items:
        if item.action in (Action.RENAME, Action.KEEP):
            by_rule[item.rule] = by_rule.get(item.rule, 0) + 1

    counts = plan.summary()
    typer.secho(f"目录：{root_dir}", bold=True)
    typer.secho(
        f"共 {counts['total']} 个子目录 | 可改名 {counts['rename']} | "
        f"已规范 {counts['keep']} | 未匹配 {counts['skip']} | 冲突 {counts['conflict']}",
        fg=typer.colors.CYAN,
    )
    typer.secho("\n按规则命中：", bold=True)
    for rule_name, count in sorted(by_rule.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {rule_name:24s} {count:4d}")

    if show_unmatched and plan.skip_items:
        typer.secho("\n未匹配（需补规则）：", bold=True)
        for item in plan.skip_items[: limit or None]:
            typer.echo(f"  {item.original}")


@app.command()
def merge(
    root: str = typer.Argument(..., help="要整理的父目录"),
    apply: bool = typer.Option(False, "--apply", help="真正执行合并（默认只预览）"),
    rules: Optional[Path] = typer.Option(None, "--rules", help="规则文件路径"),
    keep_separate: bool = typer.Option(
        False, "--keep-separate",
        help="保守模式：只合并内容完全一致的组，内容不同/部分重叠的各自保留",
    ),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出计划"),
) -> None:
    """识别重复目录并合并。

    默认**一律合并**基础名相同的目录：保留一个主目录，其余目录的文件
    挪进去。文件级撞名按**体积**分配 —— 大的那份占着规范名，小的改名
    `` (2)``，两份都留着；内容完全相同的只留一份。

    判定依据是**文件内容指纹**，不是目录名 —— 带 (1) 的目录未必是复制副本，
    也可能是同作品的不同画质版本或分P。
    """
    root_dir = _resolve_root(root)
    ruleset = _load_rules(rules)
    names = _list_dirs(root_dir)
    plan = merge_mod.build_merge_plan(root_dir, ruleset, names)

    if json_out:
        typer.echo(json.dumps(
            {"root": plan.root, "summary": plan.summary(),
             "groups": [g.model_dump(mode="json") for g in plan.groups]},
            ensure_ascii=False, indent=2,
        ))
    else:
        _print_merge_plan(plan)

    if not apply:
        if not json_out:
            counts = plan.summary()
            typer.secho(
                f"\n预览模式：{counts['groups']} 组基础名相同 —— "
                f"{counts['duplicate']} 组内容完全一致、"
                f"{counts['partial']} 组部分重叠、"
                f"{counts['different']} 组内容不同。"
                f" 确认无误后加 --apply 执行。",
                fg=typer.colors.YELLOW,
            )
            if keep_separate:
                typer.secho(
                    "保守模式：只有内容完全一致的组会被合并，"
                    "内容不同/部分重叠的组各自保留。",
                    fg=typer.colors.YELLOW,
                )
        return

    results = merge_mod.apply_merge_plan(
        root_dir, plan, only_duplicates=keep_separate
    )
    merged = [r for r in results if r["action"] == MergeAction.MERGE.value]
    kept = [r for r in results if r["action"] == MergeAction.KEEP.value]
    skipped = [r for r in results if r["action"] == MergeAction.SKIP.value]
    failed_total = sum(len(r.get("failed", [])) for r in results)
    deduped_total = sum(len(r.get("deduped", [])) for r in results)
    typer.secho(
        f"合并完成：{len(merged)} 个副本目录已并入，"
        f"{len(kept)} 个主目录保留，{len(skipped)} 个未处理。",
        fg=typer.colors.GREEN,
    )
    for r in merged:
        typer.echo(f"  {r['dir']}  ->  {r['base']}  (并入 {len(r['moved'])} 个文件)")
        if r.get("deduped"):
            typer.secho(
                f"      内容重复已去重 {len(r['deduped'])} 个（只留一份）",
                fg=typer.colors.YELLOW,
            )
        if r.get("failed"):
            typer.secho(
                f"      失败 {len(r['failed'])} 个（被占用或无权限，原地保留）："
                f"{', '.join(r['failed'][:3])}",
                fg=typer.colors.RED,
            )
    if deduped_total:
        typer.secho(
            f"共去重 {deduped_total} 个内容完全相同的文件（每个只留一份）。",
            fg=typer.colors.YELLOW,
        )
    if failed_total:
        typer.secho(
            f"共 {failed_total} 个文件未能并入 —— 通常是被其他程序占用"
            "（资源管理器预览窗、播放器）。关掉占用者后重跑即可。",
            fg=typer.colors.YELLOW,
        )


def _print_merge_plan(plan) -> None:
    typer.secho(f"目录：{plan.root}", bold=True)
    if not plan.groups:
        typer.secho("没有发现基础名相同的目录组，无需合并。", fg=typer.colors.GREEN)
        return

    color = {
        MergeVerdict.DUPLICATE: typer.colors.GREEN,
        MergeVerdict.PARTIAL: typer.colors.YELLOW,
        MergeVerdict.DIFFERENT: typer.colors.RED,
    }
    for group in plan.groups:
        typer.secho(
            f"[{group.verdict.value.upper()}] {group.base}",
            fg=color[group.verdict], bold=True,
        )
        for d in group.dirs:
            mark = "=>" if d == group.primary else "  "
            typer.echo(f"   {mark} {d}")
        typer.secho(f"         {group.detail}", fg=typer.colors.CYAN)
        if group.conflicting_files:
            typer.secho(
                f"         冲突文件 {len(group.conflicting_files)} 个（不会覆盖）："
                f"{', '.join(group.conflicting_files[:3])}",
                fg=typer.colors.YELLOW,
            )
        if group.unique_files:
            typer.secho(
                f"         可并入 {len(group.unique_files)} 个独有文件", fg=typer.colors.CYAN
            )


@app.command()
def archive(
    root: str = typer.Argument(..., help="平铺文件的父目录"),
    apply: bool = typer.Option(False, "--apply", help="真正执行分装（默认只预览）"),
    keep_forks: bool = typer.Option(False, "--keep-forks", help="保留 ._ 资源叉垃圾文件"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出计划"),
) -> None:
    """把平铺的文件按归一番号分装进子目录。

    两种变体会被归一到同一目录：
    - 横杠变体：``CLUB810`` 与 ``CLUB-810`` 是同一番号（不同版本）
    - C 后缀：  ``CLUB830C`` 与 ``CLUB830`` 是同一作品（C = 中文字幕版）

    目录内已有的子目录不会被递归或合并。
    """
    root_dir = _resolve_root(root)
    plan = archive_mod.build_archive_plan(root_dir)

    if json_out:
        typer.echo(json.dumps(
            {"root": plan.root, "summary": plan.summary(),
             "groups": [g.model_dump(mode="json") for g in plan.groups],
             "forks": plan.forks, "untouched": plan.untouched,
             "blocked": plan.blocked, "unsafe": plan.unsafe},
            ensure_ascii=False, indent=2,
        ))
    else:
        _print_archive_plan(plan)

    if not apply:
        if not json_out:
            counts = plan.summary()
            typer.secho(
                f"\n预览模式：将建 {counts['groups']} 个目录、分装 {counts['files']} 个文件"
                f"（其中 {counts['merged_groups']} 组由多个变体归一而来）、"
                f"清理 {counts['forks']} 个 ._ 垃圾文件。"
                f" 确认无误后加 --apply 执行。",
                fg=typer.colors.YELLOW,
            )
        return

    result = archive_mod.apply_archive_plan(
        root_dir, plan, clean_forks=not keep_forks
    )
    typer.secho(
        f"归档完成：新建 {result['created_dirs']} 个目录，"
        f"分装 {result['moved']} 个文件，"
        f"清理 {result['forks_removed']} 个垃圾文件。",
        fg=typer.colors.GREEN,
    )
    if result["conflicts"]:
        typer.secho(
            f"注意：{result['conflicts']} 个文件因目标已存在同名文件而未移动（未覆盖）。",
            fg=typer.colors.YELLOW,
        )
    if result.get("failed"):
        typer.secho(
            f"注意：{len(result['failed'])} 个文件未能移动（被占用或无权限）："
            f"{', '.join(result['failed'][:5])}",
            fg=typer.colors.YELLOW,
        )
    if result.get("unsafe"):
        _report_unsafe(result.get("unsafe"), "文件")
    if result["blocked"]:
        typer.secho(
            f"注意：{len(result['blocked'])} 组因目录名与现有文件冲突而未处理："
            f"{', '.join(result['blocked'])}",
            fg=typer.colors.YELLOW,
        )


def _print_archive_plan(plan) -> None:
    typer.secho(f"目录：{plan.root}", bold=True)
    counts = plan.summary()
    typer.secho(
        f"共 {counts['groups']} 个番号组 / {counts['files']} 个文件"
        f"（{counts['merged_groups']} 组由多个变体归一）",
        fg=typer.colors.CYAN,
    )

    for group in plan.groups:
        if group.merged:
            typer.secho(f"[MERGED] {group.name}", fg=typer.colors.MAGENTA, bold=True)
            for variant in group.variants:
                files = [i.filename for i in group.items if i.variant == variant]
                typer.echo(f"     {variant}  ->  {files}")
        else:
            typer.secho(f"[GROUP ] {group.name}", fg=typer.colors.GREEN)
            for item in group.items:
                typer.echo(f"     {item.filename}")
        if group.conflicts:
            typer.secho(
                f"     冲突（目标已有同名文件，不会覆盖）：{', '.join(group.conflicts)}",
                fg=typer.colors.RED,
            )

    if plan.forks:
        typer.secho(
            f"\n将清理 {len(plan.forks)} 个 ._ 资源叉垃圾文件"
            f"（如 {', '.join(plan.forks[:3])}...）",
            fg=typer.colors.YELLOW,
        )
    if plan.untouched:
        typer.secho(
            f"保留原地（不参与分装）{len(plan.untouched)} 个：{', '.join(plan.untouched[:5])}",
            fg=typer.colors.YELLOW,
        )
    if plan.blocked:
        typer.secho(
            f"无法处理（目录名与现有文件冲突）：{', '.join(plan.blocked)}",
            fg=typer.colors.RED,
        )
    if plan.unsafe:
        _report_unsafe(plan.unsafe, "文件")


@app.command()
def tidy(
    root: str = typer.Argument(..., help="要整理的目录"),
    apply: bool = typer.Option(False, "--apply", help="真正执行（默认只预览）"),
    rules: Optional[Path] = typer.Option(None, "--rules", help="规则文件路径"),
    keep_separate: bool = typer.Option(
        False, "--keep-separate",
        help="保守模式：内容不同/部分重叠的同名目录各自保留，不合并",
    ),
    isolate: Optional[str] = typer.Option(
        None, "--isolate", help="把不符合番号规范的目录挪到这里（如 D:\\Media\\GC）"
    ),
    keep_forks: bool = typer.Option(False, "--keep-forks", help="保留 ._ 资源叉垃圾文件"),
    no_file_rename: bool = typer.Option(
        False, "--no-file-rename", help="只改目录名，不改文件名里的番号"
    ),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出计划"),
) -> None:
    """智能整理一个目录：自动判断形态并编排动作。

    自动识别目录形态并决定做什么：

    - flat   （平铺文件）      → 分装进番号目录
    - nested （子目录是番号）  → 规范化目录名 + 合并重复目录 + 规范化文件名
    - mixed  （两者都有）      → 先分装再规范化
    - loose  （子目录不像番号）→ 只报告，不动

    执行顺序固定为 clean → isolate → merge → archive → rename → rename-files。
    """
    root_dir = _resolve_root(root)
    ruleset = _load_rules(rules)
    isolate_to = Path(isolate).expanduser() if isolate else None
    if isolate_to is not None and isolate_to.exists() and not isolate_to.is_dir():
        _die(f"隔离目标不是目录：{isolate_to}")
    try:
        plan = tidy_mod.build_tidy_plan(
            root_dir, ruleset, isolate_to=isolate_to,
            rename_file_names=not no_file_rename,
        )
    except ValueError as exc:
        _die(str(exc))

    if json_out:
        typer.echo(json.dumps(
            {"root": plan.root, "summary": plan.summary(),
             "shape": plan.shape.value, "shape_reason": plan.shape_reason,
             "steps": [s.model_dump(mode="json") for s in plan.steps],
             "notes": plan.notes},
            ensure_ascii=False, indent=2,
        ))
    else:
        _print_tidy_plan(plan)

    if not apply:
        if not json_out:
            typer.secho(
                f"\n预览模式：识别为 {plan.shape.value} 形态，"
                f"计划 {plan.total_actions} 个动作。确认无误后加 --apply 执行。",
                fg=typer.colors.YELLOW,
            )
        return

    result = tidy_mod.apply_tidy_plan(
        root_dir, ruleset, plan,
        keep_separate=keep_separate,
        clean_forks=not keep_forks,
        isolate_to=isolate_to,
    )
    typer.secho(
        f"整理完成：隔离 {result['isolated']} 个目录，"
        f"合并 {result['merged']} 个重复目录，"
        f"分装 {result['archived']} 个文件，"
        f"改名 {result['renamed']} 个目录，"
        f"规范化 {result['renamed_files']} 个文件名，"
        f"清理 {result['forks_removed']} 个垃圾文件。",
        fg=typer.colors.GREEN,
    )
    if result["conflicts"]:
        typer.secho(
            f"注意：{result['conflicts']} 项因目标已存在而未处理（未覆盖）。",
            fg=typer.colors.YELLOW,
        )
    if result.get("failed"):
        typer.secho(
            f"注意：{result['failed']} 项因被占用或无权限而失败，原地保留。",
            fg=typer.colors.YELLOW,
        )


def _print_tidy_plan(plan) -> None:
    typer.secho(f"目录：{plan.root}", bold=True)
    typer.secho(
        f"识别形态：{plan.shape.value.upper()}  （{plan.shape_reason}）",
        fg=typer.colors.CYAN, bold=True,
    )

    if not plan.steps:
        typer.secho("无需改动。", fg=typer.colors.GREEN)

    icon = {
        StepKind.CLEAN: "清理",
        StepKind.ISOLATE: "隔离",
        StepKind.MERGE: "合并",
        StepKind.ARCHIVE: "分装",
        StepKind.RENAME: "改名",
        StepKind.RENAME_FILES: "改文件名",
    }
    for step in plan.steps:
        typer.secho(
            f"[{icon[step.kind]}] {step.detail} —— {step.count} 项",
            fg=typer.colors.YELLOW, bold=True,
        )
        for name in step.names[:8]:
            typer.echo(f"     {name}")
        if len(step.names) > 8:
            typer.echo(f"     … 另有 {len(step.names) - 8} 项")

    if plan.notes:
        typer.secho("\n提示：", bold=True)
        for note in plan.notes:
            typer.secho(f"  • {note}", fg=typer.colors.CYAN)


@app.command()
def isolate(
    root: str = typer.Argument(..., help="要整理的目录"),
    target: str = typer.Option(..., "--to", help="把不符合的目录挪到这里（如 D:\\Media\\GC）"),
    apply: bool = typer.Option(False, "--apply", help="真正执行（默认只预览）"),
    rules: Optional[Path] = typer.Option(None, "--rules", help="规则文件路径"),
    keep_empty: bool = typer.Option(False, "--keep-empty", help="空目录不挪走"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出计划"),
) -> None:
    """把不符合番号规范的目录挪到另一个目录。

    判定看的是**内容**，不是目录名 —— 名字不像番号但内含正规番号的合集目录
    （如 ``Uncensored Leaked_001`` 里的 ABP-001）会被保留，不会被误挪走。
    """
    root_dir = _resolve_root(root)
    target_dir = Path(target).expanduser()
    if target_dir.exists() and not target_dir.is_dir():
        _die(f"目标不是目录：{target_dir}")
    ruleset = _load_rules(rules)
    try:
        plan = isolate_mod.build_isolation_plan(
            root_dir, target_dir, ruleset, move_empty=not keep_empty
        )
    except ValueError as exc:
        _die(str(exc))
    if json_out:
        typer.echo(json.dumps(
            {"root": plan.root, "target": plan.target, "summary": plan.summary(),
             "items": [i.model_dump(mode="json") for i in plan.items],
             "conflicts": plan.conflicts, "unsafe": plan.unsafe},
            ensure_ascii=False, indent=2,
        ))
    else:
        _print_isolation_plan(plan)

    if not apply:
        if not json_out:
            counts = plan.summary()
            typer.secho(
                f"\n预览模式：{counts['move']} 个目录将挪到 {target_dir}，"
                f"{counts['keep']} 个保留。确认无误后加 --apply 执行。",
                fg=typer.colors.YELLOW,
            )
        return

    result = isolate_mod.apply_isolation_plan(root_dir, target_dir, plan)
    typer.secho(
        f"隔离完成：挪走 {len(result['moved'])} 个目录到 {target_dir}，"
        f"保留 {len(result['kept'])} 个。",
        fg=typer.colors.GREEN,
    )
    for name in result["moved"]:
        typer.echo(f"  {name}")
    for note in result["renamed"]:
        typer.secho(f"  （目标已存在同名目录）{note}", fg=typer.colors.YELLOW)
    if result["failed"]:
        typer.secho(f"失败 {len(result['failed'])} 个：{result['failed']}", fg=typer.colors.RED)
    if plan.unsafe:
        _report_unsafe(plan.unsafe, "目录")


def _print_isolation_plan(plan) -> None:
    typer.secho(f"源目录：{plan.root}", bold=True)
    typer.secho(f"目标目录：{plan.target}", bold=True)
    counts = plan.summary()
    typer.secho(
        f"共 {counts['total']} 个子目录 | 挪走 {counts['move']} | 保留 {counts['keep']}",
        fg=typer.colors.CYAN,
    )

    if plan.move_items:
        typer.secho("\n将挪走：", bold=True)
        for item in plan.move_items:
            typer.echo(f"  {item.name}")
            typer.secho(f"      {item.reason}", fg=typer.colors.YELLOW)

    # 只展示「非番号目录名」的保留项，番号目录保留是显然的
    interesting_keeps = [i for i in plan.keep_items if i.code_samples]
    if interesting_keeps:
        typer.secho("\n保留（含番号的合集目录）：", bold=True)
        for item in interesting_keeps:
            typer.echo(f"  {item.name}")
            typer.secho(f"      {item.reason}", fg=typer.colors.GREEN)

    if plan.conflicts:
        typer.secho(
            f"\n注意：目标目录已存在同名目录，挪动时会自动加序号："
            f"{', '.join(plan.conflicts)}",
            fg=typer.colors.YELLOW,
        )
    if plan.unsafe:
        typer.echo("")
        _report_unsafe(plan.unsafe, "目录")


@app.command()
def clean(
    root: str = typer.Argument(..., help="要清理的目录"),
    apply: bool = typer.Option(False, "--apply", help="真正执行（默认只预览）"),
    no_recursive: bool = typer.Option(False, "--no-recursive", help="只清理顶层，不递归"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出计划"),
) -> None:
    """递归清理资源叉垃圾（``._xxx`` / ``.DS_Store`` / ``Thumbs.db``）。

    这些是 macOS / Windows 在 exFAT 等非 HFS+ 卷上留下的元数据副本，
    对媒体文件没有价值。只删垃圾，真实文件和目录一律不动。
    """
    root_dir = _resolve_root(root)
    plan = clean_mod.build_clean_plan(root_dir, recursive=not no_recursive)

    if json_out:
        typer.echo(json.dumps(
            {"root": plan.root, "summary": plan.summary(),
             "items": [i.model_dump(mode="json") for i in plan.items[:500]]},
            ensure_ascii=False, indent=2,
        ))
    else:
        counts = plan.summary()
        typer.secho(f"目录：{plan.root}", bold=True)
        typer.secho(
            f"发现 {counts['forks']} 个资源叉垃圾，"
            f"共 {counts['size'] / 1024 / 1024:.1f} MB",
            fg=typer.colors.CYAN,
        )
        for item in plan.items[:10]:
            typer.echo(f"  {item.path}")
        if len(plan.items) > 10:
            typer.echo(f"  … 另有 {len(plan.items) - 10} 个")

    if not apply:
        if not json_out:
            typer.secho(
                f"\n预览模式：将清理 {plan.summary()['forks']} 个资源叉垃圾。"
                f" 确认无误后加 --apply 执行。",
                fg=typer.colors.YELLOW,
            )
        return

    result = clean_mod.apply_clean_plan(plan)
    typer.secho(
        f"清理完成：删除 {result['removed']} 个资源叉，"
        f"释放 {result['freed'] / 1024 / 1024:.1f} MB。",
        fg=typer.colors.GREEN,
    )
    if result["failed"]:
        typer.secho(f"失败 {len(result['failed'])} 个", fg=typer.colors.RED)


@rules_app.command("show")
def rules_show(
    rules: Optional[Path] = typer.Option(None, "--rules", help="规则文件路径"),
) -> None:
    """打印当前规则集摘要。"""
    ruleset = _load_rules(rules)
    path = rules or DEFAULT_RULES
    typer.secho(f"规则文件：{path}", bold=True)
    typer.secho(f"版本：{ruleset.version}  {ruleset.description}\n")
    typer.secho("预处理规则：", bold=True)
    for rule in ruleset.preprocess:
        typer.echo(f"  {rule.name:24s} {rule.description}")
    typer.secho("\n番号规则：", bold=True)
    for rule in ruleset.ordered_matches():
        typer.echo(f"  [{rule.priority:3d}] {rule.name:20s} {rule.description}")
        typer.echo(f"        pattern : {rule.pattern}")
        typer.echo(f"        template: {rule.template}   dup_suffix: {rule.dup_suffix}")


def _print_plan(plan) -> None:
    typer.secho(f"目录：{plan.root}", bold=True)
    for item in plan.items:
        if item.action is Action.SKIP:
            continue
        color = {
            Action.RENAME: typer.colors.YELLOW,
            Action.KEEP: typer.colors.GREEN,
            Action.CONFLICT: typer.colors.RED,
        }[item.action]
        tag = {Action.RENAME: "RENAME", Action.KEEP: "KEEP  ", Action.CONFLICT: "CONFLICT"}[item.action]
        typer.secho(f"[{tag}] ", fg=color, nl=False)
        typer.echo(f"{item.original}  ->  {item.target}")
        if item.action is Action.CONFLICT:
            typer.secho(f"         {item.reason}", fg=typer.colors.RED)


def _parse_time(value: str) -> float:
    """把 ``12.5`` / ``00:00:12.5`` / ``1:02`` 解析成秒。"""
    text = value.strip()
    try:
        parts = text.split(":")
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise typer.BadParameter(f"看不懂的时间格式：{value}（用 12.5 或 00:00:12.5）")


def _load_ad_bank(path: Optional[Path]):
    templates_path = path or DEFAULT_AD_TEMPLATES
    try:
        return adcut_templates.load_bank(templates_path), templates_path
    except adcut_templates.TemplateError as exc:
        _die(str(exc))


def _load_ad_boundaries(path: Optional[Path]):
    """载入人工核验的逐文件边界表。

    表不存在就当没有 —— 它是可选的增强，不是必需品。
    """
    boundaries_path = path or DEFAULT_AD_BOUNDARIES
    if not boundaries_path.exists():
        return {}, boundaries_path
    try:
        book = adcut_boundaries.load_boundaries(boundaries_path)
    except adcut_boundaries.BoundaryError as exc:
        _die(str(exc))
    return {item.path: item for item in book.boundaries}, boundaries_path


@adcut_app.command("scan")
def adcut_scan(
    root: str = typer.Argument(..., help="要扫描的目录"),
    templates: Optional[Path] = typer.Option(None, "--templates", help="模板库路径"),
    boundaries: Optional[Path] = typer.Option(None, "--boundaries", help="人工核验边界表路径"),
    head_seconds: float = typer.Option(adcut_detect.DEFAULT_HEAD_SECONDS, "--head-seconds", help="只看开头多少秒"),
    jobs: int = typer.Option(4, "--jobs", help="并发数"),
    limit: int = typer.Option(0, help="最多处理几个视频（0 = 全部）"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出"),
    quiet: bool = typer.Option(False, "--quiet", help="只打摘要"),
) -> None:
    """扫描目录，判断每个视频开头有没有广告。

    只读不写。判断依据是**模板库**：库里存着已知广告的感知指纹，
    命中即判定为「有广告」。
    """
    root_dir = _resolve_root(root)
    bank, _ = _load_ad_bank(templates)
    manual_boundaries, _ = _load_ad_boundaries(boundaries)

    def progress(done: int, total: int, _path) -> None:
        if not quiet and not json_out and (done % 10 == 0 or done == total):
            typer.echo(f"  扫描中 {done}/{total} …", nl=False if done < total else True)

    plan = adcut_detect.scan_tree(
        root_dir, bank, head_seconds=head_seconds, jobs=jobs, limit=limit,
        on_progress=progress, boundaries=manual_boundaries,
    )

    if json_out:
        typer.echo(json.dumps(
            {"root": plan.root, "summary": plan.summary(),
             "detections": [d.model_dump(mode="json") for d in plan.detections],
             "errors": plan.errors},
            ensure_ascii=False, indent=2,
        ))
        return

    counts = plan.summary()
    typer.secho(f"目录：{root_dir}", bold=True)
    typer.secho(
        f"共 {counts['total']} 个视频 | 可裁 {counts['cut']} | "
        f"待确认 {counts['review']} | 无广告 {counts['none']} | 跳过 {counts['skip']}",
        fg=typer.colors.CYAN,
    )
    if counts["suspected"]:
        typer.secho(
            f"其中 {counts['suspected']} 个开头几乎静止，可能是没认识的片头卡 —— "
            f"值得用 sheet 扫一眼",
            fg=typer.colors.YELLOW,
        )
    _print_detections(plan.cut_items, "可裁", typer.colors.GREEN)
    _print_detections(plan.review_items, "待确认", typer.colors.YELLOW, hint=True)
    if counts["suspected"]:
        _print_detections(plan.suspected_items, "疑似有广告（未识别）", typer.colors.YELLOW, hint=True)
    if plan.errors:
        typer.secho(f"\n出错 {len(plan.errors)} 个：", fg=typer.colors.RED)
        for message in plan.errors[:5]:
            typer.secho(f"  {message}", fg=typer.colors.RED)


def _print_detections(items, title: str, color, hint: bool = False) -> None:
    if not items:
        return
    typer.secho(f"\n{title}（{len(items)} 个）：", fg=color, bold=True)
    for item in items[:40]:
        typer.echo(
            f"  {item.path}  切 {item.cut_at:.2f}s  "
            f"置信 {item.confidence:.2f}  [{item.template_id}] {item.template_label}"
        )
        typer.secho(f"      {item.reason}", fg=typer.colors.CYAN)
        if item.note:
            typer.secho(f"      ⚠ {item.note}", fg=typer.colors.YELLOW)
        if hint:
            where = item.cut_at if item.cut_at > 0 else 10.0
            typer.secho(
                f"      肉眼确认：file-tool ad-cut sheet {item.path} --at {where:.0f}",
                fg=typer.colors.YELLOW,
            )
    if len(items) > 40:
        typer.echo(f"  … 另有 {len(items) - 40} 个")


@adcut_app.command("cluster")
def adcut_cluster(
    root: str = typer.Argument(..., help="要扫描的目录"),
    templates: Optional[Path] = typer.Option(None, "--templates", help="模板库路径"),
    boundaries: Optional[Path] = typer.Option(None, "--boundaries", help="人工核验边界表路径"),
    head_seconds: float = typer.Option(adcut_detect.DEFAULT_HEAD_SECONDS, "--head-seconds", help="只看开头多少秒"),
    jobs: int = typer.Option(4, "--jobs", help="并发数"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出"),
) -> None:
    """找出「还没认识」的片头广告，按相似度聚成组。

    同一组里的视频共享同一段片头。人工只要给其中**一个**划一刀
    (``ad-cut learn``)，整组就都能自动处理了 —— 人工量正比于
    广告种类数，而不是视频数。
    """
    root_dir = _resolve_root(root)
    bank, _ = _load_ad_bank(templates)
    manual_boundaries, _ = _load_ad_boundaries(boundaries)

    plan = adcut_detect.scan_tree(
        root_dir, bank, head_seconds=head_seconds, jobs=jobs,
        boundaries=manual_boundaries,
    )
    videos = adcut_detect.find_videos(root_dir)
    signatures = adcut_detect.collect_signatures(
        videos, root_dir, head_seconds=head_seconds
    )
    clusters = adcut_detect.cluster_unknown(plan, signatures=signatures)

    if json_out:
        typer.echo(json.dumps(
            {"root": plan.root,
             "clusters": [c.model_dump(mode="json") for c in clusters]},
            ensure_ascii=False, indent=2,
        ))
        return

    typer.secho(f"目录：{root_dir}", bold=True)
    if not clusters:
        typer.secho("没有发现共享同一段未知片头的视频组。", fg=typer.colors.GREEN)
    else:
        typer.secho(
            f"发现 {len(clusters)} 组未知片头，共覆盖 "
            f"{sum(c.size for c in clusters)} 个视频：",
            fg=typer.colors.CYAN,
        )
        for cluster in clusters:
            typer.secho(
                f"\n[{cluster.id}] {cluster.size} 个视频共享同一段片头",
                fg=typer.colors.YELLOW, bold=True,
            )
            if cluster.note:
                typer.secho(f"  {cluster.note}", fg=typer.colors.CYAN)
            typer.echo(f"  代表：{cluster.representative}")
            for member in cluster.members[:8]:
                typer.echo(f"    - {member}")
            if cluster.size > 8:
                typer.echo(f"    … 另有 {cluster.size - 8} 个")

    suspected = plan.suspected_items
    if suspected:
        typer.secho(
            f"\n另有 {len(suspected)} 个视频开头几乎静止、但没匹配到任何模板 —— "
            f"可能是只出现过一次的片头卡：",
            fg=typer.colors.YELLOW,
        )
        for item in suspected[:15]:
            typer.echo(f"  {item.path}  （静止 {item.static_ratio * 100:.0f}%）")
        if len(suspected) > 15:
            typer.echo(f"  … 另有 {len(suspected) - 15} 个")

    typer.secho(
        "\n给每组挑一个代表，量出广告结束时间后学进模板库：\n"
        "  file-tool ad-cut learn <视频> --end 12.5 --label \"厂牌片头\"",
        fg=typer.colors.CYAN,
    )


@adcut_app.command("learn")
def adcut_learn(
    video: str = typer.Argument(..., help="拿哪个视频当样本"),
    end: str = typer.Option(..., "--end", help="广告结束时间（秒或 00:00:12.5）"),
    label: str = typer.Option("", "--label", help="给这支广告起个名字"),
    templates: Optional[Path] = typer.Option(None, "--templates", help="模板库路径"),
    note: str = typer.Option("", "--note", help="备注"),
    replace: Optional[str] = typer.Option(
        None, "--replace", help="覆盖这条已有模板（改边界时用），保留 id 和计数",
    ),
) -> None:
    """把一支广告学进模板库。

    只需要**一支**广告的样本 —— 同族的所有视频会自动命中它。
    反复对同一支广告执行本命令会合并到已有模板（samples 递增）。

    ``--replace tpl-0002`` 用来**改**已有模板的边界：库里的模板长度
    （也就是裁剪点）划错了时，覆盖比新建好 —— 新建会让「错的旧模板」
    和「对的新模板」同时留在库里，两条都能匹配上，结果是随机切。
    """
    path = Path(video).expanduser()
    if not path.exists():
        _die(f"文件不存在：{path}")
    cut_at = _parse_time(end)
    if cut_at <= 0:
        _die("--end 必须大于 0")

    bank, templates_path = _load_ad_bank(templates)
    try:
        signature = adcut_fingerprint.segment_signature_of(path, 0.0, cut_at)
    except Exception as exc:
        _die(f"取帧失败：{exc}")
    if not signature.hashes:
        _die("取不到片头帧，视频是不是太短了？")

    if replace:
        try:
            template = adcut_templates.replace_template(
                bank, replace, signature.hashes, sample=str(path),
                label=label, note=note, brightness=signature.brightness,
            )
        except adcut_templates.TemplateError as exc:
            _die(str(exc))
        created = False
    else:
        try:
            template, created = adcut_templates.learn(
                bank, signature.hashes, sample=str(path), label=label, note=note,
                brightness=signature.brightness,
            )
        except adcut_templates.TemplateError as exc:
            _die(str(exc))
    try:
        adcut_templates.save_bank(bank, templates_path)
    except Exception as exc:
        _die(f"模板库写入失败：{exc}")

    if replace:
        typer.secho(
            f"已覆盖模板 {template.id}：{template.label or '（未命名）'} "
            f"（{template.seconds_label}）",
            fg=typer.colors.GREEN,
        )
    elif created:
        typer.secho(
            f"新建模板 {template.id}：{template.label or '（未命名）'} "
            f"（{template.seconds_label}）",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            f"合并到已有模板 {template.id}（累计 {template.samples} 次确认，"
            f"{template.seconds_label}）",
            fg=typer.colors.GREEN,
        )
    typer.echo(f"  模板库：{templates_path}")
    typer.secho(
        f"  肉眼确认切点：file-tool ad-cut sheet {path} --at {cut_at:.0f}",
        fg=typer.colors.CYAN,
    )


@adcut_app.command("reject")
def adcut_reject(
    video: str = typer.Argument(..., help="哪个视频"),
    end: str = typer.Option(..., "--end", help="没广告的区间长度（秒）"),
    reason: str = typer.Option("", "--reason", help="为什么这里没有广告"),
    templates: Optional[Path] = typer.Option(None, "--templates", help="模板库路径"),
) -> None:
    """记一条负样本：这个片头其实没有广告，别再切它。"""
    path = Path(video).expanduser()
    if not path.exists():
        _die(f"文件不存在：{path}")
    cut_at = _parse_time(end)

    bank, templates_path = _load_ad_bank(templates)
    try:
        signature = adcut_fingerprint.segment_signature_of(path, 0.0, cut_at)
    except Exception as exc:
        _die(f"取帧失败：{exc}")
    if not signature.hashes:
        _die("取不到片头帧，视频是不是太短了？")

    negative = adcut_templates.reject(
        bank, signature.hashes, sample=str(path), reason=reason,
        brightness=signature.brightness,
    )
    try:
        adcut_templates.save_bank(bank, templates_path)
    except Exception as exc:
        _die(f"模板库写入失败：{exc}")

    typer.secho(f"已记负样本 {negative.id}，以后这类片头不会被自动裁。", fg=typer.colors.GREEN)
    typer.echo(f"  模板库：{templates_path}")


@adcut_app.command("cut")
def adcut_cut_cmd(
    root: str = typer.Argument(..., help="要裁剪的目录"),
    apply: bool = typer.Option(False, "--apply", help="真正执行裁剪（默认只预览）"),
    templates: Optional[Path] = typer.Option(None, "--templates", help="模板库路径"),
    boundaries: Optional[Path] = typer.Option(None, "--boundaries", help="人工核验边界表路径"),
    head_seconds: float = typer.Option(adcut_detect.DEFAULT_HEAD_SECONDS, "--head-seconds", help="只看开头多少秒"),
    jobs: int = typer.Option(4, "--jobs", help="并发数（扫描与裁剪都算）"),
    accurate: bool = typer.Option(False, "--accurate", help="强制重编码，切在精确帧"),
    max_snap: float = typer.Option(2.0, "--max-snap", help="关键帧吸附上限（秒）"),
    limit: int = typer.Option(0, help="最多裁几个（0 = 全部）"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出"),
) -> None:
    """裁掉识别出来的片头广告。

    原地替换：先写到同目录的临时文件，校验通过才顶掉原片；
    任何一步失败原片都原封不动。默认只预览，加 ``--apply`` 才动手。
    """
    root_dir = _resolve_root(root)
    bank, _ = _load_ad_bank(templates)
    manual_boundaries, _ = _load_ad_boundaries(boundaries)

    plan = adcut_detect.scan_tree(
        root_dir, bank, head_seconds=head_seconds, jobs=jobs,
        boundaries=manual_boundaries,
    )
    targets = plan.cut_items
    if limit:
        targets = targets[:limit]

    if json_out:
        # `--json` 下 stdout 上只有**一份** JSON —— 预览和执行结果合在一起输出，
        # 下游 `json.loads(stdout)` 才能直接用
        payload = {
            "root": plan.root,
            "targets": [d.model_dump(mode="json") for d in targets],
        }
        if apply:
            outcomes = _run_cuts(root_dir, targets, max_snap, accurate, jobs)
            payload["ok"] = sum(1 for o in outcomes if o.ok)
            payload["failed"] = sum(1 for o in outcomes if not o.ok)
            payload["outcomes"] = [o.model_dump(mode="json") for o in outcomes]
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not targets:
        typer.secho("没有识别出可裁的视频。", fg=typer.colors.GREEN)
        return

    if not apply:
        typer.secho(f"目录：{root_dir}", bold=True)
        typer.secho(
            f"将裁剪 {len(targets)} 个视频（原地替换，先写临时文件再校验）：",
            fg=typer.colors.CYAN,
        )
        for item in targets[:40]:
            typer.echo(
                f"  {item.path}  切 {item.cut_at:.2f}s  置信 {item.confidence:.2f}  "
                f"[{item.template_id}]"
            )
            if item.note:
                typer.secho(f"      ⚠ {item.note}", fg=typer.colors.YELLOW)
        if len(targets) > 40:
            typer.echo(f"  … 另有 {len(targets) - 40} 个")
        typer.secho(
            "\n预览模式：确认无误后加 --apply 执行。",
            fg=typer.colors.YELLOW,
        )
        return

    outcomes = _run_cuts(root_dir, targets, max_snap, accurate, jobs)
    ok = sum(1 for o in outcomes if o.ok)
    failed = len(outcomes) - ok

    for outcome in outcomes:
        if outcome.ok:
            typer.secho(f"  ✓ {outcome.path}  {outcome.reason}", fg=typer.colors.GREEN)
        else:
            typer.secho(f"  ✗ {outcome.path}  {outcome.reason}", fg=typer.colors.RED)

    typer.secho(
        f"\n裁剪完成：成功 {ok} 个，失败 {failed} 个（失败的原片保持不变）。",
        fg=typer.colors.GREEN if not failed else typer.colors.YELLOW,
    )


def _run_cuts(root_dir: Path, targets, max_snap: float, accurate: bool, jobs: int = 1):
    """按计划逐个裁剪。

    ``jobs > 1`` 时并发裁。裁剪本身是「ffmpeg 子进程 + 临时文件」，
    瓶颈在磁盘 I/O 和编码，不在 Python —— 所以用线程池就够了
    （``cut_video`` 全程在 ``subprocess.run`` 里等，解释器能腾出手来）。

    实测：有几支的关键帧离裁剪点太远，只能重编码切在精确帧上，
    单支要二三十分钟。串行的话全库要四五个小时，并发能压到一小时以内。
    """
    def one(item):
        path = Path(item.path)
        if not path.is_absolute():
            path = root_dir / path
        return adcut_cut.cut_video(
            path, cut_at=item.cut_at, max_snap=max_snap, accurate=accurate,
        )

    if jobs <= 1 or len(targets) <= 1:
        return [one(item) for item in targets]

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        # 保持和输入一样的顺序，输出才好读
        return list(pool.map(one, targets))


@adcut_app.command("sheet")
def adcut_sheet(
    video: str = typer.Argument(..., help="哪个视频"),
    at: str = typer.Option("0", "--at", help="想看的时间点（秒或 00:01:20）"),
    span: float = typer.Option(60.0, "--span", help="前后一共看多少秒"),
    interval: float = typer.Option(3.0, "--interval", help="每格几秒"),
    columns: int = typer.Option(5, "--columns", help="每行几格"),
    rows: int = typer.Option(4, "--rows", help="几行"),
    out: Optional[Path] = typer.Option(None, "--out", help="输出 PNG 路径"),
) -> None:
    """生成时间轴拼图，肉眼确认广告在哪结束。

    人工划刀时最耗时的是拖进度条。拼图把候选区间切成小图，
    一眼就能看出「广告卡」和「正片」的分界在哪一格。

    这套 ffmpeg 没有 drawtext，烧不了时间戳 —— 时间按格数推算，
    输出里会给出「格号 ↔ 时间」的对照表。
    """
    path = Path(video).expanduser()
    if not path.exists():
        _die(f"文件不存在：{path}")
    center = _parse_time(at)
    start = max(0.0, center - span / 2.0)
    try:
        target = adcut_cut.contact_sheet(
            path, at=center, span=span, interval=interval,
            columns=columns, rows=rows, out=out,
        )
    except Exception as exc:
        _die(f"生成拼图失败：{exc}")

    typer.secho(f"拼图：{target}", fg=typer.colors.GREEN)
    typer.secho(
        f"从 {start:.1f}s 开始，每格 {interval:g} 秒，{columns}×{rows} 格：",
        fg=typer.colors.CYAN,
    )
    for row in range(rows):
        cells = []
        for col in range(columns):
            index = row * columns + col
            cells.append(f"第{row * columns + col + 1}格={start + index * interval:.0f}s")
        typer.echo("  " + "  ".join(cells))


@adcut_app.command("templates")
def adcut_templates_cmd(
    templates: Optional[Path] = typer.Option(None, "--templates", help="模板库路径"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出"),
) -> None:
    """列出模板库里已知的广告。"""
    bank, templates_path = _load_ad_bank(templates)
    if json_out:
        typer.echo(json.dumps(
            {"path": str(templates_path),
             "templates": [t.model_dump(mode="json") for t in bank.templates],
             "negatives": [n.model_dump(mode="json") for n in bank.negatives]},
            ensure_ascii=False, indent=2,
        ))
        return

    typer.secho(f"模板库：{templates_path}", bold=True)
    if not bank.templates:
        typer.secho("还没有任何模板 —— 用 ad-cut learn 学一支广告进来。",
                    fg=typer.colors.YELLOW)
        return
    typer.secho(f"共 {len(bank.templates)} 支已知广告：", fg=typer.colors.CYAN)
    for template in bank.templates:
        typer.secho(
            f"  [{template.id}] {template.label or '（未命名）':20s} "
            f"{template.seconds_label:>7s}  确认 {template.samples} 次  "
            f"{template.status.value}",
            fg=typer.colors.GREEN if template.status.value == "confirmed" else typer.colors.YELLOW,
        )
        if template.sample:
            typer.echo(f"      来源：{template.sample}")
    if bank.negatives:
        typer.secho(f"\n负样本 {len(bank.negatives)} 条：", fg=typer.colors.CYAN)
        for negative in bank.negatives:
            typer.echo(f"  [{negative.id}] {negative.reason or '（无说明）'}")


@adcut_app.command("cache")
def adcut_cache_cmd(
    clear: bool = typer.Option(False, "--clear", help="清空指纹缓存"),
) -> None:
    """管理指纹缓存（纯加速用，删了不影响正确性）。"""
    root = adcut_cache.cache_dir()
    if clear:
        count = adcut_cache.clear()
        typer.secho(f"已清空 {count} 个缓存文件。", fg=typer.colors.GREEN)
        return
    files = list(root.glob("*.json"))
    total = sum(item.stat().st_size for item in files)
    typer.secho(f"缓存目录：{root}", bold=True)
    typer.secho(f"共 {len(files)} 个缓存文件，占 {total / 1024:.1f} KB。")


def main() -> None:
    _force_utf8_output()
    app()


if __name__ == "__main__":
    main()
