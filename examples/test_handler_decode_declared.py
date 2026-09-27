#!/usr/bin/env python3
"""Every exposed handler in this repo must declare its payload codec.

``@public`` and ``@callable_by`` take a ``decode=`` selector. Omitting it takes a
default that the node resolves at decoration time, and that default is scheduled
to move from ``"raw"`` to ``"cbor"`` at an activation height
(``permissions._DECODE_DEFAULT_ACTIVATION_HEIGHT``, a dormant ``u64::MAX``
sentinel today). A handler that declares nothing therefore changes the type of
its payload — ``bytes`` before, a decoded value after — on a flag day it does not
control. Declaring ``decode=`` explicitly opts out of that switch entirely.

It is AST-based rather than a grep, so an aliased or qualified import still
resolves and a decorator inside a string cannot masquerade as one.

Two node rules are checked as well, because violating either fails at *class
creation* rather than at call time (``actor.py``):

  * a continuation handler cannot declare a non-raw codec — the FSM resume has
    its own payload path
  * ``on_fork`` cannot either — the host invokes it with a bare 20-byte parent
    address, and an address whose first byte is a valid CBOR header decodes to a
    truncated address instead of raising

Run: python3 examples/test_handler_decode_declared.py (or under pytest)
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path


EXAMPLES_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXAMPLES_DIR.parent

PERMISSION_DECORATORS = ("public", "callable_by")

# Each entry must exist and yield at least one file, so a rename cannot quietly
# empty this guard's coverage.
PYTHON_TREES = (EXAMPLES_DIR,)
MARKDOWN_TREES = (REPO_ROOT / "docs", REPO_ROOT / "ai-context")
MARKDOWN_FILES = (REPO_ROOT / "README.md",)

PYTHON_FENCE = re.compile(r"```(?:python|py)\n(.*?)```", re.S)


def _python_sources() -> list[Path]:
    found = []
    for tree in PYTHON_TREES:
        assert tree.is_dir(), f"configured tree is missing: {tree}"
        in_tree = [p for p in sorted(tree.rglob("*.py"))
                   if "__pycache__" not in p.parts]
        assert in_tree, f"configured tree yielded no Python sources: {tree}"
        found += in_tree
    return found


def _markdown_sources() -> list[Path]:
    found = []
    for tree in MARKDOWN_TREES:
        assert tree.is_dir(), f"configured tree is missing: {tree}"
        in_tree = sorted(tree.rglob("*.md")) + sorted(tree.rglob("*.mdx"))
        assert in_tree, f"configured tree yielded no markdown: {tree}"
        found += in_tree
    for path in MARKDOWN_FILES:
        assert path.is_file(), f"configured file is missing: {path}"
        found.append(path)
    return found


def _permission_decorator_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Local names bound to the permission decorators, and module aliases for
    ``cowboy_sdk`` — so an aliased or qualified import is still recognized."""
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("cowboy_sdk"):
            for alias in node.names:
                if alias.name in PERMISSION_DECORATORS:
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "cowboy_sdk":
                    modules.add(alias.asname or alias.name)
    return names, modules


