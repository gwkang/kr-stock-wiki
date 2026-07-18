from pathlib import Path

from kr_stock_wiki.wiki_lint import lint_wiki


VALID = """---
title: Home
created: 2026-07-18
updated: 2026-07-18
type: summary
tags: [market]
sources: [https://www.nextrade.co.kr/]
as_of: 2026-07-18T20:30:00+09:00
confidence: medium
---
# Home

See [[Methodology]].
"""


def test_lint_accepts_valid_frontmatter_and_wikilinks(tmp_path: Path):
    (tmp_path / "Home.md").write_text(VALID, encoding="utf-8")
    (tmp_path / "Methodology.md").write_text(
        VALID.replace("title: Home", "title: Methodology").replace(
            "[[Methodology]]", "[[Home]]"
        ),
        encoding="utf-8",
    )

    assert lint_wiki(tmp_path) == []


def test_lint_handles_non_string_confidence_and_invalid_dates(tmp_path: Path):
    (tmp_path / "Home.md").write_text(
        VALID.replace("confidence: medium", "confidence: [medium]")
        .replace("created: 2026-07-18", "created: []")
        .replace("updated: 2026-07-18", "updated: null"),
        encoding="utf-8",
    )

    issues = lint_wiki(tmp_path)
    codes = {issue.code for issue in issues}

    assert "invalid-confidence" in codes
    assert "invalid-created" in codes
    assert "invalid-updated" in codes


def test_lint_rejects_empty_wiki(tmp_path: Path):
    issues = lint_wiki(tmp_path)

    assert any(issue.code == "empty-wiki" for issue in issues)
    assert any(issue.code == "missing-home" for issue in issues)


def test_lint_validates_metadata_types_values_and_source_urls(tmp_path: Path):
    (tmp_path / "Home.md").write_text(
        VALID.replace("confidence: medium", "confidence: certain")
        .replace("tags: [market]", "tags: market")
        .replace(
            "sources: [https://www.nextrade.co.kr/]", "sources: [javascript:alert(1)]"
        )
        .replace("as_of: 2026-07-18T20:30:00+09:00", "as_of: 2026-07-18T20:30:00"),
        encoding="utf-8",
    )

    issues = lint_wiki(tmp_path)
    codes = {issue.code for issue in issues}

    assert {
        "invalid-confidence",
        "invalid-tags",
        "invalid-source",
        "invalid-as-of",
    } <= codes


def test_lint_reports_malformed_yaml_without_crashing(tmp_path: Path):
    (tmp_path / "Bad.md").write_text(
        "---\ntitle: [broken\n---\n# Bad", encoding="utf-8"
    )

    issues = lint_wiki(tmp_path)

    assert any(issue.code == "invalid-frontmatter" for issue in issues)


def test_lint_does_not_accept_same_stem_in_wrong_directory(tmp_path: Path):
    (tmp_path / "Home.md").write_text(
        VALID.replace("[[Methodology]]", "[[stocks/Methodology]]"), encoding="utf-8"
    )
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "Methodology.md").write_text(
        VALID.replace("title: Home", "title: Other").replace(
            "[[Methodology]]", "[[Home]]"
        ),
        encoding="utf-8",
    )

    issues = lint_wiki(tmp_path)

    assert any(issue.code == "broken-wikilink" for issue in issues)


def test_lint_reports_missing_metadata_and_broken_link(tmp_path: Path):
    (tmp_path / "Bad.md").write_text("# Bad\n\n[[Missing]]\n", encoding="utf-8")

    issues = lint_wiki(tmp_path)

    assert any(issue.code == "missing-frontmatter" for issue in issues)
    assert any(issue.code == "broken-wikilink" for issue in issues)
