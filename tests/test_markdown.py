from taskprinter import markdown as md
from taskprinter import render
from taskprinter.markdown import Run, Style


def styles(text):
    return [(r.text, r.style) for r in md.parse_inline(text)]


def test_inline_styles():
    assert styles("a **b** *c* ~~d~~ `e`") == [
        ("a ", Style()), ("b", Style(bold=True)), (" ", Style()), ("c", Style(italic=True)),
        (" ", Style()), ("d", Style(strike=True)), (" ", Style()), ("e", Style(code=True)),
    ]
    assert styles("***both***")[0] == ("both", Style(bold=True, italic=True))
    assert styles("[site](https://x.y)") == [("site", Style(underline=True))]


def test_literal_markers_stay_literal():
    assert md.parse_inline("5 * 3 * 2") == [Run("5 * 3 * 2")]
    assert md.parse_inline("snake_case_name") == [Run("snake_case_name")]
    assert md.parse_inline(r"\*not italic\*") == [Run("*not italic*")]
    assert md.parse_inline("unclosed **bold") == [Run("unclosed **bold")]


def test_blocks():
    blocks = md.parse(
        "# Title\nline one\nline two\n\n- a\n  - nested\n1. first\n- [ ] todo\n- [x] done\n"
        "> quoted\n---\n```\ncode  here\n```"
    )
    kinds = [(b.kind, b.level, b.marker, b.checked) for b in blocks]
    assert kinds == [
        ("heading", 1, "", None), ("para", 0, "", None),
        ("item", 0, "•", None), ("item", 1, "•", None), ("item", 0, "1.", None),
        ("item", 0, "", False), ("item", 0, "", True),
        ("quote", 0, "", None), ("rule", 0, "", None), ("code", 0, "", None),
    ]
    assert blocks[1].runs == [Run("line one\nline two")]  # single newlines kept
    assert blocks[-1].text == "code  here"


def test_plain():
    assert md.plain("# Hi **there**\n- one") == "Hi there one"


def test_markdown_renders_and_wraps():
    img = render.note("# Big\n" + "word " * 80 + "\n- item\n```\nx = 1\n```", 384)
    assert render.note("x", 384, fmt=render.Format(font="serif", border="double")).width == 384
    assert img.width == 384 and img.height > 300
    plain = render.note("**not bold**", 384, markdown=False)
    assert plain.width == 384