def _decorator_use(dec: ast.expr, names: set[str], modules: set[str]):
    """``(decorator_name, declares_decode)`` if ``dec`` is a permission
    decorator, else ``None``. Handles the bare, called, and qualified forms."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    name = None
    if isinstance(target, ast.Name):
        if target.id in names:
            name = target.id
    elif isinstance(target, ast.Attribute) and target.attr in PERMISSION_DECORATORS:
        base = target.value
        if isinstance(base, ast.Name) and (base.id in modules or base.id == "cowboy_sdk"):
            name = target.attr
    if name is None:
        return None
    declares = isinstance(dec, ast.Call) and any(k.arg == "decode" for k in dec.keywords)
    return name, declares


def _is_continuation(fn: ast.AST) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "continuation":
            return True
    return False


def _declared_codec(dec: ast.expr):
    if not isinstance(dec, ast.Call):
        return None
    for kw in dec.keywords:
        if kw.arg == "decode" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def scan_source(source: str, label: str, line_offset: int = 0):
    """``(undeclared, raw_only_violations)`` for one Python source unit."""
    tree = ast.parse(source)
    names, modules = _permission_decorator_names(tree)
    # A doc snippet often omits the import it would need; assume the plain names.
    names |= set(PERMISSION_DECORATORS)

    undeclared, raw_only = [], []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in fn.decorator_list:
            use = _decorator_use(dec, names, modules)
            if use is None:
                continue
            decorator, declares = use
            line = line_offset + fn.lineno
            if not declares:
                undeclared.append(f"{label}:{line} @{decorator} on {fn.name}() "
                                  f"omits decode=")
                continue
            codec = _declared_codec(dec)
            if codec == "raw" or codec is None:
                continue
            if _is_continuation(fn):
                raw_only.append(f"{label}:{line} continuation {fn.name}() declares "
                                f"decode={codec!r}; only 'raw' is allowed")
            elif fn.name == "on_fork":
                raw_only.append(f"{label}:{line} on_fork declares decode={codec!r}; "
                                f"the host delivers a bare 20-byte address")
    return undeclared, raw_only


def _scan_repo():
    undeclared, raw_only, unparsed = [], [], []
    for path in _python_sources():
        label = str(path.relative_to(REPO_ROOT))
        try:
            found, bad = scan_source(path.read_text(), label)
        except SyntaxError as exc:
            unparsed.append(f"{label}: {exc}")
            continue
        undeclared += found
        raw_only += bad
    for path in _markdown_sources():
        label = str(path.relative_to(REPO_ROOT))
        text = path.read_text()
        for match in PYTHON_FENCE.finditer(text):
            snippet = match.group(1)
            if not any(f"@{d}" in snippet for d in PERMISSION_DECORATORS):
                continue
            offset = text[:match.start(1)].count("\n")
            try:
                found, bad = scan_source(snippet, label, offset)
            except SyntaxError:
                # A methods-only snippet is indented out of its class; retry
                # dedented, because an unparseable snippet is invisible here.
                try:
                    found, bad = scan_source(textwrap.dedent(snippet), label, offset)
                except SyntaxError as exc:
                    unparsed.append(f"{label}:{offset + 1} (snippet): {exc}")
                    continue
            undeclared += found
            raw_only += bad
    return undeclared, raw_only, unparsed


def test_every_exposed_handler_declares_its_codec() -> None:
    undeclared, _, _ = _scan_repo()
    assert undeclared == [], (
        "these handlers take the node's gated decode default, so their payload "
        "type changes at the activation height:\n  " + "\n  ".join(undeclared))


def test_no_handler_declares_a_codec_the_node_refuses() -> None:
    _, raw_only, _ = _scan_repo()
    assert raw_only == [], (
        "these declarations fail at class creation:\n  " + "\n  ".join(raw_only))


def test_every_actor_snippet_in_the_docs_parses() -> None:
    """An unparseable snippet is invisible to the guard above, so it cannot be
    allowed to pass silently."""
    _, _, unparsed = _scan_repo()
    assert unparsed == [], "\n  ".join(unparsed)


def test_guard_detects_an_offender() -> None:
    """The checks above are only worth having if they fail on the real thing."""
    offenders = (
        'from cowboy_sdk import actor, public, callable_by, OWNER\n'
        '@actor\n'
        'class A:\n'
        '    @public\n'                          # bare
        '    def a(self, payload): ...\n'
        '    @public()\n'                        # called, no decode
        '    def b(self, payload): ...\n'
        '    @callable_by(OWNER)\n'              # args, no decode
        '    def c(self, payload): ...\n'
    )
    found, bad = scan_source(offenders, "offender.py")
    assert [f.split(" ", 1)[1] for f in found] == [
        "@public on a() omits decode=",
        "@public on b() omits decode=",
        "@callable_by on c() omits decode=",
    ], found
    assert bad == [], bad

    aliased = (
        'from cowboy_sdk import public as pub\n'
        'import cowboy_sdk as sdk\n'
        'class A:\n'
        '    @pub\n'
        '    def a(self, payload): ...\n'
        '    @sdk.callable_by(OWNER)\n'
        '    def b(self, payload): ...\n'
    )
    found, _ = scan_source(aliased, "aliased.py")
    assert len(found) == 2, found

    declared = (
        'from cowboy_sdk import actor, public, callable_by, OWNER\n'
        'class A:\n'
        '    @public(decode="raw")\n'
        '    def a(self, payload): ...\n'
        '    @public(decode="cbor")\n'
        '    def b(self, payload): ...\n'
        '    @callable_by(OWNER, decode="json")\n'
        '    def c(self, payload): ...\n'
    )
    found, bad = scan_source(declared, "declared.py")
    assert found == [] and bad == [], (found, bad)


def test_guard_detects_a_codec_the_node_refuses() -> None:
    source = (
        'from cowboy_sdk import actor, public, runner\n'
        'class A:\n'
        '    @public(decode="cbor")\n'
        '    @runner.continuation\n'
        '    async def go(self, payload): ...\n'
        '    @public(decode="cbor")\n'
        '    def on_fork(self, payload): ...\n'
    )
    _, bad = scan_source(source, "refused.py")
    assert len(bad) == 2, bad
    assert "continuation go()" in bad[0], bad
    assert "on_fork" in bad[1], bad


def main() -> None:
    tests = [
        test_every_exposed_handler_declares_its_codec,
        test_no_handler_declares_a_codec_the_node_refuses,
        test_every_actor_snippet_in_the_docs_parses,
        test_guard_detects_an_offender,
        test_guard_detects_a_codec_the_node_refuses,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    undeclared, raw_only, unparsed = _scan_repo()
    print(f"\nscanned {len(_python_sources())} python sources and "
          f"{len(_markdown_sources())} markdown files; "
          f"{len(undeclared)} undeclared, {len(raw_only)} refused, "
          f"{len(unparsed)} unparsed")


if __name__ == "__main__":
    main()
