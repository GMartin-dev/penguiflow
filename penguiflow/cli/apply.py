"""Implementation of `penguiflow apply`."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import NamedTuple

import click

from .generate import (
    _normalise_package_name,
    _render_template,
    _tool_render,
    _tool_test_render,
    run_generate,
)
from .init import CLIError
from .spec import Spec, load_spec
from .spec_errors import SpecValidationError


class ApplyResult(NamedTuple):
    """Result of running `penguiflow apply`."""

    success: bool
    changed: list[str]
    skipped: list[str]
    errors: list[str]
    diffs: list[str]
    project_dir: Path
    package_name: str


def _managed_start(name: str) -> str:
    return f"# <penguiflow:managed {name}>"


def _managed_end(name: str) -> str:
    return f"# </penguiflow:managed {name}>"


def _extract_managed_block(content: str, name: str) -> str | None:
    start = _managed_start(name)
    end = _managed_end(name)
    start_idx = content.find(start)
    if start_idx < 0:
        return None
    body_start = content.find("\n", start_idx)
    if body_start < 0:
        return None
    end_idx = content.find(end, body_start + 1)
    if end_idx < 0:
        return None
    return content[body_start + 1 : end_idx]


def _replace_managed_block(content: str, name: str, replacement_body: str) -> str | None:
    start = _managed_start(name)
    end = _managed_end(name)
    start_idx = content.find(start)
    if start_idx < 0:
        return None
    body_start = content.find("\n", start_idx)
    if body_start < 0:
        return None
    end_idx = content.find(end, body_start + 1)
    if end_idx < 0:
        return None
    return content[: body_start + 1] + replacement_body + content[end_idx:]


def _replace_triple_quoted_assignment(content: str, name: str, value: str) -> str | None:
    pattern = re.compile(rf'^{re.escape(name)} = """.*?"""', re.MULTILINE | re.DOTALL)
    replacement = f'{name} = """{value}"""'
    updated, count = pattern.subn(lambda _match: replacement, content, count=1)
    return updated if count else None


def _write_if_changed(
    path: Path,
    content: str,
    *,
    check: bool,
    show_diff: bool,
    changed: list[str],
    operations: dict[str, str],
    diffs: list[str],
) -> None:
    exists = path.exists()
    old = path.read_text(encoding="utf-8") if exists else ""
    if old == content:
        return
    rendered_path = path.as_posix()
    changed.append(rendered_path)
    operations[rendered_path] = "update" if exists else "create"
    if show_diff:
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=f"{path.as_posix()} (current)",
            tofile=f"{path.as_posix()} (desired)",
        )
        rendered = "".join(diff)
        if rendered:
            diffs.append(rendered)
    if not check:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _apply_tools_registry(
    project_dir: Path,
    package_name: str,
    spec: Spec,
    *,
    check: bool,
    force: bool,
    show_diff: bool,
    changed: list[str],
    operations: dict[str, str],
    skipped: list[str],
    diffs: list[str],
) -> None:
    tools_dir = project_dir / "src" / package_name / "tools"
    tools = [_tool_render(tool) for tool in spec.tools]

    for tool in tools:
        path = tools_dir / f"{tool.name}.py"
        if path.exists():
            skipped.append(path.as_posix())
            continue
        content = _render_template("tool.py.jinja", {"tool": tool})
        _write_if_changed(
            path,
            content,
            check=check,
            show_diff=show_diff,
            changed=changed,
            operations=operations,
            diffs=diffs,
        )

    init_path = tools_dir / "__init__.py"
    desired = _render_template("tools_init.py.jinja", {"agent_name": spec.agent.name, "tools": tools})
    if not init_path.exists():
        _write_if_changed(
            init_path,
            desired,
            check=check,
            show_diff=show_diff,
            changed=changed,
            operations=operations,
            diffs=diffs,
        )
        return

    current = init_path.read_text(encoding="utf-8")
    updated = current
    block_names = ["tool-imports", "tool-exports", "tool-registry", "tool-nodes"]
    if all(_extract_managed_block(current, name) is not None for name in block_names):
        for name in block_names:
            desired_body = _extract_managed_block(desired, name)
            if desired_body is None:  # pragma: no cover - template invariant
                continue
            replaced = _replace_managed_block(updated, name, desired_body)
            if replaced is not None:
                updated = replaced
    elif force:
        updated = desired
    else:
        skipped.append(f"{init_path.as_posix()} (missing managed markers)")
        return

    _write_if_changed(
        init_path,
        updated,
        check=check,
        show_diff=show_diff,
        changed=changed,
        operations=operations,
        diffs=diffs,
    )


def _apply_planner_prompts(
    project_dir: Path,
    package_name: str,
    spec: Spec,
    *,
    check: bool,
    show_diff: bool,
    changed: list[str],
    operations: dict[str, str],
    skipped: list[str],
    diffs: list[str],
) -> None:
    planner_path = project_dir / "src" / package_name / "planner.py"
    if not planner_path.exists():
        skipped.append(planner_path.as_posix())
        return

    current = planner_path.read_text(encoding="utf-8")
    updated = current
    system_prompt_body = f'SYSTEM_PROMPT_EXTRA = """{spec.planner.system_prompt_extra.strip()}"""\n'
    if _extract_managed_block(updated, "system-prompt") is not None:
        replaced = _replace_managed_block(updated, "system-prompt", system_prompt_body)
        if replaced is not None:
            updated = replaced
    else:
        replaced = _replace_triple_quoted_assignment(
            updated,
            "SYSTEM_PROMPT_EXTRA",
            spec.planner.system_prompt_extra.strip(),
        )
        if replaced is None:
            skipped.append(f"{planner_path.as_posix()} (SYSTEM_PROMPT_EXTRA not found)")
        else:
            updated = replaced

    if spec.agent.flags.memory and spec.planner.memory_prompt is not None:
        memory_prompt_body = f'MEMORY_PROMPT = """{spec.planner.memory_prompt.strip()}"""\n'
        if _extract_managed_block(updated, "memory-prompt") is not None:
            replaced = _replace_managed_block(updated, "memory-prompt", memory_prompt_body)
            if replaced is not None:
                updated = replaced
        else:
            replaced = _replace_triple_quoted_assignment(updated, "MEMORY_PROMPT", spec.planner.memory_prompt.strip())
            if replaced is not None:
                updated = replaced

    _write_if_changed(
        planner_path,
        updated,
        check=check,
        show_diff=show_diff,
        changed=changed,
        operations=operations,
        diffs=diffs,
    )


def _apply_agent_yaml(
    project_dir: Path,
    spec_path: Path,
    *,
    check: bool,
    show_diff: bool,
    changed: list[str],
    operations: dict[str, str],
    diffs: list[str],
) -> None:
    _write_if_changed(
        project_dir / "agent.yaml",
        spec_path.read_text(encoding="utf-8"),
        check=check,
        show_diff=show_diff,
        changed=changed,
        operations=operations,
        diffs=diffs,
    )


def _apply_tool_tests(
    project_dir: Path,
    package_name: str,
    spec: Spec,
    *,
    check: bool,
    show_diff: bool,
    changed: list[str],
    operations: dict[str, str],
    skipped: list[str],
    diffs: list[str],
) -> None:
    tests_dir = project_dir / "tests" / "test_tools"
    for tool in spec.tools:
        render = _tool_test_render(tool)
        path = tests_dir / f"test_{render.name}.py"
        if path.exists():
            skipped.append(path.as_posix())
            continue
        content = _render_template(
            "test_tool.py.jinja",
            {
                "tool": render,
                "package_name": package_name,
            },
        )
        _write_if_changed(
            path,
            content,
            check=check,
            show_diff=show_diff,
            changed=changed,
            operations=operations,
            diffs=diffs,
        )


def _resolve_project_dir(spec: Spec, spec_path: Path, output_dir: Path | None) -> Path:
    """Resolve the project directory for apply.

    `generate` always writes to OUTPUT_DIR/agent.name. For `apply`, the common
    workflow is running from an existing generated project root with
    `--spec agent.yaml`, so default to cwd when it already looks like the
    generated project.
    """
    package_name = _normalise_package_name(spec.agent.name)
    if output_dir is not None:
        if (output_dir / "src" / package_name).is_dir() and (output_dir / "pyproject.toml").exists():
            return output_dir
        return output_dir / spec.agent.name

    cwd = Path.cwd()
    if (cwd / "src" / package_name).is_dir() and (cwd / "pyproject.toml").exists():
        return cwd

    spec_parent = spec_path.resolve().parent
    if (spec_parent / "src" / package_name).is_dir() and (spec_parent / "pyproject.toml").exists():
        return spec_parent

    return cwd / spec.agent.name


def run_apply(
    *,
    spec_path: Path,
    output_dir: Path | None = None,
    check: bool = False,
    diff: bool = False,
    force: bool = False,
    quiet: bool = False,
    verbose: bool = False,
) -> ApplyResult:
    """Reconcile an existing project with an agent spec without overwriting tool implementations."""

    if verbose:
        click.echo(f"Loading spec from {spec_path}...")

    try:
        spec = load_spec(spec_path)
    except SpecValidationError:
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        raise CLIError(f"Failed to load spec: {exc}") from exc

    package_name = _normalise_package_name(spec.agent.name)
    project_dir = _resolve_project_dir(spec, spec_path, output_dir)
    changed: list[str] = []
    operations: dict[str, str] = {}
    skipped: list[str] = []
    errors: list[str] = []
    diffs: list[str] = []

    if not project_dir.exists():
        generated = run_generate(
            spec_path=spec_path,
            output_dir=output_dir,
            dry_run=check,
            force=False,
            quiet=True,
            verbose=verbose,
        )
        success = generated.success and not (check and generated.created)
        if not quiet:
            label = "Would create" if check else "Created"
            for path in generated.created:
                click.echo(f"✓ {label} {path}")
            for path in generated.skipped:
                click.echo(f"⚠ Skipped {path}")
            for error in generated.errors:
                click.echo(f"✗ Error: {error}", err=True)
        return ApplyResult(
            success=success,
            changed=generated.created,
            skipped=generated.skipped,
            errors=generated.errors,
            diffs=[],
            project_dir=generated.project_dir,
            package_name=generated.package_name,
        )

    try:
        _apply_tools_registry(
            project_dir,
            package_name,
            spec,
            check=check,
            force=force,
            show_diff=diff,
            changed=changed,
            operations=operations,
            skipped=skipped,
            diffs=diffs,
        )
        _apply_tool_tests(
            project_dir,
            package_name,
            spec,
            check=check,
            show_diff=diff,
            changed=changed,
            operations=operations,
            skipped=skipped,
            diffs=diffs,
        )
        _apply_planner_prompts(
            project_dir,
            package_name,
            spec,
            check=check,
            show_diff=diff,
            changed=changed,
            operations=operations,
            skipped=skipped,
            diffs=diffs,
        )
        _apply_agent_yaml(
            project_dir,
            spec_path,
            check=check,
            show_diff=diff,
            changed=changed,
            operations=operations,
            diffs=diffs,
        )
    except (CLIError, SpecValidationError):
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        errors.append(str(exc))

    success = len(errors) == 0
    if check and changed:
        success = False

    if not quiet:
        for path in changed:
            operation = operations.get(path, "update")
            if check:
                label = "Would create" if operation == "create" else "Would update"
            else:
                label = "Created" if operation == "create" else "Updated"
            click.echo(f"✓ {label} {path}")
        for path in skipped:
            if " (" in path:
                click.echo(f"⚠ Skipped {path}")
            else:
                click.echo(f"✓ Preserved {path}")
        for rendered_diff in diffs:
            click.echo(rendered_diff, nl=False)
        for error in errors:
            click.echo(f"✗ Error: {error}", err=True)

    return ApplyResult(
        success=success,
        changed=changed,
        skipped=skipped,
        errors=errors,
        diffs=diffs,
        project_dir=project_dir,
        package_name=package_name,
    )
