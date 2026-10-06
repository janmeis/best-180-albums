import unittest
from pathlib import Path

from make_best_180_playlist import find_album


class MatchAlbumTitlesTests(unittest.TestCase):
    def test_john_coltrane_standard_match_ignores_catalog_credit(self) -> None:
        collection = Path("M:/")
        album = find_album(collection, "John Coltrane", "Both Directions at Once (IMPULSE!)", "2018")
        self.assertIsNotNone(album)
        self.assertEqual(album.name, "[2018] Both Directions at Once: The Lost Album")


if __name__ == "__main__":
    unittest.main()
