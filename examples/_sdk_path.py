"""Make ``cowboy_sdk.testing`` importable for the example test suites.

The published ``cowboy-sdk`` (0.1.1) predates ``cowboy_sdk.testing``, and the
CLI scaffold's ``cowboy-sdk>=0.1.2`` is not on PyPI yet, so each example's
``pyproject.toml`` declares no SDK dependency and its ``conftest.py`` imports
this module instead. It loads the SDK package from a node checkout:
``$COWBOY_SDK_PATH`` (a directory containing ``cowboy_sdk/``), or
``../node/pvm/Lib`` beside this repository.

The package is loaded by file location rather than by putting that directory
on ``sys.path``: ``node/pvm/Lib`` is the PVM's whole standard library, and
putting it ahead of CPython's own breaks ``re`` and ``json``.

Once cowboy-sdk 0.1.2 is published, add it to each example's dependencies and
delete this module and the conftest files.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _sdk_dir() -> Path:
    override = os.environ.get("COWBOY_SDK_PATH")
    if override:
        return Path(override).expanduser().resolve() / "cowboy_sdk"
    return Path(__file__).resolve().parents[2] / "node" / "pvm" / "Lib" / "cowboy_sdk"


def _load_from_node_checkout() -> None:
    pkg = _sdk_dir()
    init = pkg / "__init__.py"
    if not (pkg / "testing.py").is_file():
        raise ImportError(
            f"cowboy_sdk with a testing module not found at {pkg}; "
            "set COWBOY_SDK_PATH to a directory containing cowboy_sdk/ "
            "(for example <node checkout>/pvm/Lib)"
        )
    for name in [m for m in sys.modules if m == "cowboy_sdk" or m.startswith("cowboy_sdk.")]:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        "cowboy_sdk", init, submodule_search_locations=[str(pkg)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["cowboy_sdk"] = module
    spec.loader.exec_module(module)


try:
    import cowboy_sdk.testing  # noqa: F401
except ImportError:
    _load_from_node_checkout()
