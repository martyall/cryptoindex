import ast
from pathlib import Path

QUERY_PKG = Path(__file__).resolve().parents[1] / "src" / "cryptoindex" / "query"


def imported_modules(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_query_code_never_imports_ingestion() -> None:
    offenders = {
        f"{path.name}: {name}"
        for path in QUERY_PKG.rglob("*.py")
        for name in imported_modules(path)
        if name == "cryptoindex.ingest" or name.startswith("cryptoindex.ingest.")
    }
    assert offenders == set()
