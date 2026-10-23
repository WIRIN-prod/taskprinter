"""A small Markdown parser tuned for receipts.

Supported: # / ## / ### headings, **bold**, *italic*, ~~strike~~, `code`, [links](url),
- bullets, 1. numbered lists, - [ ] / - [x] task items (nested by indentation),
> quotes, ``` fenced code, --- rules. Unlike CommonMark, a single newline inside a
paragraph is kept as a line break, which is what you want for short notes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Style:
    bold: bool = False
    italic: bool = False
    code: bool = False
    strike: bool = False
    underline: bool = False


@dataclass(frozen=True)
class Run:
    text: str
    style: Style = Style()


@dataclass
class Block:
    kind: str  # heading | para | item | quote | code | rule
    runs: list[Run]
    level: int = 0  # heading level, or list nesting depth
    marker: str = ""  # "•", "1.", or "" for task items
    checked: bool | None = None  # task items only
    text: str = ""  # raw text for code blocks
    align: str = ""  # "", "left", "center" or "right" ("" = the print's default)


_INLINE = re.compile(r"(\\[\\`*_~\[\]()#>-]|`[^`\n]+`|\*\*|__|~~|\*|_|\[[^\]\n]+\]\([^)\s]+\))")
_TOGGLES = {"**": "bold", "__": "bold", "*": "italic", "_": "italic", "~~": "strike"}


def parse_inline(text: str, base: Style = Style()) -> list[Run]:
    """Split text into styled runs. Unmatched markers stay literal."""
    tokens = _INLINE.split(text)
    runs: list[Run] = []
    style = base
    open_flags: set[str] = set()

    def emit(chunk: str, s: Style) -> None:
        if not chunk:
            return
        if runs and runs[-1].style == s:
            runs[-1] = Run(runs[-1].text + chunk, s)
        else:
            runs.append(Run(chunk, s))

    pos = 0  # character offset of the current token in `text`
    for i, tok in enumerate(tokens):
        start = pos
        pos += len(tok)
        if i % 2 == 0:  # plain text
            emit(tok, style)
            continue
        if tok.startswith("\\"):
            emit(tok[1:], style)
        elif tok.startswith("`"):
            emit(tok[1:-1], replace(style, code=True))
        elif tok.startswith("["):
            label = tok[1:tok.index("](")]
            for run in parse_inline(label, replace(style, underline=True)):
                emit(run.text, run.style)
        else:
            flag = _TOGGLES[tok]
            before = text[start - 1] if start > 0 else " "
            after = text[pos] if pos < len(text) else " "
            if tok in ("_", "__") and before.isalnum() and after.isalnum():
                emit(tok, style)  # snake_case words stay literal
                continue
            if flag in open_flags and not before.isspace():
                open_flags.discard(flag)
                style = replace(style, **{flag: getattr(base, flag)})
            elif flag not in open_flags and not after.isspace() and tok in text[pos:]:
                open_flags.add(flag)
                style = replace(style, **{flag: True})
            else:
                emit(tok, style)
    return runs


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(?:\[([ xX])\]\s+)?(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_FENCE = re.compile(r"^\s*```")


def parse(text: str) -> list[Block]:
    blocks: list[Block] = []
    para: list[str] = []
    quote: list[str] = []
    code: list[str] | None = None

    def flush() -> None:
        if para:
            blocks.append(Block("para", parse_inline("\n".join(para))))
            para.clear()
        if quote:
            blocks.append(Block("quote", parse_inline("\n".join(quote), Style(italic=True))))
            quote.clear()

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if code is not None:
            if _FENCE.match(line):
                blocks.append(Block("code", [], text="\n".join(code)))
                code = None
            else:
                code.append(line.expandtabs(4))
            continue
        if _FENCE.match(line):
            flush()
            code = []
            continue
        if not line.strip():
            flush()
            continue
        if m := _HEADING.match(line):
            flush()
            blocks.append(Block("heading", parse_inline(m.group(2)), level=min(3, len(m.group(1)))))
            continue
        if _RULE.match(line):
            flush()
            blocks.append(Block("rule", []))
            continue
        if m := _ITEM.match(line.expandtabs(4)):
            flush()
            indent, marker, box, body = m.groups()
            level = min(3, len(indent) // 2)
            if box is not None:
                blocks.append(Block("item", parse_inline(body), level, "", checked=box.lower() == "x"))
            else:
                blocks.append(Block("item", parse_inline(body), level, "•" if marker in "-*+" else marker))
            continue
        if m := _QUOTE.match(line):
            if para:
                flush()
            quote.append(m.group(1))
            continue
        if quote:
            flush()
        para.append(line.strip())

    if code is not None:  # unterminated fence: still print it
        blocks.append(Block("code", [], text="\n".join(code)))
    flush()
    return blocks


def plain(text: str) -> str:
    """Markdown stripped to plain text (for job titles and the like)."""
    out = []
    for block in parse(text):
        body = block.text if block.kind == "code" else "".join(r.text for r in block.runs)
        if body:
            out.append(body)
    return " ".join(" ".join(out).split())
