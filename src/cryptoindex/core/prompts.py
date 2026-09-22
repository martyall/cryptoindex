from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


@dataclass(frozen=True, slots=True)
class Prompt:
    version: str  # the file's stem, e.g. "gloss-v1" (Invariant 10)
    text: str


def load_prompt(version: str, directory: Path = PROMPTS_DIR) -> Prompt:
    """Raises FileNotFoundError if `<directory>/<version>.md` does not exist."""
    return Prompt(version=version, text=(directory / f"{version}.md").read_text())
