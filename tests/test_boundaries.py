import ast
import importlib.util
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "cryptoindex"
QUERY = SRC / "query"
INGEST = SRC / "ingest"


def module_name(path: Path) -> str:
    parts = path.relative_to(SRC.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def imported_modules(path: Path) -> set[str]:
    """Absolute names of every module the file imports, relative imports
    resolved against the file's package. `from pkg import name` also counts
    `pkg.name`, in case `name` is a submodule."""
    module = module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            relative = "." * node.level + (node.module or "")
            base = importlib.util.resolve_name(relative, package)
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def defined_in(name: str, directory: Path) -> bool:
    try:
        spec = importlib.util.find_spec(name)
    except ModuleNotFoundError:
        return False  # `from pkg import function`: pkg.function is not a module
    return (
        spec is not None
        and spec.origin is not None
        and Path(spec.origin).resolve().is_relative_to(directory)
    )


def test_query_code_never_imports_ingestion() -> None:
    offenders = {
        f"{path.relative_to(SRC)} imports {name}"
        for path in QUERY.rglob("*.py")
        for name in imported_modules(path)
        if defined_in(name, INGEST)
    }
    assert offenders == set()


def test_the_check_catches_relative_and_from_imports() -> None:
    probe = QUERY / "_boundary_probe.py"
    probe.write_text("from ..ingest import runner\nfrom cryptoindex import ingest\n")
    try:
        found = {n for n in imported_modules(probe) if defined_in(n, INGEST)}
    finally:
        probe.unlink()
    assert found == {"cryptoindex.ingest.runner", "cryptoindex.ingest"}
