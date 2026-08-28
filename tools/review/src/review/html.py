"""Render a review report to one self-contained HTML page."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final, cast

import click
from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.token import Token

from orbit.core import open_url

PROG_NAME: Final = "dev/review-html.py"

_PER_LENS_HEADING = "Per-lens reports"
_SEVERITIES = ("blocking", "should-fix", "minor")

_STYLE = """\
:root {
  color-scheme: light dark;
  --bg: #fdfdfc; --fg: #1c1c1a; --muted: #5c5c56;
  --card: #ffffff; --line: #dcdcd4; --code: #f2f2ee;
  --blocking: #a4243b; --should-fix: #9a6a00; --minor: #4a6d8c;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1a1b1a; --fg: #e6e6e1; --muted: #a3a39a;
    --card: #232423; --line: #3a3b39; --code: #2c2d2b;
    --blocking: #ff8a9b; --should-fix: #e3b341; --minor: #8ab4d8;
  }
}
body {
  background: var(--bg); color: var(--fg);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  line-height: 1.55; margin: 0 auto; max-width: 80ch;
  padding: 2rem 1.25rem 6rem;
}
h1 { font-size: 1.6rem; margin-bottom: 0.2em; }
h2 { border-bottom: 1px solid var(--line); font-size: 1.25rem;
     margin-top: 2.5rem; padding-bottom: 0.2em; }
h3 { font-size: 1.05rem; margin: 0 0 0.6rem; }
code { background: var(--code); border-radius: 4px;
       font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
       font-size: 0.9em; padding: 0.1em 0.35em; }
pre { background: var(--code); border-radius: 6px; overflow-x: auto;
      padding: 0.8rem 1rem; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid var(--line); padding: 0.35rem 0.6rem;
         text-align: left; }
.finding {
  background: var(--card); border: 1px solid var(--line);
  border-left: 4px solid var(--line); border-radius: 8px;
  margin: 1rem 0; padding: 1rem 1.15rem;
}
.finding ul { margin: 0; padding-left: 1.2rem; }
.finding li { margin: 0.25rem 0; }
.severity { font-weight: 600; letter-spacing: 0.01em; }
.severity::before { font-weight: 700; padding-right: 0.35em; }
.severity-blocking { color: var(--blocking); }
.severity-blocking::before { content: "\\25C6"; }
.severity-should-fix { color: var(--should-fix); }
.severity-should-fix::before { content: "\\25B2"; }
.severity-minor { color: var(--minor); }
.severity-minor::before { content: "\\25CF"; }
.finding:has(.severity-blocking) { border-left-color: var(--blocking); }
.finding:has(.severity-should-fix) { border-left-color: var(--should-fix); }
.finding:has(.severity-minor) { border-left-color: var(--minor); }
details { border-top: 1px solid var(--line); margin-top: 2.5rem;
          padding-top: 0.6rem; }
details > summary { cursor: pointer; font-size: 1.25rem; font-weight: 600; }
details[open] > summary { margin-bottom: 1rem; }
"""

_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{style}</style>
</head>
<body>
{body}</body>
</html>
"""


def _html_token(markup: str) -> Token:
    token = Token("html_inline", "", 0)
    token.content = markup
    return token


def _text_token(text: str) -> Token:
    token = Token("text", "", 0)
    token.content = text
    return token


def _code_token(text: str) -> Token:
    token = Token("code_inline", "code", 0)
    token.content = text
    return token


def _decorated_value(label: str, value: Token) -> list[Token]:
    text = value.content
    lead = text[: len(text) - len(text.lstrip())]
    body = text.strip()
    if label == "Severity" and body in _SEVERITIES:
        return [
            _text_token(lead),
            _html_token(f'<span class="severity severity-{body}">'),
            _text_token(body),
            _html_token("</span>"),
        ]
    if label == "Where" and body:
        return [_text_token(lead), _code_token(body)]
    return [value]


def _decorate_labels(children: list[Token]) -> list[Token]:
    out: list[Token] = []
    index = 0
    while index < len(children):
        window = children[index : index + 4]
        if [token.type for token in window] == [
            "strong_open",
            "text",
            "strong_close",
            "text",
        ]:
            out.extend(window[:3])
            out.extend(
                _decorated_value(window[1].content.strip().rstrip(":"), window[3])
            )
            index += 4
            continue
        out.append(children[index])
        index += 1
    return out


def _block_token(markup: str) -> Token:
    token = Token("html_block", "", 0)
    token.content = markup + "\n"
    token.block = True
    return token


def _structure(tokens: list[Token]) -> list[Token]:
    out: list[Token] = []
    card = False
    collapsed = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == "heading_open":
            if card:
                out.append(_block_token("</section>"))
                card = False
            if (
                token.tag == "h2"
                and not collapsed
                and tokens[index + 1].content.strip() == _PER_LENS_HEADING
            ):
                out.append(
                    _block_token(f"<details>\n<summary>{_PER_LENS_HEADING}</summary>")
                )
                collapsed = True
                index += 3
                continue
            if token.tag == "h3":
                out.append(_block_token('<section class="finding">'))
                card = True
        out.append(token)
        index += 1
    if card:
        out.append(_block_token("</section>"))
    if collapsed:
        out.append(_block_token("</details>"))
    return out


def _transform(tokens: list[Token]) -> list[Token]:
    for token in tokens:
        if token.type == "inline" and token.children:
            token.children = _decorate_labels(token.children)
    return _structure(tokens)


def _title(tokens: list[Token]) -> str:
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.tag == "h1":
            children = tokens[index + 1].children or []
            return "".join(
                child.content
                for child in children
                if child.type in ("text", "code_inline")
            )
    return "Review"


def render(markdown: str) -> str:
    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    env: dict[str, object] = {}
    parsed = md.parse(markdown, env)
    title = escapeHtml(_title(parsed))
    body = cast(str, md.renderer.render(_transform(parsed), md.options, env))
    return _PAGE.format(title=title, style=_STYLE, body=body)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("report", type=click.Path(path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    required=True,
    help="write the page here",
)
@click.option(
    "--open",
    "open_it",
    is_flag=True,
    help="open the rendered page in a browser",
)
def cli(report: Path, output: Path, open_it: bool) -> None:
    """Render REPORT, a review-diff markdown report, to one HTML page."""
    try:
        markdown = report.read_text(encoding="utf-8")
    except OSError as err:
        raise click.ClickException(f"cannot read {report}: {err.strerror}") from err
    output.write_text(render(markdown), encoding="utf-8")
    if open_it:
        open_url(output.resolve().as_uri())


def main(args: Sequence[str] | None = None) -> None:
    cli(args=args, prog_name=PROG_NAME)


if __name__ == "__main__":
    main()
