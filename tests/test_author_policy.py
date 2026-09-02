"""Tests for the author-policy chokepoint.

These are the tests that matter most in this repo. If they fail, the system is
no longer doing the thing that separates it from a watchlist.
"""

from __future__ import annotations

import pytest

from igpulse.config import PublicFigure, PublicFigureList
from igpulse.privacy.author_policy import (
    AuthorPolicy,
    HandleLeakError,
    NamedAuthor,
    PseudonymousAuthor,
)

JUSTIFICATION = "Sitting Member of Parliament; official constituency account."


@pytest.fixture()
def allowlist() -> PublicFigureList:
    return PublicFigureList(
        figures=[
            PublicFigure(
                handle="example_mp",
                display_name="A. N. Example",
                category="elected_official",
                jurisdiction="Lok Sabha",
                justification=JUSTIFICATION,
            )
        ]
    )


def test_allowlisted_handle_resolves_to_named_author(allowlist):
    policy = AuthorPolicy(allowlist)
    author = policy.resolve("example_mp")
    assert isinstance(author, NamedAuthor)
    assert author.handle == "example_mp"
    assert author.is_named is True


def test_handle_normalisation_is_case_and_at_insensitive(allowlist):
    policy = AuthorPolicy(allowlist)
    for variant in ("@Example_MP", "EXAMPLE_MP", "  example_mp  "):
        assert isinstance(policy.resolve(variant), NamedAuthor)


def test_non_allowlisted_handle_resolves_to_pseudonym(allowlist):
    policy = AuthorPolicy(allowlist)
    author = policy.resolve("some_private_citizen")
    assert isinstance(author, PseudonymousAuthor)
    assert author.is_named is False
    # The pseudonym must not contain the handle in any recoverable form.
    assert "some_private_citizen" not in author.pseudonym
    assert len(author.pseudonym) == 16


def test_pseudonym_is_stable_within_a_run(allowlist):
    policy = AuthorPolicy(allowlist)
    first = policy.resolve("private_person")
    second = policy.resolve("@Private_Person")
    assert isinstance(first, PseudonymousAuthor)
    assert isinstance(second, PseudonymousAuthor)
    # Same author, same run -> same bucket, so dedupe and coordination
    # detection work.
    assert first.pseudonym == second.pseudonym


def test_pseudonym_is_unlinkable_across_runs(allowlist):
    """The core privacy guarantee.

    Two runs must not produce the same pseudonym for the same handle, or a
    longitudinal profile of a private citizen could be assembled by joining
    narrative_post rows across runs.
    """
    run_one = AuthorPolicy(allowlist)
    run_two = AuthorPolicy(allowlist)
    first = run_one.resolve("private_person")
    second = run_two.resolve("private_person")
    assert isinstance(first, PseudonymousAuthor)
    assert isinstance(second, PseudonymousAuthor)
    assert first.pseudonym != second.pseudonym


def test_missing_handle_still_yields_a_pseudonym(allowlist):
    policy = AuthorPolicy(allowlist)
    for value in (None, "", "   "):
        author = policy.resolve(value)
        assert isinstance(author, PseudonymousAuthor)


def test_assert_persistable_rejects_non_allowlisted(allowlist):
    policy = AuthorPolicy(allowlist)
    with pytest.raises(HandleLeakError) as exc:
        policy.assert_persistable_handle("random_activist")
    assert "allowlist" in str(exc.value)


def test_assert_persistable_accepts_allowlisted(allowlist):
    policy = AuthorPolicy(allowlist)
    assert policy.assert_persistable_handle("@Example_MP") == "example_mp"


def test_distinct_author_count_excludes_allowlisted(allowlist):
    policy = AuthorPolicy(allowlist)
    policy.resolve("example_mp")          # named, not counted
    policy.resolve("private_one")
    policy.resolve("private_two")
    policy.resolve("private_one")         # repeat
    assert policy.distinct_authors_seen == 2


def test_run_salt_is_not_exposed_on_the_instance(allowlist):
    """A salt reachable through a public attribute would be one refactor away
    from being logged or persisted, which would defeat the guarantee."""
    policy = AuthorPolicy(allowlist)
    public_attrs = [a for a in dir(policy) if not a.startswith("_")]
    assert not any("salt" in a.lower() for a in public_attrs)
