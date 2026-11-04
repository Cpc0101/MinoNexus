"""在仓库检出里定位一条用例的脚本（Workbench 查看 / 编辑用）。只认 tests/，不回退渠道文件。不参与执行。"""
from __future__ import annotations

from pathlib import Path

_PLATFORMS = frozenset({"android", "ios", "web", "mac", "win"})


class ScriptPackError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def platform_script_name(name: str) -> bool:
    text = str(name or "")
    if not text.endswith(".py"):
        return False
    stem = text[:-3]
    if "." not in stem:
        return False
    return stem.rsplit(".", 1)[-1].lower() in _PLATFORMS


def _relative(checkout: Path, path: Path) -> str:
    root = checkout.resolve()
    resolved = path.resolve()
    if root != resolved and root not in resolved.parents:
        raise ScriptPackError("脚本路径超出仓库", "script_path_invalid")
    rel = resolved.relative_to(root).as_posix()
    if not rel or ".." in Path(rel).parts:
        raise ScriptPackError("脚本路径超出仓库", "script_path_invalid")
    return rel


def locate_case_script(checkout: Path, case_id: str, script_path: str = "") -> Path:
    """一个 case_id 对应 tests/<需求ID>/<case_id>.py。渠道后缀文件直接拒绝。"""
    cid = str(case_id or "").strip()
    if not cid or cid in {".", ".."} or "/" in cid or "\\" in cid:
        raise ScriptPackError("用例没有可用的脚本文件名", "script_missing")
    explicit = str(script_path or "").strip()
    if explicit:
        from mino_nexus.casework.script_workspace import safe_script_path

        try:
            rel = safe_script_path(explicit)
        except ValueError as exc:
            raise ScriptPackError("脚本路径必须是仓库内的 .py 相对路径", "script_path_invalid") from exc
        name = Path(rel).name
        if platform_script_name(name):
            raise ScriptPackError(f"脚本 {rel} 带渠道后缀，不会改跑渠道文件", "script_channel_file")
        if not rel.startswith("tests/") or name != f"{cid}.py":
            raise ScriptPackError(f"脚本必须是 tests/<需求ID>/{cid}.py，不能使用 {rel}", "script_path_invalid")
        path = (checkout / rel).resolve()
        _relative(checkout, path)
        if not path.is_file():
            raise ScriptPackError(f"仓库里没有 {rel}", "script_missing")
        return path

    matches: list[Path] = []
    channel: list[Path] = []
    tests = checkout / "tests"
    if tests.is_dir():
        for path in tests.rglob("*.py"):
            if not path.is_file():
                continue
            if path.name == f"{cid}.py":
                matches.append(path)
            elif platform_script_name(path.name) and path.name.startswith(f"{cid}."):
                channel.append(path)
    cases = checkout / "cases"
    if cases.is_dir():
        for path in cases.glob(f"{cid}*.py"):
            if path.is_file() and (path.name == f"{cid}.py" or platform_script_name(path.name)):
                channel.append(path)
    if len(matches) > 1:
        shown = ", ".join(_relative(checkout, path) for path in matches[:8])
        raise ScriptPackError(f"tests 里有多份 {cid}.py：{shown}", "script_ambiguous")
    if len(matches) == 1:
        return matches[0]
    if channel:
        shown = ", ".join(_relative(checkout, path) for path in channel[:8])
        raise ScriptPackError(
            f"只有渠道后缀脚本（{shown}），不会改跑这些文件。需要 tests/<需求ID>/{cid}.py",
            "script_channel_only",
        )
    raise ScriptPackError(f"没有 tests/<需求ID>/{cid}.py", "script_missing")
