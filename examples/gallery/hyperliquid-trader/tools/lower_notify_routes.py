"""Lower HlNotify HTTP decorators into canonical CIP-15 routes CBOR."""

import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
OUTPUT = ROOT / "notify_routes.cbor"
SDK_PATH = pathlib.Path(
    os.environ.get(
        "COWBOY_SDK_PATH", ROOT.parents[3] / "node" / "pvm" / "Lib" / "pypi_build"
    )
)
if SDK_PATH.exists():
    sys.path.insert(0, str(SDK_PATH))

from cowboy_sdk import codec
from cowboy_sdk.routes import VERBS


def _sort_routes(routes):
    routes.sort(key=lambda route: VERBS.index(route["verb"]))
    routes.sort(
        key=lambda route: (route["priority"], route["path"]),
        reverse=True,
    )


def main():
    routes = [
        {
            "verb": "POST",
            "path": "/telegram/webhook",
            "target": {"kind": "method", "name": "handle_webhook"},
            "pays": "actor",
            "priority": 0,
            "enabled": True,
        },
        {
            "verb": "GET",
            "path": "/telegram/health",
            "target": {"kind": "method", "name": "webhook_health"},
            "pays": "actor",
            "priority": 0,
            "enabled": True,
        },
    ]
    _sort_routes(routes)
    OUTPUT.write_bytes(codec.encode({"version": 1, "routes": routes}))
    print("wrote %s (%d routes)" % (OUTPUT, len(routes)))


if __name__ == "__main__":
    main()
