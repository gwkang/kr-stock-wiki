from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import yaml


REQUIRED = {
    "title",
    "created",
    "updated",
    "type",
    "tags",
    "sources",
    "as_of",
    "confidence",
}
CONFIDENCE = {"high", "medium", "low"}
LINK_RE = re.compile(r"\[\[([^\]|#]+)")


@dataclass(frozen=True)
class WikiIssue:
    code: str
    path: Path
    message: str


def _frontmatter(text: str) -> dict | None:
    if not text.startswith("---\n"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    data = yaml.safe_load(parts[1])
    return data if isinstance(data, dict) else None


def _normalize_target(target: str) -> str | None:
    path = PurePosixPath(target.strip())
    if path.is_absolute() or ".." in path.parts:
        return None
    normalized = path.as_posix().removesuffix(".md")
    return normalized or None


def _aware_datetime(value: object) -> bool:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return False
    else:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _valid_date(value: object) -> bool:
    if isinstance(value, datetime):
        return True
    if isinstance(value, date):
        return True
    if isinstance(value, str):
        try:
            date.fromisoformat(value)
            return True
        except ValueError:
            return False
    return False


def _valid_url(value: object) -> bool:
    if not isinstance(value, str) or any(char in value for char in "\r\n"):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _metadata_issues(path: Path, metadata: dict) -> list[WikiIssue]:
    issues: list[WikiIssue] = []
    missing = REQUIRED - metadata.keys()
    if missing:
        issues.append(
            WikiIssue(
                "missing-metadata", path, f"필수 메타데이터 누락: {sorted(missing)}"
            )
        )
        return issues
    if not isinstance(metadata["title"], str) or not metadata["title"].strip():
        issues.append(
            WikiIssue(
                "invalid-title", path, "title은 비어 있지 않은 문자열이어야 합니다"
            )
        )
    if not isinstance(metadata["type"], str) or not metadata["type"].strip():
        issues.append(
            WikiIssue("invalid-type", path, "type은 비어 있지 않은 문자열이어야 합니다")
        )
    tags = metadata["tags"]
    if (
        not isinstance(tags, list)
        or not tags
        or not all(isinstance(tag, str) and tag for tag in tags)
    ):
        issues.append(
            WikiIssue(
                "invalid-tags", path, "tags는 비어 있지 않은 문자열 목록이어야 합니다"
            )
        )
    sources = metadata["sources"]
    if (
        not isinstance(sources, list)
        or not sources
        or not all(_valid_url(source) for source in sources)
    ):
        issues.append(
            WikiIssue(
                "invalid-source", path, "sources는 유효한 HTTP(S) URL 목록이어야 합니다"
            )
        )
    confidence = metadata["confidence"]
    if not isinstance(confidence, str) or confidence not in CONFIDENCE:
        issues.append(
            WikiIssue(
                "invalid-confidence", path, "confidence 허용값은 high/medium/low입니다"
            )
        )
    if not _valid_date(metadata["created"]):
        issues.append(
            WikiIssue("invalid-created", path, "created는 유효한 날짜여야 합니다")
        )
    if not _valid_date(metadata["updated"]):
        issues.append(
            WikiIssue("invalid-updated", path, "updated는 유효한 날짜여야 합니다")
        )
    if not _aware_datetime(metadata["as_of"]):
        issues.append(
            WikiIssue(
                "invalid-as-of", path, "as_of는 timezone이 포함된 ISO 시각이어야 합니다"
            )
        )
    return issues


def lint_wiki(root: Path) -> list[WikiIssue]:
    if not root.exists() or not root.is_dir():
        return [WikiIssue("invalid-root", root, "Wiki 경로가 디렉터리가 아닙니다")]
    files = list(root.rglob("*.md"))
    page_paths = {path.relative_to(root).with_suffix("").as_posix() for path in files}
    issues: list[WikiIssue] = []
    if not files:
        issues.append(WikiIssue("empty-wiki", root, "Wiki에 Markdown 문서가 없습니다"))
    if "Home" not in page_paths:
        issues.append(WikiIssue("missing-home", root, "Home.md가 없습니다"))
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError as error:
            issues.append(WikiIssue("unreadable-file", path, str(error)))
            continue
        try:
            metadata = _frontmatter(text)
        except yaml.YAMLError as error:
            issues.append(WikiIssue("invalid-frontmatter", path, str(error)))
            metadata = None
        if metadata is None and not any(
            issue.path == path and issue.code == "invalid-frontmatter"
            for issue in issues
        ):
            issues.append(
                WikiIssue("missing-frontmatter", path, "YAML frontmatter가 없습니다")
            )
        elif metadata is not None:
            issues.extend(_metadata_issues(path, metadata))
        for raw_target in LINK_RE.findall(text):
            target = _normalize_target(raw_target)
            if target is None or target not in page_paths:
                issues.append(
                    WikiIssue("broken-wikilink", path, f"대상 문서 없음: {raw_target}")
                )
    return issues
