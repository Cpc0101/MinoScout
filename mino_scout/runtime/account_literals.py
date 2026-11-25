"""脚本里的硬编码账号字面量。执行前硬门禁与静态检查共用同一套判定。

账号、邮箱、手机号、密码只能来自 Nexus 号池（case.nexus.account）。页面文案里的
邮箱/电话确实需要断言时，在同一行写 `# account-literal-ok: 原因` 显式豁免。
"""
from __future__ import annotations

import io
import re
import tokenize

MARKER = "# account-literal-ok:"
EMAIL = re.compile(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
PASSWORD = re.compile(r"(?i)(password|passwd|pwd)\s*=\s*(['\"]).+?\2")


def _string_tokens(source: str) -> dict[int, str]:
    """只检查字符串字面量；注释、标识符和普通代码不算账号。"""
    found: dict[int, str] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.STRING:
                found[token.start[0]] = found.get(token.start[0], "") + token.string
    except (tokenize.TokenError, IndentationError):
        for number, line in enumerate(source.splitlines(), 1):
            if not line.lstrip().startswith("#"):
                found[number] = line
    return found


def find_account_literals(source: str) -> list[tuple[int, str]]:
    """返回 [(行号, 类别)]；豁免行不返回。"""
    hits: list[tuple[int, str]] = []
    raw_lines = source.splitlines()
    for number, literal in sorted(_string_tokens(source).items()):
        raw = raw_lines[number - 1] if 0 < number <= len(raw_lines) else ""
        if MARKER in raw:
            continue
        if EMAIL.search(literal):
            hits.append((number, "email"))
        elif PHONE.search(literal):
            hits.append((number, "phone"))
        elif PASSWORD.search(raw) and not raw.lstrip().startswith("#"):
            hits.append((number, "password"))
    return hits
