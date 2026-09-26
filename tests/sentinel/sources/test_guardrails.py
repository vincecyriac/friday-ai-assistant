"""The two promises that keep this sub-project safe, enforced mechanically."""
import ast
import pathlib

import friday
import friday.sentinel.sources as sources_pkg

FRIDAY_ROOT = pathlib.Path(friday.__file__).resolve().parent
SOURCES_ROOT = pathlib.Path(sources_pkg.__file__).resolve().parent


def _python_files(root: pathlib.Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _executable_string_literals(path: pathlib.Path) -> list[str]:
    """Every string constant that is not a docstring — prose may discuss what
    the code may not do."""
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


def test_nothing_in_friday_can_send_mail():
    """Structural for IMAP: with no SMTP client and no SMTP credential in the
    registry, sending is not something this program can do."""
    offenders = []
    for path in _python_files(FRIDAY_ROOT):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [f"{path.name}: import {a.name}" for a in node.names
                              if a.name.split(".")[0] == "smtplib"]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "smtplib":
                offenders.append(f"{path.name}: from smtplib")
    assert not offenders, offenders


def test_no_source_module_names_a_send_endpoint():
    """The Gmail guarantee is a scope promise, so this is the thing enforcing it."""
    offenders = []
    for path in _python_files(SOURCES_ROOT):
        for literal in _executable_string_literals(path):
            if "send" in literal.lower():
                offenders.append(f"{path.name}: {literal[:60]!r}")
    assert not offenders, offenders


def test_the_guardrail_would_actually_catch_a_violation(tmp_path):
    """A test that cannot fail is not a guardrail."""
    sneaky = tmp_path / "sneaky.py"
    sneaky.write_text('"""A docstring may mention the dispatch endpoint."""\n'
                      'PATH = "/gmail/v1/users/me/messages/send"\n')
    literals = _executable_string_literals(sneaky)
    assert any("send" in s for s in literals)
    assert not any("docstring" in s for s in literals)     # docstrings are excluded
