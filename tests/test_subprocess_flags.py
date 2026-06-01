import ast
from pathlib import Path


def test_subprocess_calls_have_creation_flags() -> None:
    """
    Ensure all subprocess calls include creationflags=subprocess.CREATE_NO_WINDOW.
    This prevents sudden command-line windows from popping up and triggering/annoying the user
    when background tasks are executed from the UI application.
    """
    src_dir = Path(__file__).parent.parent / "src" / "win_search_aliases"

    missing_flags = []

    for py_file in src_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(py_file))

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                is_subprocess_call = False
                if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    if node.func.value.id == "subprocess" and node.func.attr in (
                        "run",
                        "Popen",
                        "check_call",
                        "check_output",
                        "call",
                    ):
                        is_subprocess_call = True

                if is_subprocess_call:
                    has_creationflags = False
                    for kw in node.keywords:
                        if kw.arg == "creationflags":
                            has_creationflags = True
                            break

                    if not has_creationflags:
                        missing_flags.append(f"{py_file.name}:{node.lineno}")

    assert not missing_flags, f"subprocess calls missing creationflags=subprocess.CREATE_NO_WINDOW: {missing_flags}"
