from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SyncChanges:
    copied: tuple[str, ...]
    removed: tuple[str, ...]


def _validate_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{label} cannot be a symlink")
    if not path.exists() or not path.is_dir():
        raise ValueError(f"{label} must be an existing directory")
    if any(item.is_symlink() for item in path.rglob("*")):
        raise ValueError(f"{label} tree cannot contain a symlink")


def _render_for_github_wiki(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].lstrip("\n")
    return text


def sync_wiki_tree(source: Path, target: Path) -> SyncChanges:
    _validate_directory(source, "source")
    _validate_directory(target, "target")
    source_root = source.resolve()
    target_root = target.resolve()
    if (
        source_root == target_root
        or source_root in target_root.parents
        or target_root in source_root.parents
    ):
        raise ValueError("source and target directories cannot overlap")
    source_files = {path.relative_to(source) for path in source.rglob("*.md")}
    target_files = {path.relative_to(target) for path in target.rglob("*.md")}
    removed: list[str] = []
    for relative in sorted(target_files - source_files):
        destination = target / relative
        if target.resolve() not in destination.resolve().parents:
            raise ValueError("target path escaped target directory")
        destination.unlink()
        removed.append(relative.as_posix())
    copied: list[str] = []
    for relative in sorted(source_files):
        destination = target / relative
        if target.resolve() not in destination.resolve().parents:
            raise ValueError("target path escaped target directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            _render_for_github_wiki(source / relative), encoding="utf-8"
        )
        copied.append(relative.as_posix())
    for directory in sorted(
        (path for path in target.rglob("*") if path.is_dir()), reverse=True
    ):
        if not any(directory.iterdir()):
            directory.rmdir()
    return SyncChanges(tuple(copied), tuple(removed))
