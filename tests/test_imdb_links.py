from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from notion_db_updater.graph import omdb_search, read_page, route_after_search
from notion_db_updater.models import Entry
from notion_db_updater.omdb import OMDbClient


class IMDbLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_uses_identity_lookup_not_title_search(self):
        client = object.__new__(OMDbClient)
        client._get = AsyncMock(
            return_value={
                "Response": "True",
                "imdbID": "tt2583620",
                "Title": "Verified title",
                "Year": "2020",
                "Type": "movie",
            }
        )
        for query in (
            "https://www.imdb.com/title/tt2583620/?ref_=example",
            "<https://www.imdb.com/title/tt2583620/?ref_=example|Some label>",
            "<https://www.imdb.com/title/tt2583620/?ref_=example|"
            "https://www.imdb.com/title/tt2583620/?ref_=example>",
            "tt2583620",
        ):
            with self.subTest(query=query):
                candidates = await client.search(query)
                client._get.assert_awaited_with({"i": "tt2583620", "plot": "full"})
                self.assertEqual(candidates[0].title, "Verified title")

    async def test_enrichment_resolves_link_before_both_search_lanes(self):
        client = object.__new__(OMDbClient)
        client._get = AsyncMock(
            return_value={
                "Response": "True",
                "imdbID": "tt2583620",
                "Title": "Verified title",
                "Year": "2020",
                "Type": "movie",
            }
        )
        entry = Entry.from_page(
            {
                "id": "page-1",
                "properties": {
                    "Title": {
                        "title": [
                            {
                                "plain_text": "<https://www.imdb.com/title/tt2583620/?ref_=example|label>"
                            }
                        ]
                    }
                },
            }
        )
        notion = AsyncMock()
        notion.get_entry.return_value = entry
        state = {"page_id": entry.page_id}
        state.update(await read_page(state, notion=notion, omdb=client))
        self.assertEqual(state["entry"].title, "Verified title")
        state.update(await omdb_search(state, omdb=client))
        self.assertEqual(state["chosen_imdb_id"], "tt2583620")
        self.assertEqual(route_after_search(state), "omdb_details")
        client._get.assert_awaited_once_with({"i": "tt2583620", "plot": "full"})

    async def test_identity_mismatch_is_rejected(self):
        client = object.__new__(OMDbClient)
        client._get = AsyncMock(
            return_value={
                "Response": "True",
                "imdbID": "tt0000001",
                "Title": "Wrong title",
            }
        )
        with self.assertRaises(ValueError):
            await client.search("https://www.imdb.com/title/tt2583620/")

    async def test_plain_title_still_uses_title_search(self):
        client = object.__new__(OMDbClient)
        client._get = AsyncMock(return_value={"Response": "False"})
        self.assertEqual(await client.search("Dune", "Movie"), [])
        client._get.assert_awaited_once_with({"s": "Dune", "type": "movie"})
