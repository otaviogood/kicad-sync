from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Sequence, Union


@dataclass(frozen=True)
class Atom:
    value: str


@dataclass(frozen=True)
class QString:
    value: str


SExp = Union[Atom, QString, List["SExp"]]


class SExpParseError(ValueError):
    pass


def atom(value: str) -> Atom:
    return Atom(value)


def qstr(value: str) -> QString:
    return QString(value)


def as_text(node: SExp) -> str:
    if isinstance(node, Atom):
        return node.value
    if isinstance(node, QString):
        return node.value
    raise TypeError(f"Expected atom or quoted string, got {type(node)!r}")


def list_head(node: SExp) -> str:
    if not isinstance(node, list) or not node:
        return ""
    first = node[0]
    if isinstance(first, (Atom, QString)):
        return first.value
    return ""


def parse(text: str) -> List[SExp]:
    parser = _Parser(text)
    result = parser.parse_many()
    parser.ensure_eof()
    return result


def parse_one(text: str) -> SExp:
    parsed = parse(text)
    if len(parsed) != 1:
        raise SExpParseError(f"Expected exactly one top-level S-expression, got {len(parsed)}")
    return parsed[0]


def dumps(node: SExp, indent: str = "  ") -> str:
    return _dump(node, level=0, indent=indent)


def dump_many(nodes: Sequence[SExp], indent: str = "  ") -> str:
    return "\n".join(_dump(n, level=0, indent=indent) for n in nodes)


def _dump(node: SExp, level: int, indent: str) -> str:
    if isinstance(node, Atom):
        return node.value
    if isinstance(node, QString):
        escaped = node.value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    if not node:
        return "()"

    inline = _dump_inline(node)
    if inline is not None:
        return inline

    pad = indent * level
    inner_pad = indent * (level + 1)
    parts = []
    for child in node:
        if isinstance(child, list):
            parts.append(f"{inner_pad}{_dump(child, level + 1, indent)}")
        else:
            parts.append(f"{inner_pad}{_dump(child, level + 1, indent)}")
    return "(\n" + "\n".join(parts) + f"\n{pad})"


def _dump_inline(node: List[SExp]) -> str | None:
    # Keep tiny forms compact, which matches many KiCad tokens (e.g. (xy 1 2)).
    if len(node) <= 6 and all(not isinstance(child, list) for child in node):
        return "(" + " ".join(_dump(child, level=0, indent="  ") for child in node) + ")"

    head = list_head(node)
    if head in {"xy", "at", "size", "start", "end", "page", "path"}:
        if all(not isinstance(child, list) for child in node):
            return "(" + " ".join(_dump(child, level=0, indent="  ") for child in node) + ")"
    return None


class _Parser:
    def __init__(self, text: str):
        self._text = text
        self._idx = 0
        self._len = len(text)

    def parse_many(self) -> List[SExp]:
        out: List[SExp] = []
        self._skip_ws()
        while not self._eof():
            out.append(self._parse_expr())
            self._skip_ws()
        return out

    def ensure_eof(self) -> None:
        self._skip_ws()
        if not self._eof():
            raise SExpParseError(f"Unexpected trailing input at offset {self._idx}")

    def _parse_expr(self) -> SExp:
        self._skip_ws()
        if self._eof():
            raise SExpParseError("Unexpected EOF while parsing expression")

        c = self._peek()
        if c == "(":
            return self._parse_list()
        if c == '"':
            return QString(self._parse_string())
        if c == ")":
            raise SExpParseError(f"Unexpected ')' at offset {self._idx}")
        return Atom(self._parse_atom())

    def _parse_list(self) -> List[SExp]:
        self._consume("(")
        out: List[SExp] = []
        self._skip_ws()
        while not self._eof() and self._peek() != ")":
            out.append(self._parse_expr())
            self._skip_ws()
        if self._eof():
            raise SExpParseError("Unterminated list")
        self._consume(")")
        return out

    def _parse_string(self) -> str:
        self._consume('"')
        out: List[str] = []
        while not self._eof():
            c = self._peek()
            self._idx += 1
            if c == '"':
                return "".join(out)
            if c == "\\":
                if self._eof():
                    raise SExpParseError("Unterminated escape sequence")
                esc = self._peek()
                self._idx += 1
                if esc in {'"', "\\", "n", "r", "t"}:
                    out.append(_unescape_char(esc))
                else:
                    out.append(esc)
            else:
                out.append(c)
        raise SExpParseError("Unterminated string")

    def _parse_atom(self) -> str:
        start = self._idx
        while not self._eof():
            c = self._peek()
            if c.isspace() or c in {"(", ")"}:
                break
            self._idx += 1
        if self._idx == start:
            raise SExpParseError(f"Expected atom at offset {self._idx}")
        return self._text[start:self._idx]

    def _skip_ws(self) -> None:
        while not self._eof() and self._peek().isspace():
            self._idx += 1

    def _peek(self) -> str:
        return self._text[self._idx]

    def _consume(self, expected: str) -> None:
        if self._eof() or self._text[self._idx] != expected:
            got = "EOF" if self._eof() else self._text[self._idx]
            raise SExpParseError(f"Expected '{expected}', got '{got}' at offset {self._idx}")
        self._idx += 1

    def _eof(self) -> bool:
        return self._idx >= self._len


def _unescape_char(c: str) -> str:
    if c == "n":
        return "\n"
    if c == "r":
        return "\r"
    if c == "t":
        return "\t"
    return c
