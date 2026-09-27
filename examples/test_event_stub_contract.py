"""Check event stubs without importing legacy examples that are no longer present."""

import ast
from pathlib import Path

import pytest


HERE = Path(__file__).parent


@pytest.mark.parametrize(
    "source, function",
    [
        ("test_emit_and_storage_encoding.py", "_emit_event"),
        ("test_non_object_runner_results.py", "emit_event"),
        ("test_runner_result_migration.py", "emit_event"),
    ],
)
def test_event_stub_accepts_keyword_schema_and_returns_emit_result(source, function):
    tree = ast.parse((HERE / source).read_text())
    nodes = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == function]
    assert len(nodes) == 1
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    namespace = {"EVENTS": []}
    exec(compile(module, source, "exec"), namespace)
    emit = namespace[function]

    result = emit("typed", b"payload", schema=b"A" * 32)
    assert result == {"emit_id": 1, "sync_outcomes": [], "deferred_subs": []}
    assert namespace["EVENTS"] == [("typed", b"payload")]
    with pytest.raises(TypeError):
        emit("typed", b"payload", b"A" * 32)
