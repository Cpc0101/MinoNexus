"""确认脚本能被执行。不限制导入、调用或属性访问。"""
from __future__ import annotations

import ast
import io
import re
import tokenize

_MAX_CHARS = 60000
_ACCOUNT_EMAIL = re.compile(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}")
_ACCOUNT_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ACCOUNT_PASSWORD = re.compile(r"(?i)(password|passwd|pwd)\s*=\s*(['\"]).+?\2")
_ACCOUNT_LITERAL_OK = "# account-literal-ok:"


class ScriptRejected(ValueError):
    reason_code = "script_rejected"

    def __init__(self, message: str, *, line: int = 0, reason_code: str = ""):
        self.line = int(line or 0)
        self.reason_code = reason_code or type(self).reason_code
        super().__init__(message)


def _string_token_lines(text: str) -> dict[int, str]:
    """只把字符串字面量当作账号候选，注释和标识符不算。"""
    lines: dict[int, str] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.STRING:
                lines.setdefault(token.start[0], "")
                lines[token.start[0]] += token.string
    except (tokenize.TokenError, IndentationError):
        for number, line in enumerate(text.splitlines(), 1):
            if not line.lstrip().startswith("#"):
                lines[number] = line
    return lines


def _check_account_literals(text: str) -> None:
    raw_lines = text.splitlines()
    for number, literal in sorted(_string_token_lines(text).items()):
        raw = raw_lines[number - 1] if 0 < number <= len(raw_lines) else ""
        if _ACCOUNT_LITERAL_OK in raw:
            continue
        if _ACCOUNT_EMAIL.search(literal) or _ACCOUNT_PHONE.search(literal):
            raise ScriptRejected(
                "脚本不得硬编码账号、邮箱或手机号；改用 case.nexus.account(...) 从 Nexus 号池租用。"
                "确属页面文案断言时在该行加 # account-literal-ok: 说明",
                line=number, reason_code="hardcoded_account_literal",
            )
    for number, line in enumerate(raw_lines, 1):
        if _ACCOUNT_LITERAL_OK in line or line.lstrip().startswith("#"):
            continue
        if _ACCOUNT_PASSWORD.search(line):
            raise ScriptRejected(
                "脚本不得硬编码密码；改用 case.nexus.account(...) 从 Nexus 号池取凭据",
                line=number, reason_code="hardcoded_account_literal",
            )


def check_script(source: str, *, role: str) -> None:
    """role 是 run、pretest 或 posttest。空脚本不检查。"""
    text = str(source or "")
    if not text.strip():
        return
    if len(text) > _MAX_CHARS:
        raise ScriptRejected(f"脚本超过 {_MAX_CHARS} 字", line=1)
    if role not in {"run", "pretest", "posttest"}:
        raise ScriptRejected(f"未知脚本角色 {role}")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise ScriptRejected(exc.msg or "脚本语法错误", line=int(exc.lineno or 0)) from exc
    if not any(isinstance(node, ast.FunctionDef) and node.name == role for node in tree.body):
        raise ScriptRejected(f"脚本要定义 {role}(case)", line=1)
    _check_account_literals(text)
