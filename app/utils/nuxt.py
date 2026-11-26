"""Read the `window.__NUXT__` state that Nuxt 2 sites embed in their HTML.

Nuxt does not embed plain JSON. It minifies the state into a function call
that pulls repeated values out into parameters:

    window.__NUXT__=(function(a,b,c){b[0]=a;return {title:a,tags:b,n:c}}("x",Array(1),3));

This module parses that shape - the parameter list, the `name[i]=value`
assignments, the returned object literal and the argument list - and hands
back the plain data. It is a parser for JavaScript *literals* only: nothing is
evaluated, so a page cannot make this run code, and anything outside the
literal grammar (a function call other than Array(n), an operator) raises
NuxtParseError instead of being guessed at.
"""

import re
from typing import Any, Dict, List, Tuple

_PREFIX = "window.__NUXT__="


class NuxtParseError(ValueError):
    pass


class _Ref:
    """A reference to one of the function's parameters, resolved at the end."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_NUMBER = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_JS_ESCAPE = re.compile(
    r"\\(?:u([0-9a-fA-F]{4})|u\{([0-9a-fA-F]+)\}|x([0-9a-fA-F]{2})|\r\n|(.))", re.S
)
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _unescape(raw: str) -> str:
    def repl(m: "re.Match[str]") -> str:
        hex4, braced, hex2, other = m.groups()
        if hex4:
            return chr(int(hex4, 16))
        if braced:
            return chr(int(braced, 16))
        if hex2:
            return chr(int(hex2, 16))
        if other is None or other == "\n":
            return ""  # a backslash before a line break continues the line
        return _SIMPLE_ESCAPES.get(other, other)

    text = _JS_ESCAPE.sub(repl, raw)
    # \uD83D\uDE00 arrives as two halves of a surrogate pair; join them.
    return text.encode("utf-16", "surrogatepass").decode("utf-16")


class _Parser:
    def __init__(self, src: str, pos: int = 0) -> None:
        self.src = src
        self.pos = pos

    # ------------------------------------------------------------- lexing

    def _ws(self) -> None:
        while self.pos < len(self.src) and self.src[self.pos] in " \t\r\n":
            self.pos += 1

    def peek(self) -> str:
        self._ws()
        return self.src[self.pos] if self.pos < len(self.src) else ""

    def expect(self, text: str) -> None:
        self._ws()
        if not self.src.startswith(text, self.pos):
            got = self.src[self.pos : self.pos + 20]
            raise NuxtParseError(f"expected {text!r} at {self.pos}, got {got!r}")
        self.pos += len(text)

    def accept(self, text: str) -> bool:
        self._ws()
        if self.src.startswith(text, self.pos):
            self.pos += len(text)
            return True
        return False

    def ident(self) -> str:
        self._ws()
        m = _IDENT.match(self.src, self.pos)
        if not m:
            raise NuxtParseError(f"expected a name at {self.pos}")
        self.pos = m.end()
        return m.group(0)

    def string(self) -> str:
        quote = self.src[self.pos]
        end = self.pos + 1
        while True:
            end = self.src.find(quote, end)
            if end < 0:
                raise NuxtParseError("unterminated string")
            backslashes = 0
            i = end - 1
            while self.src[i] == "\\":
                backslashes += 1
                i -= 1
            if backslashes % 2 == 0:
                break
            end += 1
        raw = self.src[self.pos + 1 : end]
        self.pos = end + 1
        return _unescape(raw)

    # ------------------------------------------------------------ grammar

    def value(self) -> Any:
        ch = self.peek()
        if ch == "{":
            return self.obj()
        if ch == "[":
            self.pos += 1
            items: List[Any] = []
            while not self.accept("]"):
                items.append(self.value())
                if not self.accept(","):
                    self.expect("]")
                    break
            return items
        if ch in "\"'":
            return self.string()
        m = _NUMBER.match(self.src, self.pos)
        if m:
            self.pos = m.end()
            text = m.group(0)
            return float(text) if any(c in text for c in ".eE") else int(text)
        name = self.ident()
        if name == "true":
            return True
        if name == "false":
            return False
        if name in ("null", "undefined"):
            return None
        if name == "void":
            self.value()
            return None
        if name == "Array" and self.accept("("):
            size = self.value()
            self.expect(")")
            return [None] * int(size)
        return _Ref(name)

    def obj(self) -> Dict[str, Any]:
        self.expect("{")
        out: Dict[str, Any] = {}
        while not self.accept("}"):
            ch = self.peek()
            if ch in "\"'":
                key = self.string()
            else:
                m = _NUMBER.match(self.src, self.pos)
                key = m.group(0) if m else self.ident()
                if m:
                    self.pos = m.end()
            self.expect(":")
            out[key] = self.value()
            if not self.accept(","):
                self.expect("}")
                break
        return out


def _resolve(node: Any, env: Dict[str, Any], depth: int = 0) -> Any:
    if depth > 200:
        raise NuxtParseError("state nests too deeply")
    if isinstance(node, _Ref):
        if node.name not in env:
            raise NuxtParseError(f"unknown name {node.name!r}")
        return env[node.name]
    if isinstance(node, dict):
        return {k: _resolve(v, env, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, env, depth + 1) for v in node]
    return node


def parse_nuxt_state(html: str) -> Any:
    """The page's `window.__NUXT__` state as plain Python data."""
    start = html.find(_PREFIX)
    if start < 0:
        raise NuxtParseError("page has no window.__NUXT__ state")
    p = _Parser(html, start + len(_PREFIX))

    p.expect("(")
    p.expect("function")
    p.expect("(")
    params: List[str] = []
    while not p.accept(")"):
        params.append(p.ident())
        p.accept(",")
    p.expect("{")

    # Statements before the return are element writes: name[0]=value;
    assignments: List[Tuple[str, Any, Any]] = []
    while not p.accept("return"):
        target = p.ident()
        if p.accept("["):
            key = p.value()
            p.expect("]")
        else:
            p.expect(".")
            key = p.ident()
        p.expect("=")
        assignments.append((target, key, p.value()))
        p.expect(";")
    body = p.value()
    p.accept(";")
    p.expect("}")

    p.expect("(")
    args: List[Any] = []
    while not p.accept(")"):
        args.append(p.value())
        p.accept(",")

    # Arguments are literals; a missing one is undefined in JavaScript.
    env = {name: args[i] if i < len(args) else None for i, name in enumerate(params)}
    for target, key, value in assignments:
        container = env.get(target)
        if isinstance(container, list) and isinstance(key, int) and 0 <= key < len(container):
            container[key] = _resolve(value, env)
        elif isinstance(container, dict):
            container[str(key)] = _resolve(value, env)
        else:
            raise NuxtParseError(f"cannot assign into {target!r}")
    return _resolve(body, env)
