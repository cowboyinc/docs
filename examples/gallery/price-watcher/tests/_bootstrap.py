"""Test bootstrap: load the repo cowboy_sdk + mock pvm_host + the actor.

Order matters and paths are deliberate:
- mock pvm_host must be in sys.modules BEFORE cowboy_sdk imports it;
- cowboy_sdk is loaded AS A PACKAGE BY PATH (spec_from_file_location with
  submodule_search_locations) rather than via sys.path, because
  (a) node/pvm/Lib ships a RustPython stdlib copy that must not shadow
      CPython's (threading breaks), and
  (b) an installed pypi cowboy_sdk in a venv must not win over the repo one.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.normpath(os.path.join(HERE, "..", "actors", "watcher", "main.py"))
# node is a sibling of this repo in the standard dev layout — the same place
# the example's own demo.sh resolves it ($REPO_ROOT/../node) — unless
# COWBOY_SDK_PATH points elsewhere (the same override _sdk_path honors).
LIB = os.environ.get("COWBOY_SDK_PATH") or os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "..", "..", "node", "pvm", "Lib"))
if not os.path.isfile(os.path.join(LIB, "cowboy_sdk", "tests", "mock_pvm_host.py")):
    raise SystemExit(
        "skip: these suites need the sibling `node` checkout for the SDK mock "
        "host (expected at %s). Clone cowboyinc/node alongside this repo." % LIB)

_spec = importlib.util.spec_from_file_location(
    "mock_pvm_host", os.path.join(LIB, "cowboy_sdk", "tests", "mock_pvm_host.py"))
mock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mock)
sys.modules["pvm_host"] = mock
mock.reset()

_pkg = os.path.join(LIB, "cowboy_sdk")
_spec = importlib.util.spec_from_file_location(
    "cowboy_sdk", os.path.join(_pkg, "__init__.py"),
    submodule_search_locations=[_pkg])
_sdk = importlib.util.module_from_spec(_spec)
sys.modules["cowboy_sdk"] = _sdk
_spec.loader.exec_module(_sdk)


def load_actor():
    """Fresh import of actors/watcher/main.py (runs the real FSM compiler)."""
    s = importlib.util.spec_from_file_location("price_watcher", EXAMPLE)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m
