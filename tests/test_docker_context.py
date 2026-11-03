"""The Dockerfile and .dockerignore must agree.

A `COPY` of a path the build context excludes fails the build with "not found" —
a message that names the destination and says nothing about the cause. This test
reads both files and checks the agreement directly, because the alternative is
discovering it during a deploy.
"""

from __future__ import annotations

import fnmatch
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(ROOT, "Dockerfile")
DOCKERIGNORE = os.path.join(ROOT, ".dockerignore")


def copy_sources() -> list[str]:
    """Every local path the Dockerfile copies in, excluding build args."""
    sources: list[str] = []
    with open(DOCKERFILE, encoding="utf-8") as handle:
        for line in handle:
            match = re.match(r"\s*COPY\s+(?:--\S+\s+)*(.+)$", line)
            if not match:
                continue
            parts = match.group(1).split()
            if len(parts) < 2:
                continue
            # Everything but the destination is a source; a JSON form is rare
            # here and would be caught by the same split.
            sources.extend(part for part in parts[:-1] if not part.startswith("--"))
    return sources


def ignore_patterns() -> list[str]:
    patterns: list[str] = []
    if not os.path.exists(DOCKERIGNORE):
        return patterns
    with open(DOCKERIGNORE, encoding="utf-8") as handle:
        for line in handle:
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            patterns.append(value)
    return patterns


def is_excluded(path: str, patterns: list[str]) -> bool:
    """Docker's rule: a pattern matches the path or any parent directory."""
    candidates = [path] + [path.rsplit("/", i)[0] for i in range(1, path.count("/") + 1)]
    excluded = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        # Docker treats "tests/" and "tests" alike; fnmatch does not.
        clean = pattern.lstrip("!").rstrip("/")
        for candidate in candidates:
            if fnmatch.fnmatch(candidate, clean) or fnmatch.fnmatch(
                os.path.basename(candidate), clean
            ):
                excluded = not negated
    return excluded


def test_the_dockerfile_copies_something():
    assert copy_sources(), "the Dockerfile copies nothing?"


def test_every_copied_path_is_inside_the_build_context():
    """The exact failure this test exists for: COPY tests ./tests."""
    patterns = ignore_patterns()
    offenders = [source for source in copy_sources() if is_excluded(source, patterns)]

    assert not offenders, (
        f"the Dockerfile copies {offenders}, which .dockerignore excludes — "
        "the build will fail with 'not found'"
    )


def test_every_copied_path_exists():
    missing = [
        source for source in copy_sources()
        if not os.path.exists(os.path.join(ROOT, source))
    ]

    assert not missing, f"the Dockerfile copies paths that are not there: {missing}"


def test_the_suite_ships_in_the_image():
    """The deployment check is `docker compose exec backend pytest`.

    An image without the tests cannot answer it, so `tests` must be copied and
    must not be ignored.
    """
    assert "tests" in copy_sources()
    assert not is_excluded("tests", ignore_patterns())


def test_secrets_are_still_excluded():
    """The reason .dockerignore exists at all."""
    patterns = ignore_patterns()

    assert is_excluded(".env", patterns)
    assert is_excluded("local.db", patterns)
