import pytest
from pydantic import ValidationError

from lean_weave.context.models import (
    CodeExcerpt,
    ContextQuery,
    ExpansionLevel,
    SourceRange,
    content_digest,
    iter_lines,
    normalize_relative_path,
)
from lean_weave.errors import ForbiddenContext, InvalidContextRequest


def test_digest_and_lines_are_canonical():
    assert content_digest("a\r\nb") == content_digest("a\r\nb")
    assert iter_lines("") == []
    assert iter_lines("a") == ["a"]
    assert iter_lines("a\n") == ["a"]
    assert iter_lines("a\n\n") == ["a", ""]
    assert iter_lines("\n") == [""]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("pkg/mod.py", "pkg/mod.py"),
        ("./pkg/mod.py", "pkg/mod.py"),
        ("pkg\\mod.py", "pkg/mod.py"),
    ],
)
def test_normalize_relative_path(value, expected):
    assert normalize_relative_path(value) == expected


@pytest.mark.parametrize("value", ["", "/abs/path", "../escape", "pkg/../x", "a//b", "a\x00b"])
def test_normalize_rejects_unsafe(value):
    with pytest.raises(InvalidContextRequest):
        normalize_relative_path(value)


def test_source_range_bounds():
    with pytest.raises(ValidationError):
        SourceRange(path="a.py", start_line=0, end_line=1)
    with pytest.raises(ValidationError):
        SourceRange(path="a.py", start_line=3, end_line=2)


def test_full_requires_one_path_and_reason():
    with pytest.raises(ValidationError):
        ContextQuery(task="work", level=ExpansionLevel.FULL, paths=[])
    with pytest.raises(ValidationError):
        ContextQuery(
            task="work",
            level=ExpansionLevel.FULL,
            paths=["a.py", "b.py"],
            reason="why",
        )
    with pytest.raises(ValidationError):
        ContextQuery(task="work", level=ExpansionLevel.FULL, paths=["a.py"])
    query = ContextQuery(
        task="work",
        level=ExpansionLevel.FULL,
        paths=["a.py"],
        reason="whole file needed",
    )
    assert query.level == ExpansionLevel.FULL


def test_secret_task_text_is_rejected_without_echo():
    secret = "api_key='abcdefghijklmnopqrstuvwxyz012345'"
    with pytest.raises(ForbiddenContext) as error:
        ContextQuery(task=secret)
    assert "abcdefghijklmnopqrstuvwxyz012345" not in str(error.value)


def test_code_excerpt_gets_default_provenance():
    excerpt = CodeExcerpt(
        path="a.py",
        start_line=2,
        end_line=4,
        text="x",
        digest="sha256:x",
        kind="range",
    )
    assert excerpt.provenance == [SourceRange(path="a.py", start_line=2, end_line=4)]
