"""Lower HlUiHost decorators and mount into a canonical CIP-15 route blob."""

import importlib.util
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SDK = pathlib.Path(
    os.environ.get(
        "COWBOY_SDK_PATH", ROOT.parents[4] / "node" / "pvm" / "Lib" / "pypi_build"
    )
)
sys.path.insert(0, str(SDK))

from cowboy_sdk import codec  # noqa: E402
from cowboy_sdk.routes import VERBS, collect_routes  # noqa: E402


def _sort_routes(entries):
    entries.sort(key=lambda route: VERBS.index(route["verb"]))
    entries.sort(
        key=lambda route: (route["priority"], route["path"]),
        reverse=True,
    )


def load_actor():
    spec = importlib.util.spec_from_file_location("hl_ui_host", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.HlUiHost


def lower(actor_class):
    entries = []
    for kind, spec, attached in collect_routes(actor_class):
        if kind == "method":
            for verb in spec.verbs:
                entries.append(
                    {
                        "verb": verb,
                        "path": spec.path,
                        "target": {"kind": "method", "name": attached},
                        "pays": spec.pays,
                        "priority": spec.priority,
                        "enabled": True,
                    }
                )
        else:
            target = {
                "kind": "volume",
                "volume_name": spec.volume,
                "strip_prefix": spec.strip_prefix,
                "volume_path_prefix": spec.volume_path_prefix,
                "fallback_status": spec.fallback_status,
            }
            if spec.volume_id is not None:
                target["volume_id"] = spec.volume_id
            if spec.fallback is not None:
                target["fallback"] = spec.fallback
            entries.append(
                {
                    "verb": "GET",
                    "path": spec.path,
                    "target": target,
                    "pays": "actor",
                    "priority": spec.priority,
                    "enabled": True,
                }
            )
    _sort_routes(entries)
    return {"version": 1, "routes": entries}


def main():
    table = lower(load_actor())
    blob = codec.encode(table)
    output = ROOT / "routes.cbor"
    output.write_bytes(blob)
    print("wrote %s (%d bytes, %d routes)" % (output, len(blob), len(table["routes"])))
    for route in table["routes"]:
        print(
            "%-5s %-20s -> %s" % (route["verb"], route["path"], route["target"]["kind"])
        )


if __name__ == "__main__":
    main()
