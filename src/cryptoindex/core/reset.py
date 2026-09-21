"""`make reset`'s data-directory step: resolve CI_DATA_DIR through the same
config loader the app uses, refuse a dangerous target, ask, then delete it."""

import shutil
import sys
from pathlib import Path

from cryptoindex.core import config


def unsafe_reason(data_dir: Path, repo: Path) -> str | None:
    """Why deleting `data_dir` would be dangerous, or None. Both paths must be
    resolved (absolute, symlinks followed)."""
    if data_dir == Path(data_dir.anchor):
        return "it is a filesystem root"
    if data_dir == Path.home():
        return "it is the home directory"
    if data_dir == repo or data_dir in repo.parents:
        return "it contains the repository"
    return None


def main() -> None:
    data_dir = config.settings.data_dir.resolve()
    if reason := unsafe_reason(data_dir, Path.cwd().resolve()):
        sys.exit(f"refusing to delete CI_DATA_DIR={data_dir}: {reason}")
    answer = input(f"Delete the database and everything in {data_dir}? [y/N] ")
    if answer.strip().lower() != "y":
        sys.exit("aborted")
    shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
