"""Actor input builders and output normalisation.

Every actor-specific field name lives here. The rest of the codebase works with
the normalised dataclasses below, so swapping an actor is a change to this file
plus config, not a change to ingest/analysis logic.

Input field names verified against the published input schemas for
``apify/instagram-scraper`` and ``apify/instagram-comment-scraper``.

Output field names are the documented shape of the Instagram scrapers' dataset
records. Instagram changes its internals often and the actors track those
changes, so every extractor here is defensive: missing fields degrade to None
rather than raising, and :func:`normalise_post` records which fields it could
not find so a run can be audited rather than silently thinning out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Input builders
# --------------------------------------------------------------------------- #
def build_hashtag_search_input(
    terms: list[str],
    *,
    results_limit: int,
    lookback: str,
) -> dict[str, Any]:
    """Input for apify/instagram-scraper in hashtag-search mode.

    `search` takes a single term, so callers issue one run per term; that also
    keeps per-narrative attribution unambiguous when a post matches two terms.
    """
    if not terms:
        raise ValueError("build_hashtag_search_input called with no terms")
    if len(terms) != 1:
        raise ValueError(
            "instagram-scraper accepts one `search` value per run; issue one "
            f"run per term (got {len(terms)})"
        )
    term = terms[0]
    return {
        "search": term,
        "searchType": "hashtag" if term.startswith("#") else "user",
        # searchLimit is capped at 250 by the actor's schema.
        "searchLimit": min(results_limit, 250),
        "resultsType": "posts",
        "resultsLimit": results_limit,
        "onlyPostsNewerThan": lookback,
        "addParentData": True,
    }


def build_profile_input(
    handles: list[str],
    *,
    posts_per_handle: int,
    lookback: str,
) -> dict[str, Any]:
    """Input for apify/instagram-scraper against specific profile URLs."""
    if not handles:
        raise ValueError("build_profile_input called with no handles")
    return {
        "directUrls": [f"https://www.instagram.com/{h}/" for h in handles],
        "resultsType": "details",
        "resultsLimit": posts_per_handle,
        "onlyPostsNewerThan": lookback,
        "addParentData": True,
    }


def build_comment_input(
    post_urls: list[str],
    *,
    comments_per_post: int,
    include_nested: bool,
) -> dict[str, Any]:
    """Input for apify/instagram-comment-scraper."""
    if not post_urls:
        raise ValueError("build_comment_input called with no post URLs")
    return {
        "directUrls": post_urls,
        "resultsLimit": comments_per_post,
        "includeNestedComments": include_nested,
    }


def build_sentiment_input(texts: list[str], input_field: str) -> dict[str, Any]:
    """Input for the configured sentiment actor.

    The store sentiment actors take a single text field, so a batch is joined
    with newlines and split back out by line index. Any text containing a
    newline is collapsed first, otherwise the split would desynchronise the
    response from the request.
    """
    flattened = [" ".join(t.split()) for t in texts]
    return {input_field: "\n".join(flattened)}


# --------------------------------------------------------------------------- #
# Output normalisation
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RawPost:
    shortcode: str
    author_handle: str | None
    author_is_verified: bool | None
    posted_at: datetime
    caption: str | None
    like_count: int | None
    comment_count: int | None
    is_video: bool | None
    view_count: int | None
    url: str
    missing_fields: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RawComment:
    external_id: str
    post_shortcode: str | None
    author_handle: str | None
    posted_at: datetime | None
    body: str | None
    like_count: int | None


@dataclass(slots=True)
class RawProfile:
    handle: str
    follower_count: int | None
    following_count: int | None
    post_count: int | None
    is_verified: bool | None
    biography: str | None


def _first(record: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-None value among `keys`."""
    for key in keys:
        value = record.get(key)
        if value is not None:
            return value
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse Apify timestamps into tz-aware UTC datetimes.

    The Instagram actors emit ISO-8601 strings; some fields carry Unix epochs.
    Naive values are treated as UTC because the actor documentation states
    "Times are in UTC, not local time" — assuming local here would silently
    shift every timestamp by the runner's offset.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            logger.debug("unparseable timestamp: %r", value)
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalise_post(record: dict[str, Any]) -> RawPost | None:
    """Map an instagram-scraper post record onto RawPost.

    Returns None when the record has no usable identity or timestamp, since a
    post without either cannot be deduped or placed on a timeline.
    """
    shortcode = _first(record, "shortCode", "shortcode", "code")
    posted_at = _parse_timestamp(_first(record, "timestamp", "takenAt", "taken_at"))
    if not shortcode or posted_at is None:
        logger.debug(
            "dropping post record: shortcode=%r posted_at=%r", shortcode, posted_at
        )
        return None

    missing: list[str] = []
    caption = _first(record, "caption", "text")
    if caption is None:
        missing.append("caption")

    like_count = _as_int(_first(record, "likesCount", "likeCount"))
    if like_count is None:
        missing.append("likesCount")

    owner = record.get("owner") if isinstance(record.get("owner"), dict) else {}
    author_handle = _first(record, "ownerUsername", "username") or owner.get("username")
    if not author_handle:
        missing.append("ownerUsername")

    url = _first(record, "url", "postUrl") or (
        f"https://www.instagram.com/p/{shortcode}/"
    )

    return RawPost(
        shortcode=str(shortcode),
        author_handle=author_handle,
        author_is_verified=_first(record, "isVerified", "ownerIsVerified"),
        posted_at=posted_at,
        caption=caption,
        like_count=like_count,
        comment_count=_as_int(_first(record, "commentsCount", "commentCount")),
        is_video=_first(record, "isVideo", "is_video"),
        view_count=_as_int(_first(record, "videoViewCount", "videoPlayCount")),
        url=str(url),
        missing_fields=missing,
    )


def normalise_comment(record: dict[str, Any]) -> RawComment | None:
    external_id = _first(record, "id", "commentId")
    if not external_id:
        return None
    owner = record.get("owner") if isinstance(record.get("owner"), dict) else {}
    return RawComment(
        external_id=str(external_id),
        post_shortcode=_first(record, "postShortCode", "shortCode"),
        author_handle=_first(record, "ownerUsername", "username")
        or owner.get("username"),
        posted_at=_parse_timestamp(_first(record, "timestamp", "createdAt")),
        body=_first(record, "text", "comment"),
        like_count=_as_int(_first(record, "likesCount", "likeCount")),
    )


def normalise_profile(record: dict[str, Any]) -> RawProfile | None:
    handle = _first(record, "username", "ownerUsername")
    if not handle:
        return None
    return RawProfile(
        handle=str(handle).lower(),
        follower_count=_as_int(_first(record, "followersCount", "followers")),
        following_count=_as_int(_first(record, "followsCount", "following")),
        post_count=_as_int(_first(record, "postsCount", "mediaCount")),
        is_verified=_first(record, "verified", "isVerified"),
        biography=_first(record, "biography", "bio"),
    )
