import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = (
    ROOT / "api",
    ROOT / "scripts",
    ROOT / "skills" / "osc-orchestrator",
)
HELPER_MARKERS = ("{osc_todo_source_sql", "{calendar_todo_source_sql")


def test_calendar_source_sql_helpers_are_interpolated_before_execution():
    offenders: list[str] = []
    for base in SOURCE_DIRS:
        for path in sorted(base.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if any(marker in node.value for marker in HELPER_MARKERS):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert offenders == []
