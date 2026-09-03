"""Placeholder-credential detection.

An unfilled .env is the single most common first-run failure, and it surfaces
as a 401 — which sends you looking for a revoked or wrong-account token
instead of an unedited config file. These tests pin the detection so the
distinction stays.
"""

from __future__ import annotations

import pytest

from igpulse.apify.client import ApifyClient, looks_like_placeholder
from igpulse.config import load_config


@pytest.mark.parametrize(
    "token",
    [
        "",
        "   ",
        "apify_api_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",  # the shipped example
        "apify_api_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
        "your_token_here",
        "<your-apify-token>",
        "change-me",
        "changeme",
        "replace_this_value",
        "placeholder",
        "apify_api_example",
        "short",
    ],
)
def test_placeholders_are_detected(token):
    assert looks_like_placeholder(token) is True


@pytest.mark.parametrize(
    "token",
    [
        "apify_api_9fK2mQ7vB4nL8sT1wR6yU3jH5gD0aZcPeN",
        "apify_api_abcdefghijklmnopqrstuvwxyz01234567",
    ],
)
def test_real_shaped_tokens_pass(token):
    assert looks_like_placeholder(token) is False


def test_client_refuses_to_construct_with_a_placeholder():
    """Fail at construction, not at the first request.

    Otherwise a long ingest starts, runs, and dies on a 401 partway through.
    """
    cfg = load_config()
    with pytest.raises(ValueError) as exc:
        ApifyClient("apify_api_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", cfg.settings.apify)
    message = str(exc.value)
    assert "placeholder" in message.lower()
    assert "API & Integrations" in message


def test_client_refuses_empty_token():
    cfg = load_config()
    with pytest.raises(ValueError):
        ApifyClient("", cfg.settings.apify)
