from __future__ import annotations

import pytest

from draftomen.card_data_client import CardDataClient


@pytest.fixture(autouse=True)
def _no_hosted_sets_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop card-data clients built without an explicit manifest URL from reaching the website.
    Tests that exercise manifest checks pass sets_manifest_url themselves.
    """

    monkeypatch.setitem(CardDataClient.__init__.__kwdefaults__, "sets_manifest_url", None)

