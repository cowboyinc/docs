#!/usr/bin/env python3
"""Generate the Cowboy SDK reference (Mintlify MDX) from cowboy_sdk source.

Parses the cowboy_sdk package with `ast` — it does NOT import it, so it works in
CI / a docs-only checkout without the PVM's native modules on PYTHONPATH (the
import gotcha noted in COW-411). Signatures and docstrings come straight from the
source, so the page tracks the code instead of drifting.

Usage:
    python3 docs/scripts/gen_sdk_reference.py \
        --sdk-dir ../node/pvm/Lib/cowboy_sdk \
        --out docs/developers/sdk-reference.mdx
"""
from __future__ import annotations
import argparse, ast, os, sys
from pathlib import Path

# Modules surfaced in the reference, in presentation order. Each is a cowboy_sdk
# submodule whose public top-level functions/classes are documented.
MODULES = [
    ("runtime", "Runtime — host calls (state, time, randomness, timers, tokens)"),
    ("call", "Cross-actor calls"),
    ("send", "Async messages"),
    ("timer", "Timers"),
    ("runner", "Off-chain runner jobs (LLM / HTTP / MCP)"),
    ("events", "Event hooks (emit / on_event)"),
    ("stream", "Streams"),
    ("watchtower", "Watchtower"),
    ("codec", "Deterministic codec (CBOR)"),
    ("payload_codec", "Payload codecs — the handler/caller selector"),
    ("json_codec", "Strict JSON codec"),
    ("errors", "Errors"),
]

def sig(fn: ast.FunctionDef) -> str:
    a = fn.args
    parts = []
    posonly = getattr(a, "posonlyargs", [])
    args = posonly + a.args
    defaults = a.defaults
    ndef = len(defaults)
    nnodef = len(args) - ndef
    for i, arg in enumerate(args):
        s = arg.arg
        if arg.annotation is not None:
            s += ": " + ast.unparse(arg.annotation)
        if i >= nnodef:
            s += " = " + ast.unparse(defaults[i - nnodef])
        parts.append(s)
        if i == len(posonly) - 1:
            parts.append("/")
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        parts.append("*")
    for i, arg in enumerate(a.kwonlyargs):
        s = "" + arg.arg
        if arg.annotation is not None:
            s += ": " + ast.unparse(arg.annotation)
        if a.kw_defaults[i] is not None:
            s += " = " + ast.unparse(a.kw_defaults[i])
        parts.append(s)
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    ret = " -> " + ast.unparse(fn.returns) if fn.returns else ""
    return f"{fn.name}({', '.join(parts)}){ret}"

def public_defs(mod_path: Path):
    """Yield (kind, name, signature, docstring) for public top-level defs."""
    tree = ast.parse(mod_path.read_text())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            yield ("function", node.name, sig(node), ast.get_docstring(node))
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            yield ("class", node.name, node.name, ast.get_docstring(node))

def render(sdk_dir: Path) -> str:
    rendered = []
    for mod, title in MODULES:
        p = sdk_dir / f"{mod}.py"
        if not p.exists():
            continue
        defs = list(public_defs(p))
        if defs:
            rendered.append((mod, title, defs))

    # Emitted only when the sections it names are, so an SDK predating the codec
    # modules gets no dangling reference.
    codec_pointer = [
        "Where `call()` and `send()` below refer to `payload_codec.CODECS`, the members of",
        "that set and the behavior of each are documented under **Payload codecs** and",
        "**Strict JSON codec**; [CIP-6 §9.5](/cips/cip-6-sdk) is the normative version.",
        "",
    ] if {"payload_codec", "json_codec"} <= {mod for mod, _, _ in rendered} else []

    out = [
        "---",
        'title: "SDK Reference"',
        'description: "Auto-generated reference for the cowboy_sdk public API — signatures and docstrings extracted from source. Do not edit by hand; regenerate with docs/scripts/gen_sdk_reference.py."',
        "---",
        "",
        "{/* GENERATED FILE — do not edit by hand. */}",
        "{/* Regenerate: python3 docs/scripts/gen_sdk_reference.py (see Developer Guide). */}",
        "",
        "This page is generated from the `cowboy_sdk` source docstrings, so it tracks the",
        "shipped SDK. For tutorials see [Your First Actor](/developers/your-first-actor)",
        "and the [Runtime API](/developers/runtime-api); for the determinism rules the",
        "PVM enforces see the [PVM reference](/architecture/actor-vm/pvm-reference).",
        "",
        *codec_pointer,
    ]
    for mod, title, defs in rendered:
        out.append(f"## {title}")
        out.append("")
        out.append(f"_Module: `cowboy_sdk.{mod}`_")
        out.append("")
        for kind, name, signature, doc in defs:
            out.append(f"### `{name}`")
            out.append("")
            out.append("```python")
            out.append(("class " if kind == "class" else "") + signature)
            out.append("```")
            out.append("")
            if doc:
                out.append(doc.strip())
                out.append("")
    return "\n".join(out) + "\n"

def main():
    ap = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent
    ap.add_argument("--sdk-dir", default=str((here / "../../../node/pvm/Lib/cowboy_sdk").resolve()))
    ap.add_argument("--out", default=str((here / "../developers/sdk-reference.mdx").resolve()))
    args = ap.parse_args()
    sdk_dir = Path(args.sdk_dir)
    if not sdk_dir.exists():
        sys.exit(f"cowboy_sdk source not found at {sdk_dir} — pass --sdk-dir (expects a sibling node/ checkout)")
    Path(args.out).write_text(render(sdk_dir))
    print(f"wrote {args.out} from {sdk_dir}")

if __name__ == "__main__":
    main()
