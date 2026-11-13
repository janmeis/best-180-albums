import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from make_best_180_playlist import find_album, format_album_name


class MatchAlbumTitlesTests(unittest.TestCase):
    def test_schnitzler_artist_spelling_variant(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            collection = Path(temporary_directory)
            artist_directory = collection / "Conrad Schnitzler"
            rot = artist_directory / "[1975] Rot"
            blau = artist_directory / "[1973] Blau"
            rot.mkdir(parents=True)
            blau.mkdir()
            self.assertEqual(find_album(collection, "Konrad Schnitzler", "Rot", None), rot)
            self.assertEqual(find_album(collection, "Konrad Schnitzler", "Blau", None), blau)
            self.assertIsNone(find_album(collection, "Konrad Schnitzler", "Unrelated Album", None))

    def test_achim_reichel_aliases_and_bilingual_title(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            collection = Path(temporary_directory)
            artist_directory = collection / "Achim Reichel"
            journey = artist_directory / "[1971] Die grune Reise"
            ar3 = artist_directory / "[1972] AR3"
            journey.mkdir(parents=True)
            ar3.mkdir()
            self.assertEqual(find_album(collection, "A.R. & Machines", "Die Grüne Reise - The Green Journey", None), journey)
            self.assertEqual(find_album(collection, "A.R.", "AR3", None), ar3)
            self.assertIsNone(find_album(collection, "A.R.", "Unrelated Album", None))

    def test_artist_folder_with_parenthetical_qualifier(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            collection = Path(temporary_directory)
            (collection / "Organisation" / "[1970] Other Album").mkdir(parents=True)
            album_directory = collection / "Organisation (Kraftwerk)" / "[1969] Tone Float"
            album_directory.mkdir(parents=True)
            (collection / "Organisation Extra (Other)" / "[1969] Unrelated").mkdir(parents=True)
            self.assertEqual(find_album(collection, "Organisation", "Tone Float", None), album_directory)
            self.assertIsNone(find_album(collection, "Organisation", "Unrelated", None))

    def test_alternative_band_name(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            collection = Path(temporary_directory)
            album_directory = collection / "The Cosmic Jokers" / "[1974] Galactic Supermarket"
            album_directory.mkdir(parents=True)
            self.assertEqual(find_album(collection, "Galactic Supermarket", "Galactic Supermarket", None), album_directory)
            self.assertIsNone(find_album(collection, "Galactic Supermarket", "Unrelated Album", None))

    def test_album_prefix_with_release_details(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            collection = Path(temporary_directory)
            album_directory = collection / "Dom" / "[1972 (2001 + Bonus Tracks)] Edge Of Time"
            album_directory.mkdir(parents=True)
            self.assertEqual(find_album(collection, "Dom", "Edge Of Time", None), album_directory)
            self.assertEqual(find_album(collection, "Dom", "Edge Of Time", "1972"), album_directory)
            self.assertIsNone(find_album(collection, "Dom", "Other Album", None))

    def test_album_prefix_without_year(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            collection = Path(temporary_directory)
            album_directory = collection / "Dom" / "[Bonus Tracks] Edge Of Time"
            album_directory.mkdir(parents=True)
            self.assertEqual(find_album(collection, "Dom", "Edge Of Time", None), album_directory)
            self.assertEqual(format_album_name(album_directory.name), "Edge Of Time")

    def test_multi_year_album_with_edition_suffix(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            collection = Path(temporary_directory)
            album_directory = collection / "Can" / "[1971,72] Tago Mago [40th Anniversary Edition]"
            album_directory.mkdir(parents=True)
            self.assertEqual(find_album(collection, "Can", "Tago Mago", None), album_directory)
            self.assertEqual(find_album(collection, "Can", "Tago Mago", "1971"), album_directory)
            self.assertIsNone(find_album(collection, "Can", "Unrelated Album", None))

    def test_john_coltrane_standard_match_ignores_catalog_credit(self) -> None:
        collection = Path("M:/")
        album = find_album(collection, "John Coltrane", "Both Directions at Once (IMPULSE!)", "2018")
        self.assertIsNotNone(album)
        self.assertEqual(album.name, "[2018] Both Directions at Once: The Lost Album")


if __name__ == "__main__":
    unittest.main()
