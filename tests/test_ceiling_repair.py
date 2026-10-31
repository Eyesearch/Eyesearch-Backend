"""The gates added for the second hygiene pass: quality, revalidation, repair."""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.engine.store import PageDocument

quality_module = load_agent("04_quality_agent")
enforcer_module = load_agent("11_rule_enforcer_agent")
revalidation = load_agent("06_revalidation_agent")

BODY = "real body text that is long enough to pass the density gate easily. " * 3


def document(title: str, body: str = BODY, digest: str = "d") -> PageDocument:
    return PageDocument(
        url="https://a.example/x", domain="a.example", title=title,
        description="d", body_text=body, digest=digest,
    )


# ---------------------------------------------------------------------------
# Quality: a title must be words, not punctuation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", [".in", "-::", "::", "...", "|"])
def test_a_punctuation_only_title_is_rejected(title):
    """Too short is caught first; past that, punctuation alone is caught."""
    verdict = quality_module.QualityAgent().check(document(title))

    assert verdict.ok is False
    assert verdict.reason in {"no_title", "title_not_a_word"}


def test_a_kannada_title_is_not_punctuation():
    """The alphanumeric test has to cover the scripts the index serves."""
    verdict = quality_module.QualityAgent().check(document("ಕರ್ನಾಟಕ ಆರೋಗ್ಯ"))

    assert verdict.ok is True


@pytest.mark.parametrize("title", ["404 Not Found", "Access Denied", "403 Forbidden"])
def test_a_junk_title_is_rejected_even_with_a_long_body(title):
    """A 404 can carry a body full of navigation and still be a 404."""
    verdict = quality_module.QualityAgent().check(document(title))

    assert verdict.ok is False
    assert verdict.reason == "junk_page"


# ---------------------------------------------------------------------------
# Revalidation: the overwrite path runs the same gates
# ---------------------------------------------------------------------------


def test_revalidation_imports_the_sanitisers_it_uses():
    """A NameError here would only fire on the first real revalidation."""
    for name in (
        "sanitize_brand",
        "brand_from_domain",
        "format_company_bounded_title",
        "format_ai_snippet",
    ):
        assert callable(getattr(revalidation, name)), name


def test_revalidation_uses_the_same_caps_as_ingestion():
    assert revalidation.TITLE_CHAR_CAP == 50
    assert revalidation.BRAND_CHAR_CAP == 35
    assert revalidation.DESCRIPTION_CHAR_CAP == 300


# ---------------------------------------------------------------------------
# Repair: stored rows written before the ceiling existed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_stored_titles_are_trimmed(client):
    await client.execute(
        "INSERT INTO pages (url, title, description, body_text, digest, brand_name) "
        "VALUES ('https://a.example/long', ?, ?, 'body', 'rep-1', ?)",
        ("T" * 120, "D" * 500, "B" * 120),
    )
    from app.engine.config import Settings

    agent = enforcer_module.RuleEnforcerAgent(client, Settings(_env_file=None, environment="test"))

    assert await agent.enforce_title_ceiling(dry_run=True) == 1
    assert await agent.enforce_brand_ceiling(dry_run=True) == 1
    assert await agent.enforce_description_ceiling(dry_run=True) == 1

    await agent.enforce_title_ceiling()
    await agent.enforce_brand_ceiling()
    await agent.enforce_description_ceiling()

    row = await client.execute(
        "SELECT length(title), length(brand_name), length(description) "
        "FROM pages WHERE url = 'https://a.example/long'"
    )
    assert tuple(int(value) for value in row.rows[0]) == (50, 35, 300)


@pytest.mark.asyncio
async def test_a_column_name_cannot_be_injected(client):
    from app.engine.config import Settings

    agent = enforcer_module.RuleEnforcerAgent(client, Settings(_env_file=None, environment="test"))

    with pytest.raises(ValueError):
        await agent._enforce_ceiling("title; DROP TABLE pages", 50, dry_run=True)
