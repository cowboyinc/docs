#!/usr/bin/env python3
"""Structural checks for the Cowboy examples curriculum layout."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


EXAMPLES_DIR = Path(__file__).resolve().parent


CORE_EXAMPLES = [
    "00-quickstart-hello",
    "01-state-events-queries",
    "02-senders-and-permissions",
    "03-two-actor-messaging",
    "04-multi-actor-workflow",
    "05-tokens-and-balances",
    "06-token-escrow",
    "07-timers-and-automation",
    "08-minimal-runner-continuation",
    "09-runner-llm",
    "10-async-cache-refresh",
    "11-failure-recovery",
    "12-capstone-agent",
]


GALLERY_EXAMPLES = [
    "advanced-messaging-ring",
    "rebalancing-agent",
    "liquidity-pools",
    "copy-trading",
    "spam-detection",
    "content-recommendations",
    "sentiment-analysis",
    "audio-transcription",
    "image-generation",
    "content-moderator",
    "alerts-and-schedulers",
    "liquidation-oracle",
    "dao-copilot",
    "compliance",
    "fraud-watcher",
    "contract-summarizer",
    "hn-feed",
    "watchtower",
    "casino-rounds",
    "blackjack",
    "cowbot",
    "erlangcowboydemo",
    "hyperliquid-trader",  # registered README-bearing gallery system
    "price-watcher",
]


RUNNABLE_ENTRYPOINT_EXCEPTIONS = {
    "gallery/advanced-messaging-ring": "deploy_ring.sh",
    "gallery/erlangcowboydemo": "Makefile",
    # An off-chain function project (CIP-10 signing bundle), not an actor:
    # nothing to deploy, the interface module is the entrypoint.
    "gallery/venue-signer": "offchain/main.py",
    "gallery/hello-world": "deploy.sh",
    "gallery/ranchhand": "deploy.sh",
    "gallery/twilio": "deploy.sh",
}


NON_DEFAULT_SWEEP_EXAMPLES = {
    "gallery/alphavantage",
    "gallery/postmark-email",
    "gallery/anchor-browser",
    "gallery/jina-reader",
    "gallery/exa-search",
    "gallery/agentmail-inbox",
    "gallery/hyperbrowser-scrape",
    "gallery/tavily-search",
    "gallery/parallel-search",
    "gallery/browserbase-browse",
    "gallery/deepgram",
    "gallery/browse-kernel",
    "gallery/e2b-code",
    "gallery/firecrawl-scrape",
    "gallery/steel-browser",
    "gallery/resend-email",
    "gallery/reducto-parse",
    "gallery/modal-compute",
    "gallery/llamaparse",
    "gallery/cowbot",
    "gallery/watchtower",
    # Custom multi-actor suite; hyperliquid-trader-tests.yml is its PR gate.
    "gallery/hyperliquid-trader",
    "gallery/price-watcher",
}


REMOVED_EXAMPLES = [
    "29-watchtower",
    "32-cowbot",
    "33-watchtower",
    "gallery/pub-sub-stream",
]


# Standalone top-level demos retained from the pre-`core`/`gallery` layout.
# These are real, README-bearing examples that have not yet been migrated under
# a track root. Allow them explicitly so the structural lint reflects the actual
# committed tree; relocating or retiring them is tracked by the examples-harness
# epics (COW-1344 / COW-1347).
STANDALONE_TOP_LEVEL_EXAMPLES = {
    "31-cbss-secrets",
    "31-frontier",
    "34-cbfs-volume",
    "34-simple-telegram",
    "35-cbfs-private-volume",
    "36-cbfs-grant-delegation",
    "37-private-workspace-production-path",
}


def test_curriculum_and_gallery_directories_exist() -> None:
    for name in CORE_EXAMPLES:
        assert (EXAMPLES_DIR / "core" / name / "README.md").is_file(), name
    for name in GALLERY_EXAMPLES:
        assert (EXAMPLES_DIR / "gallery" / name / "README.md").is_file(), name
    # tooling/local-simulation retired: SimulatedChain tests + `cowboy dev` replace it.
    assert not (EXAMPLES_DIR / "tooling").exists()


def test_runnable_examples_have_registered_entrypoints() -> None:
    missing = []
    for track in ("core", "gallery"):
        for readme in sorted((EXAMPLES_DIR / track).glob("*/README.md")):
            example_dir = readme.parent
            key = f"{track}/{example_dir.name}"
            if (example_dir / "demo.sh").is_file():
                continue
            alternate = RUNNABLE_ENTRYPOINT_EXCEPTIONS.get(key)
            if alternate and (example_dir / alternate).exists():
                continue
            missing.append(key)
    assert missing == []


def test_registered_entrypoint_exceptions_exist_and_are_documented() -> None:
    """Each exception names a real entrypoint that its README points at.

    The sweep above only consults an exception when the example has no
    demo.sh, so a stale entry (a renamed script, an exception left behind
    after a demo.sh was added) would otherwise go unnoticed.
    """
    offenders = []
    for key, entrypoint in sorted(RUNNABLE_ENTRYPOINT_EXCEPTIONS.items()):
        track, name = key.split("/", 1)
        example_dir = EXAMPLES_DIR / track / name
        if not (example_dir / entrypoint).exists():
            offenders.append(f"{key}: missing {entrypoint}")
            continue
        if entrypoint not in (example_dir / "README.md").read_text():
            offenders.append(f"{key}: README missing {entrypoint}")
    assert offenders == []


def test_examples_do_not_have_top_level_or_removed_showcase_dirs() -> None:
    unexpected_top_level = []
    allowed_top_level = {"core", "gallery", "_shared"} | STANDALONE_TOP_LEVEL_EXAMPLES
    for path in sorted(EXAMPLES_DIR.iterdir()):
        if not path.is_dir() or path.name in allowed_top_level:
            continue
        unexpected_top_level.append(path.name)

    removed_present = [
        name for name in REMOVED_EXAMPLES if (EXAMPLES_DIR / name).exists()
    ]
    assert unexpected_top_level == []
    assert removed_present == []


def test_parallel_harness_discovers_nested_examples_and_no_stale_defaults() -> None:
    parallel = (EXAMPLES_DIR / "test_examples_parallel.sh").read_text()
    sequential = (EXAMPLES_DIR / "test_examples_sequential.sh").read_text()

    assert "-mindepth 3 -maxdepth 3" in parallel
    assert "core/00-quickstart-hello" in parallel
    assert "gallery/copy-trading" in parallel

    stale_selectors = [
        "01-tokens",
        "20-minimal-runner-continuation",
        "21-pure-timer-scheduler",
        "27-father-jokes",
    ]
    offenders = []
    for selector in stale_selectors:
        if selector in parallel:
            offenders.append(f"parallel: {selector}")
        if selector in sequential:
            offenders.append(f"sequential: {selector}")
    assert offenders == []


def _smoke_set_keys() -> list[str]:
    workflow = (EXAMPLES_DIR.parent / ".github" / "workflows" / "examples-smoke.yml")
    lines = workflow.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("SMOKE_SET:"))
    indent = len(lines[start]) - len(lines[start].lstrip())
    keys = []
    for line in lines[start + 1:]:
        if not line.strip():
            break
        if len(line) - len(line.lstrip()) <= indent:
            break
        keys.append(line.strip())
    return keys


def test_smoke_workflow_selectors_name_examples_that_exist() -> None:
    """A selector that matches no example aborts the sweep before it runs any."""
    keys = _smoke_set_keys()
    assert keys, "no SMOKE_SET keys found in examples-smoke.yml"
    missing = [key for key in keys if not (EXAMPLES_DIR / key / "demo.sh").is_file()]
    assert missing == []


def test_active_runnable_demos_are_registered_in_sweeps() -> None:
    runnable = []
    for track in ("core", "gallery"):
        for demo in sorted((EXAMPLES_DIR / track).glob("*/demo.sh")):
            key = f"{track}/{demo.parent.name}"
            if key not in NON_DEFAULT_SWEEP_EXAMPLES:
                runnable.append(key)

    local = (EXAMPLES_DIR / "test_examples_local.sh").read_text()
    parallel = (EXAMPLES_DIR / "test_examples_parallel.sh").read_text()
    sequential = (EXAMPLES_DIR / "test_examples_sequential.sh").read_text()

    missing = []
    for key in runnable:
        if f'run_example "{key}"' not in local:
            missing.append(f"local: {key}")
        if key not in parallel:
            missing.append(f"parallel: {key}")
        if key not in sequential:
            missing.append(f"sequential: {key}")
    assert missing == []


def test_non_default_gallery_demos_are_documented_and_not_in_active_sweeps() -> None:
    local = (EXAMPLES_DIR / "test_examples_local.sh").read_text()
    parallel = (EXAMPLES_DIR / "test_examples_parallel.sh").read_text()
    sequential = (EXAMPLES_DIR / "test_examples_sequential.sh").read_text()

    offenders = []
    for key in NON_DEFAULT_SWEEP_EXAMPLES:
        readme = EXAMPLES_DIR / key / "README.md"
        text = " ".join(readme.read_text().split())
        if "outside the default gallery sweep" not in text:
            offenders.append(f"{key}: missing non-default sweep note")
        if f'run_example "{key}"' in local:
            offenders.append(f"local: {key}")
        if f"  {key}" in parallel:
            offenders.append(f"parallel default: {key}")
        if f"  {key}" in sequential:
            offenders.append(f"sequential: {key}")
    assert offenders == []


def test_runner_help_exposes_gallery_and_all_flags() -> None:
    result = subprocess.run(
        ["bash", str(EXAMPLES_DIR / "run_examples.sh"), "--help"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    assert "--gallery" in result.stdout
    assert "--all" in result.stdout
    assert "core/08-minimal-runner-continuation" in result.stdout
    assert "09-runner-llm" in result.stdout
    assert "12-capstone-agent" in result.stdout


def test_spawn_local_all_accepts_empty_selector_list() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spawn_script = Path(tmp) / "spawn.sh"
        spawn_script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf 'mode=%s args=%s\\n' \"$COWBOY_EXAMPLES_MODE\" \"$#\"\n"
        )
        spawn_script.chmod(0o755)
        env = os.environ.copy()
        env["RUN_EXAMPLES_SPAWN_SCRIPT"] = str(spawn_script)
        result = subprocess.run(
            ["bash", str(EXAMPLES_DIR / "run_examples.sh"), "--spawn-local", "--all"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    assert result.returncode == 0, result.stderr
    assert "mode=all args=0" in result.stdout


def test_default_runner_accepts_empty_selector_list() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sweep_script = Path(tmp) / "sweep.sh"
        sweep_script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf 'mode=%s args=%s rpc=%s\\n' \"$COWBOY_EXAMPLES_MODE\" \"$#\" \"$RPC_URL\"\n"
        )
        sweep_script.chmod(0o755)
        env = os.environ.copy()
        env["RUN_EXAMPLES_SWEEP_SCRIPT"] = str(sweep_script)
        env["RUN_EXAMPLES_PREPARE_KEY"] = "0"
        result = subprocess.run(
            ["bash", str(EXAMPLES_DIR / "run_examples.sh")],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    assert result.returncode == 0, result.stderr
    assert "mode=core args=0" in result.stdout


def test_local_sweep_rejects_unknown_selectors_before_validator_preflight() -> None:
    env = os.environ.copy()
    env["COWBOY_EXAMPLES_MODE"] = "core"
    result = subprocess.run(
        ["bash", str(EXAMPLES_DIR / "test_examples_local.sh"), "not-a-real-example"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "No examples matched" in output
    assert "Expected a Cowboy validator" not in output


# Examples are `cowboy init` projects: targets are discovered from
# actors/<name>/main.py, tests drive them through SimulatedChain, and demo.sh
# deploys by target name. Raw `--code` deploys skip the project compiler and
# the node's runtime-profile check, so nothing under a project may use them.
CLI_PROJECT_TRACKS = ("core", "gallery")

# Not standard `cowboy init` Python actor projects: an Erlang comparison demo,
# the inference actor whose layout is owned by its own in-flight branch, the
# Hyperliquid desk's multi-actor deployment bundle (tested by its dedicated
# pull-request workflow), and an off-chain-only function project whose schema
# support is staged separately.
CLI_PROJECT_EXCEPTIONS = {
    "gallery/erlangcowboydemo",
    "gallery/hyperliquid-trader",
    "gallery/inference",
    "gallery/tests",
    "gallery/venue-signer",
}


# Schema v2 `[offchain]` projects (node `cli/crates/actor-build/src/config.rs`
# on the off-chain build branches, node#1599/#1610). The pinned CLI on main
# parses schema v1 only and rejects these manifests, so `check_projects.sh`
# and `test_examples_are_cli_projects` cannot see a typo in them; this mirrors
# the node's `deny_unknown_fields` raw config until that CLI ships.
OFFCHAIN_PROJECT_SCHEMA_VERSION = 2
OFFCHAIN_TOP_LEVEL_KEYS = {"schema_version", "actors", "offchain"}
OFFCHAIN_TABLE_KEYS = {"context", "python", "resource_tier", "min_isolation_tier", "volumes"}
OFFCHAIN_PYTHON_VERSIONS = {"3.12"}
OFFCHAIN_RESOURCE_TIERS = {"small", "medium"}
OFFCHAIN_ISOLATION_TIERS = {"standard", "strong"}


def _is_offchain_manifest(manifest: Path) -> bool:
    import tomllib

    return manifest.is_file() and "offchain" in tomllib.loads(manifest.read_text())


def _offchain_manifests(examples_dir: Path = EXAMPLES_DIR) -> list[Path]:
    manifests = []
    for track in CLI_PROJECT_TRACKS:
        for manifest in sorted((examples_dir / track).glob("*/cowboy.toml")):
            if _is_offchain_manifest(manifest):
                manifests.append(manifest)
    return manifests


def test_staged_offchain_manifests_match_the_node_offchain_schema() -> None:
    import tomllib

    manifests = _offchain_manifests()
    # Discovery must actually find the staged example; the check is not vacuous.
    assert EXAMPLES_DIR / "gallery" / "venue-signer" / "cowboy.toml" in manifests
    offenders = []
    for manifest in manifests:
        key = manifest.parent.relative_to(EXAMPLES_DIR)
        config = tomllib.loads(manifest.read_text())
        if config.get("schema_version") != OFFCHAIN_PROJECT_SCHEMA_VERSION:
            offenders.append(f"{key}: [offchain] needs schema_version = 2")
        for unknown in sorted(set(config) - OFFCHAIN_TOP_LEVEL_KEYS):
            offenders.append(f"{key}: unknown top-level key {unknown}")
        offchain = config["offchain"]
        for unknown in sorted(set(offchain) - OFFCHAIN_TABLE_KEYS):
            offenders.append(f"{key}: unknown [offchain] key {unknown}")
        context = offchain.get("context", "offchain")
        if not (manifest.parent / context / "main.py").is_file():
            offenders.append(f"{key}: [offchain] context {context!r} has no main.py")
        if offchain.get("python", "3.12") not in OFFCHAIN_PYTHON_VERSIONS:
            offenders.append(f"{key}: unsupported [offchain] python {offchain['python']!r}")
        if "resource_tier" in offchain and offchain["resource_tier"] not in OFFCHAIN_RESOURCE_TIERS:
            offenders.append(f"{key}: unknown resource_tier {offchain['resource_tier']!r}")
        if (
            "min_isolation_tier" in offchain
            and offchain["min_isolation_tier"] not in OFFCHAIN_ISOLATION_TIERS
        ):
            offenders.append(f"{key}: unknown min_isolation_tier {offchain['min_isolation_tier']!r}")
        for volume in offchain.get("volumes", []):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}(:(ro|rw|wo))?", str(volume)):
                offenders.append(f"{key}: malformed volume {volume!r}")
        if not (manifest.parent / "uv.lock").is_file():
            offenders.append(f"{key}: off-chain image builds from a committed uv.lock")
    assert offenders == []


def _cli_project_dirs() -> list[Path]:
    dirs = []
    for track in CLI_PROJECT_TRACKS:
        for p in sorted((EXAMPLES_DIR / track).iterdir()):
            if p.is_dir() and f"{track}/{p.name}" not in CLI_PROJECT_EXCEPTIONS:
                dirs.append(p)
    return dirs


def test_examples_are_cli_projects() -> None:
    offenders = []
    for example_dir in _cli_project_dirs():
        key = example_dir.relative_to(EXAMPLES_DIR)
        manifest = example_dir / "cowboy.toml"
        if not manifest.is_file() or "schema_version" not in manifest.read_text():
            offenders.append(f"{key}: missing cowboy.toml with schema_version")
        if not (example_dir / "pyproject.toml").is_file():
            offenders.append(f"{key}: missing pyproject.toml")
        if not (example_dir / "conftest.py").is_file():
            offenders.append(f"{key}: missing conftest.py")
        if not (example_dir / "actors" / "__init__.py").is_file():
            offenders.append(f"{key}: missing actors/__init__.py")
        targets = sorted((example_dir / "actors").glob("*/main.py"))
        if not targets:
            offenders.append(f"{key}: no actors/<target>/main.py")
        for target in targets:
            if not (target.parent / "__init__.py").is_file():
                offenders.append(f"{key}: {target.parent.name} is not a package")
        if not list((example_dir / "tests").glob("test_*.py")):
            offenders.append(f"{key}: no tests/test_*.py")
        stray = sorted(p.name for p in example_dir.glob("*.py") if p.name != "conftest.py")
        if stray:
            offenders.append(f"{key}: actor sources outside actors/: {stray}")
        readme = (example_dir / "README.md").read_text()
        if "uv run pytest" not in readme or "cowboy actor check" not in readme:
            offenders.append(f"{key}: README does not show how to simulate locally")
        demo = example_dir / "demo.sh"
        # `--code` only; `--code-visibility` is a trading-post flag, not a deploy.
        if demo.is_file() and re.search(r"--code(?![-\w])", demo.read_text()):
            offenders.append(f"{key}: demo.sh deploys raw --code instead of a target")
    assert offenders == []


def test_demo_lib_deploys_targets_not_raw_code() -> None:
    lib = (EXAMPLES_DIR / "_demo_lib.sh").read_text()
    assert "--code" not in lib
    assert "actor deploy \"$target\"" in lib


def generated_artifacts(repo: Path) -> list[Path]:
    tracked = subprocess.check_output(["git", "-C", str(repo), "ls-files", "-z", "--", "examples"])
    bad_paths = []
    for filename in tracked.decode().split("\0"):
        if not filename:
            continue
        path = Path(filename)
        forbidden_name = path.name in {
            ".last_actor", "deploy-state.env", "erl_crash.dump", "uv.lock",
        }
        # An off-chain image is installed from the project's committed
        # lockfile (schema v2 `[offchain]`; the manifest shape is checked by
        # test_staged_offchain_manifests_match_the_node_offchain_schema).
        if path.name == "uv.lock" and _is_offchain_manifest(repo / path.parent / "cowboy.toml"):
            forbidden_name = False
        if (
            "_build" in path.parts
            or ".cowboy" in path.parts
            or ".venv" in path.parts
            or ".pytest_cache" in path.parts
            or path.suffix in {".pyc", ".beam"}
            or forbidden_name
        ):
            bad_paths.append(path)
    return bad_paths


def test_no_generated_artifacts_are_committed_under_examples() -> None:
    assert generated_artifacts(EXAMPLES_DIR.parent) == []


def test_tracked_cache_bytecode_is_rejected_but_local_cache_is_ignored() -> None:
    with tempfile.TemporaryDirectory(prefix=".structure-test-", dir=EXAMPLES_DIR.parent) as directory:
        repo = Path(directory)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        cache = repo / "examples" / "__pycache__"
        cache.mkdir(parents=True)
        planted = cache / "planted.pyc"
        planted.write_bytes(b"deliberate lint regression")
        (cache / "local.pyc").write_bytes(b"untracked local cache")
        subprocess.run(["git", "-C", str(repo), "add", "-f", "--", str(planted)], check=True)
        assert generated_artifacts(repo) == [Path("examples/__pycache__/planted.pyc")]


def test_top_level_readme_points_to_runner_path_and_gallery() -> None:
    text = (EXAMPLES_DIR / "README.md").read_text()
    assert "Fast Path" in text
    assert "Full Builder Path" in text
    assert "08-minimal-runner-continuation" in text
    assert "09-runner-llm" in text
    assert "12-capstone-agent" in text
    assert "tooling/local-simulation" not in text
    assert "Gallery" in text
    assert "Advanced And Historical Showcases" not in text
    assert "33-watchtower" not in text
    assert "32-cowbot" not in text
    assert "29-watchtower" not in text
    assert "pub-sub-stream" not in text
    assert "submit_task(" not in text
    assert "get_price(" not in text
    assert "set_timeout(" not in text


def test_gallery_readmes_do_not_use_stale_rpc_or_todo_copy() -> None:
    offenders = []
    for path in sorted((EXAMPLES_DIR / "gallery").glob("*/README.md")):
        text = path.read_text()
        if "http://localhost:3000" in text:
            offenders.append(f"{path.relative_to(EXAMPLES_DIR)}: localhost:3000")
        if "TODO" in text:
            offenders.append(f"{path.relative_to(EXAMPLES_DIR)}: TODO")
        if "/developers/permissions-reference" in text:
            offenders.append(f"{path.relative_to(EXAMPLES_DIR)}: broken permissions-reference link")
    assert offenders == []


def test_gallery_readmes_point_to_runnable_entrypoint() -> None:
    offenders = []
    for path in sorted((EXAMPLES_DIR / "gallery").glob("*/README.md")):
        key = f"gallery/{path.parent.name}"
        text = path.read_text()
        if key in RUNNABLE_ENTRYPOINT_EXCEPTIONS:
            expected = RUNNABLE_ENTRYPOINT_EXCEPTIONS[key]
        else:
            expected = "./demo.sh"
        if expected not in text and f"bash {expected.removeprefix('./')}" not in text:
            offenders.append(f"{path.relative_to(EXAMPLES_DIR)}: missing {expected}")
        if "CowboyClient(" in text:
            offenders.append(f"{path.relative_to(EXAMPLES_DIR)}: stale SDK quickstart")
    assert offenders == []


def test_deterministic_gallery_readmes_explain_demo_scope() -> None:
    quick_start_markers = [
        (
            "The demo deploys the actor, submits a deterministic sample request, "
            "and reads back the stored result."
        ),
        "The runnable walkthrough deploys",
    ]
    offenders = []
    for path in sorted((EXAMPLES_DIR / "gallery").glob("*/README.md")):
        text = path.read_text()
        if any(marker in text for marker in quick_start_markers) and "## Demo Scope" not in text:
            offenders.append(str(path.relative_to(EXAMPLES_DIR)))
    assert offenders == []


def test_cowbot_files_do_not_reference_removed_tunnel_directory() -> None:
    offenders = []
    for path in (EXAMPLES_DIR / "gallery" / "cowbot").rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        for pattern in ["tunnel/README.md", "cd tunnel", "examples/32-cowbot"]:
            if pattern in text:
                offenders.append(f"{path.relative_to(EXAMPLES_DIR)}: {pattern}")
    assert offenders == []


# COW-3878 (D36) guards. The two SINGLE-actor bridges (telegram, cowbot) were
# converted so the route handler IS the @runner.continuation — no self-send. The
# old shape acked the route, self-sent to a @deferred mailbox handler, then
# awaited a runner job there; that runner job runs as a deferred grandchild whose
# derived access budget (162 units) cannot fund the nested submit_job child, so
# the node refuses it (E1005) and the bot never answers.
#
# Detection covers the self-ADDRESS send forms the corpus actually uses — the
# target of an sdk_send(...) being self.address, _self_bytes(), a _self_*()
# helper, or runtime.get_actor_address(). Written as a regex (escaped parens) so
# this guard file does not itself carry the contiguous literal it hunts —
# keeping the `git grep 'sdk_send(self.<addr>' -- examples` evidence empty.
_SELF_SEND_RE = re.compile(
    r"sdk_send\(\s*(?:self\.address\b|_self_\w*\(|runtime\.get_actor_address\()"
)


def _self_send_pos(text: str) -> int:
    """Earliest index of a self-address send in `text`, or -1."""
    m = _SELF_SEND_RE.search(text)
    return m.start() if m else -1


# Multi-actor credential-proxy designs whose self-send was LOAD-BEARING and
# could NOT be converted the telegram/cowbot way. A runner job's submitter is the
# enclosing top-level ExecuteActor's actor (it governs secrets=/verify=/hire= and
# the resume-callback target), so making the call()-reached ingress the
# @runner.continuation would attribute the job to the WRONG actor; the self-send
# re-entered the actor as its own top-level frame (right attribution) but capped
# at 162 accesses (D36). COW-3894 converted gallery/twilio (twilio.py +
# proxy/twiliorelay.py) to a FUNDED self-timer — schedule_timer_ex(...,
# access_limit=322) fires on self (same attribution) with a full deferred-child
# reserve — so neither file self-sends any more and this set is now empty. The
# self-cleaning assertion below enforces it: a file left here that no longer
# matches _SELF_SEND_RE fails, forcing a converted actor to be dropped.
KNOWN_MULTI_ACTOR_SELF_SENDS = set()
# The OTHER multi-actor design — ranchhand/slackrelay — is a CROSS-actor relay
# (ranchhand -> slackrelay proxy and back), NOT a self-send: neither file
# self-addresses a send (verified: 0 self-address sends; ranchhand.py sends to
# tenant["proxy"], slackrelay.py sends to the ranchhand address), so from= is
# already the correct actor at the proxy. The self-send guard below therefore
# does not — and must not — flag them, so they are out of scope for a self-send
# exception set. (Their conversion is also design-tracked by COW-3894.)

# The two route handlers this PR converted; the second guard is scoped to these
# two SINGLE-actor bridges, NOT repo-wide.
CONTINUATION_ROUTE_HANDLERS = [
    ("34-simple-telegram/actors/telegram/main.py", "handle_webhook"),
    ("gallery/cowbot/actors/cowbot/main.py", "slack_http"),
]


def test_no_example_self_sends_before_a_runner_await() -> None:
    """Repo-wide over examples/*.py (test harnesses excluded): no example may
    pair a self-ADDRESS send with a later runner await — the D36 self-send-then-
    continuation defect (COW-3878). The multi-actor credential-proxy designs in
    KNOWN_MULTI_ACTOR_SELF_SENDS are exempt (COW-3894): there the self-send is
    load-bearing for runner-job attribution and cannot be converted this way."""
    # Self-cleaning: an exempted file that no longer self-sends must FAIL here,
    # forcing its removal from the set once COW-3894 converts it.
    stale = sorted(
        rel for rel in KNOWN_MULTI_ACTOR_SELF_SENDS
        if _self_send_pos((EXAMPLES_DIR / rel).read_text()) == -1
    )
    assert stale == [], (
        "no longer self-send — drop from KNOWN_MULTI_ACTOR_SELF_SENDS "
        "(COW-3894 converted them?): " + repr(stale)
    )
    offenders = []
    for path in sorted(EXAMPLES_DIR.rglob("*.py")):
        rel = str(path.relative_to(EXAMPLES_DIR))
        if path.name.startswith("test_") or rel in KNOWN_MULTI_ACTOR_SELF_SENDS:
            continue
        text = path.read_text(errors="ignore")
        idx = _self_send_pos(text)
        if idx != -1 and "await runner." in text[idx:]:
            offenders.append(rel)
    assert offenders == [], offenders


def test_chatbot_route_handlers_are_direct_runner_continuations() -> None:
    """Scoped to the two SINGLE-actor bridges this PR converted (telegram +
    cowbot) — NOT repo-wide. Each route handler carries @runner.continuation
    directly above `async def`, and neither file keeps a @deferred handler or a
    self-send: the route handler IS the continuation, so its runner JobSubmit is
    a first-level child of the gateway-signed tx (COW-3878 / D36)."""
    for relpath, handler in CONTINUATION_ROUTE_HANDLERS:
        text = (EXAMPLES_DIR / relpath).read_text()
        pattern = re.compile(
            r"@runner\.continuation\s*\n\s*async def " + re.escape(handler) + r"\b"
        )
        assert pattern.search(text), f"{relpath}: {handler} is not a direct @runner.continuation"
        assert "@deferred" not in text, f"{relpath}: still declares a @deferred handler"
        assert _self_send_pos(text) == -1, f"{relpath}: still self-sends"


def test_guided_demo_library_replaces_common_runner() -> None:
    demo_lib = EXAMPLES_DIR / "_demo_lib.sh"
    assert demo_lib.is_file()
    text = demo_lib.read_text()
    for function_name in [
        "demo_begin",
        "demo_step",
        "demo_deploy_actor",
        "demo_call",
        "demo_wait",
        "demo_show_status",
        "demo_done",
    ]:
        assert f"{function_name}()" in text

    stale_patterns = [
        "_demo_common.sh",
        "WAIT_STEP_TEXT",
        "WAIT_STEP_LABEL",
        "INIT_ACTION",
        "RUN_ACTION",
    ]
    offenders = []
    for path in EXAMPLES_DIR.rglob("*.sh"):
        if path.name == "_demo_common.sh":
            continue
        content = path.read_text()
        for pattern in stale_patterns:
            if pattern in content:
                offenders.append(f"{path.relative_to(EXAMPLES_DIR)}: {pattern}")
    assert offenders == []


def main() -> None:
    tests = [
        test_curriculum_and_gallery_directories_exist,
        test_runnable_examples_have_registered_entrypoints,
        test_registered_entrypoint_exceptions_exist_and_are_documented,
        test_staged_offchain_manifests_match_the_node_offchain_schema,
        test_examples_do_not_have_top_level_or_removed_showcase_dirs,
        test_parallel_harness_discovers_nested_examples_and_no_stale_defaults,
        test_smoke_workflow_selectors_name_examples_that_exist,
        test_active_runnable_demos_are_registered_in_sweeps,
        test_non_default_gallery_demos_are_documented_and_not_in_active_sweeps,
        test_runner_help_exposes_gallery_and_all_flags,
        test_spawn_local_all_accepts_empty_selector_list,
        test_default_runner_accepts_empty_selector_list,
        test_local_sweep_rejects_unknown_selectors_before_validator_preflight,
        test_examples_are_cli_projects,
        test_demo_lib_deploys_targets_not_raw_code,
        test_no_generated_artifacts_are_committed_under_examples,
        test_tracked_cache_bytecode_is_rejected_but_local_cache_is_ignored,
        test_top_level_readme_points_to_runner_path_and_gallery,
        test_gallery_readmes_do_not_use_stale_rpc_or_todo_copy,
        test_gallery_readmes_point_to_runnable_entrypoint,
        test_deterministic_gallery_readmes_explain_demo_scope,
        test_cowbot_files_do_not_reference_removed_tunnel_directory,
        test_no_example_self_sends_before_a_runner_await,
        test_chatbot_route_handlers_are_direct_runner_continuations,
        test_guided_demo_library_replaces_common_runner,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")


if __name__ == "__main__":
    main()
