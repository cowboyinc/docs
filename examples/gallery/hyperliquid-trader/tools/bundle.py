#!/usr/bin/env python3
"""Inline hl_common into an actor file for deploy.

Timer-fired handlers do NOT get CIP-26 lib pins injected (deploy/execute do),
so a lib-importing actor's timers silently no-op. Deploys therefore ship a
bundled, self-contained file; hl_common.py stays the canonical source.
Usage: bundle.py <actor.py> <out.py>
"""

import re
import sys

actor = open(sys.argv[1]).read()
lib = open(__file__.rsplit("/", 2)[0] + "/hl_common.py").read()
# strip the lib's docstring + its own cowboy_sdk import (actor already has one)
lib = re.sub(r'^""".*?"""\n', "", lib, count=1, flags=re.S)
lib = lib.replace("from cowboy_sdk import runtime\n", "")
body = (
    "\n# ==== inlined hl_common (by tools/bundle.py; edit the library, not this) ====\n"
    + lib
    + "\n# ==== end inlined hl_common ====\n"
)
# replace the import with the inlined body, and alias lines with no-ops
assert actor.count("import hl_common\n") == 1, "expected one plain hl_common import"
actor = actor.replace("import hl_common\n", body, 1)
actor = re.sub(r"^(\w+) = hl_common\.(\w+)\n", "", actor, flags=re.M)
assert "hl_common." not in actor.replace("hl_common.py", ""), (
    "unresolved hl_common references remain"
)
open(sys.argv[2], "w").write(actor)
print(sys.argv[2], len(actor.split("\n")), "lines")
