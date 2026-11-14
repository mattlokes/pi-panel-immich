"""ImmichClient's random-asset fetch, with the HTTP session mocked out.
Offline: no Immich server."""

from __future__ import annotations

from unittest.mock import MagicMock

import requests

from pi_panel_immich.client import ImmichClient


def _client(response: MagicMock) -> tuple[ImmichClient, MagicMock]:
    session = MagicMock(spec=requests.Session)
    session.headers = {}
    session.post.return_value = response
    return ImmichClient("http://immich.test/", "key", session=session), session


def test_random_assets_uses_search_random():
    resp = MagicMock()
    resp.json.return_value = [{"id": "a", "type": "IMAGE"}, {"id": "b", "type": "VIDEO"}]
    client, session = _client(resp)

    assert client.get_random_assets(count=7) == [{"id": "a", "type": "IMAGE"}]
    session.post.assert_called_once_with(
        "http://immich.test/api/search/random",
        json={"size": 7, "type": "IMAGE"},
        timeout=10,
    )
    session.get.assert_not_called()


def test_random_assets_error_returns_empty():
    resp = MagicMock()
    resp.raise_for_status.side_effect = requests.HTTPError("400 Client Error")
    client, _ = _client(resp)

    assert client.get_random_assets() == []
