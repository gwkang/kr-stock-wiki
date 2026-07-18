from pathlib import Path

from kr_stock_wiki.sync import sync_wiki_tree


def test_sync_rejects_same_or_nested_source_and_target(tmp_path: Path):
    import pytest

    source = tmp_path / "source"
    source.mkdir()
    nested = source / "target"
    nested.mkdir()

    with pytest.raises(ValueError, match="overlap"):
        sync_wiki_tree(source, source)
    with pytest.raises(ValueError, match="overlap"):
        sync_wiki_tree(source, nested)


def test_sync_rejects_symlink_sources(tmp_path: Path):
    import pytest

    real = tmp_path / "real"
    real.mkdir()
    (real / "Home.md").write_text("home", encoding="utf-8")
    source = tmp_path / "source"
    source.symlink_to(real, target_is_directory=True)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ValueError, match="symlink"):
        sync_wiki_tree(source, target)


def test_sync_wiki_tree_copies_markdown_and_removes_stale_pages(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / "stocks").mkdir(parents=True)
    target.mkdir()
    (source / "Home.md").write_text("home", encoding="utf-8")
    (source / "stocks" / "005930.md").write_text("report", encoding="utf-8")
    (source / "raw.json").write_text("{}", encoding="utf-8")
    (target / "Stale.md").write_text("stale", encoding="utf-8")

    changed = sync_wiki_tree(source, target)

    assert sorted(
        path.relative_to(target).as_posix() for path in target.rglob("*.md")
    ) == ["Home.md", "stocks/005930.md"]
    assert "Stale.md" in changed.removed
    assert "Home.md" in changed.copied
