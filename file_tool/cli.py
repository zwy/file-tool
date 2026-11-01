"""file-tool CLI。

常用命令：
  file-tool rename /Volumes/P7000Z/R              # 预览改名计划（dry-run）
  file-tool rename /Volumes/P7000Z/R --apply      # 真正执行改名
  file-tool scan /Volumes/P7000Z/R                # 只看识别结果统计
  file-tool rules show                            # 打印当前规则集
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from file_tool import engine
from file_tool import archive as archive_mod
from file_tool import clean as clean_mod
from file_tool import isolate as isolate_mod
from file_tool import merge as merge_mod
from file_tool import rename_files as rename_files_mod
from file_tool import tidy as tidy_mod
from file_tool.merge import MergeAction, MergeVerdict
from file_tool.models import Action, RuleSet
from file_tool.tidy import StepKind

DEFAULT_RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"

app = typer.Typer(
    help="本地文件整理 CLI：批量重命名目录、番号规范化",
    no_args_is_help=True,
)
rules_app = typer.Typer(help="规则集管理", no_args_is_help=True)
app.add_typer(rules_app, name="rules")


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
        counts = plan.summary()
        typer.secho(
            f"\n预览模式：{counts['rename']} 个待改名 / {counts['keep']} 个已是规范名 / "
            f"{counts['skip']} 个未匹配 / {counts['conflict']} 个冲突。"
            f" 确认无误后加 --apply 执行。",
            fg=typer.colors.YELLOW,
        )
        return

    done = engine.apply_plan(root_dir, plan)
    typer.secho(f"已改名 {len(done)} 个目录。", fg=typer.colors.GREEN)
    for old, new in done:
        typer.echo(f"  {old}  ->  {new}")


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
             "items": [i.model_dump(mode="json") for i in plan.items]},
            ensure_ascii=False, indent=2,
        ))
    else:
        _print_file_plan(plan)

    if not apply:
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
    include_partial: bool = typer.Option(
        False, "--include-partial", help="同时处理部分重叠的组（只并入不冲突的文件）"
    ),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出计划"),
) -> None:
    """识别重复目录并合并。

    判定依据是**文件内容指纹**，不是目录名 —— 带 (1) 的目录未必是复制副本，
    也可能是同作品的不同画质版本或分P。内容不同的目录一律保留，绝不合并。
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
        counts = plan.summary()
        typer.secho(
            f"\n预览模式：{counts['duplicate']} 组内容完全一致（可合并）、"
            f"{counts['partial']} 组部分重叠、{counts['different']} 组内容不同（不合并）。"
            f" 确认无误后加 --apply 执行。",
            fg=typer.colors.YELLOW,
        )
        return

    results = merge_mod.apply_merge_plan(
        root_dir, plan, only_duplicates=not include_partial
    )
    merged = [r for r in results if r["action"] == MergeAction.MERGE.value]
    kept = [r for r in results if r["action"] == MergeAction.KEEP.value]
    skipped = [r for r in results if r["action"] == MergeAction.SKIP.value]
    typer.secho(
        f"合并完成：{len(merged)} 个副本目录已并入，"
        f"{len(kept)} 个主目录保留，{len(skipped)} 个未处理。",
        fg=typer.colors.GREEN,
    )
    for r in merged:
        typer.echo(f"  {r['dir']}  ->  {r['base']}  (并入 {len(r['moved'])} 个文件)")
        if r["skipped"]:
            typer.secho(
                f"      冲突保留 {len(r['skipped'])} 个同名不同内容文件", fg=typer.colors.YELLOW
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
             "blocked": plan.blocked},
            ensure_ascii=False, indent=2,
        ))
    else:
        _print_archive_plan(plan)

    if not apply:
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


@app.command()
def tidy(
    root: str = typer.Argument(..., help="要整理的目录"),
    apply: bool = typer.Option(False, "--apply", help="真正执行（默认只预览）"),
    rules: Optional[Path] = typer.Option(None, "--rules", help="规则文件路径"),
    include_partial: bool = typer.Option(
        False, "--include-partial", help="同时处理部分重叠的组"
    ),
    isolate: Optional[str] = typer.Option(
        None, "--isolate", help="把不符合番号规范的目录挪到这里（如 /Volumes/P7000Z/GC）"
    ),
    keep_forks: bool = typer.Option(False, "--keep-forks", help="保留 ._ 资源叉垃圾文件"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出计划"),
) -> None:
    """智能整理一个目录：自动判断形态并编排动作。

    自动识别目录形态并决定做什么：

    - flat   （平铺文件）      → 分装进番号目录
    - nested （子目录是番号）  → 规范化目录名 + 合并重复目录
    - mixed  （两者都有）      → 先分装再规范化
    - loose  （子目录不像番号）→ 只报告，不动

    执行顺序固定为 clean → isolate → merge → archive → rename。
    """
    root_dir = _resolve_root(root)
    ruleset = _load_rules(rules)
    isolate_to = Path(isolate).expanduser() if isolate else None
    if isolate_to is not None and isolate_to.exists() and not isolate_to.is_dir():
        _die(f"隔离目标不是目录：{isolate_to}")
    try:
        plan = tidy_mod.build_tidy_plan(root_dir, ruleset, isolate_to=isolate_to)
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
        typer.secho(
            f"\n预览模式：识别为 {plan.shape.value} 形态，"
            f"计划 {plan.total_actions} 个动作。确认无误后加 --apply 执行。",
            fg=typer.colors.YELLOW,
        )
        return

    result = tidy_mod.apply_tidy_plan(
        root_dir, ruleset, plan,
        include_partial=include_partial,
        clean_forks=not keep_forks,
        isolate_to=isolate_to,
    )
    typer.secho(
        f"整理完成：隔离 {result['isolated']} 个目录，"
        f"合并 {result['merged']} 个重复目录，"
        f"分装 {result['archived']} 个文件，"
        f"改名 {result['renamed']} 个目录，"
        f"清理 {result['forks_removed']} 个垃圾文件。",
        fg=typer.colors.GREEN,
    )
    if result["conflicts"]:
        typer.secho(
            f"注意：{result['conflicts']} 项因目标已存在而未处理（未覆盖）。",
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
    target: str = typer.Option(..., "--to", help="把不符合的目录挪到这里（如 /Volumes/P7000Z/GC）"),
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
             "conflicts": plan.conflicts},
            ensure_ascii=False, indent=2,
        ))
    else:
        _print_isolation_plan(plan)

    if not apply:
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


@app.command()
def clean(
    root: str = typer.Argument(..., help="要清理的目录"),
    apply: bool = typer.Option(False, "--apply", help="真正执行（默认只预览）"),
    no_recursive: bool = typer.Option(False, "--no-recursive", help="只清理顶层，不递归"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出计划"),
) -> None:
    """递归清理 macOS 资源叉垃圾（``._xxx`` / ``.DS_Store``）。

    这些是 exFAT 等非 HFS+ 卷上的元数据副本，对媒体文件没有价值。
    只删资源叉，真实文件和目录一律不动。
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
