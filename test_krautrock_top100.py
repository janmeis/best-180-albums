import unittest
from pathlib import Path
from unittest.mock import patch

from check_krautrock_top100 import find_missing, parse_items


class KrautrockTop100Tests(unittest.TestCase):
    def test_searches_secondary_library(self) -> None:
        def find_album(collection, artist, album, year):
            return collection / "found" if collection == Path("secondary") and album == "Free Improvisation" else None

        with patch("check_krautrock_top100.find_album", side_effect=find_album):
            missing = find_missing([Path("primary"), Path("secondary")], [
                ("96", "New Phonic Art 1973 / Iskra 1903 / Wired", "Free Improvisation"),
                ("97", "Someone", "Missing Album"),
            ])
        self.assertEqual(missing, ["97\tSomeone\tMissing Album\t(album not found)"])

    def test_et_cetera_override_is_album_specific(self) -> None:
        with patch("check_krautrock_top100.find_album", return_value=Path("found")) as lookup:
            missing = find_missing([Path("library")], [
                ("34", "Et Cetera", "Et Cetera"),
                ("35", "Et Cetera", "Other Album"),
            ])
        self.assertEqual(missing, [])
        self.assertEqual(lookup.call_args_list[0].args, (Path("library"), "Wolfgang Dauner", "Et Cetera", None))
        self.assertEqual(lookup.call_args_list[1].args, (Path("library"), "Et Cetera", "Other Album", None))

    def test_parse_items_preserves_titles_and_removes_discogs_artist_suffixes(self) -> None:
        items = [{"display_title": "Alcatraz (4) - Vampire State Building"}] * 100
        items[1] = {"display_title": "Agitation Free - معليش = Malesch"}
        items[2] = {"display_title": "Konrad Schnitzler* - Rot"}
        albums = parse_items({"items": items})
        self.assertEqual(albums[0], ("1", "Alcatraz", "Vampire State Building"))
        self.assertEqual(albums[1], ("2", "Agitation Free", "معليش = Malesch"))
        self.assertEqual(albums[2], ("3", "Konrad Schnitzler", "Rot"))

    def test_rejects_incomplete_list(self) -> None:
        with self.assertRaises(ValueError):
            parse_items({"items": []})

    def test_alternate_title_and_missing_report(self) -> None:
        def find_album(collection, artist, album, year):
            return Path("found") if album == "Malesch" else None

        with patch("check_krautrock_top100.find_album", side_effect=find_album):
            missing = find_missing([Path("library")], [
                ("1", "Agitation Free", "معليش = Malesch"),
                ("2", "Can", "Missing Album"),
            ])
        self.assertEqual(missing, ["2\tCan\tMissing Album\t(album not found)"])


if __name__ == "__main__":
    unittest.main()