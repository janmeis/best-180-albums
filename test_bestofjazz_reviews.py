import unittest

from make_best_30_jazz_playlist import parse_albums


class BestOfJazzReviewsTests(unittest.TestCase):
    def test_parses_review_entry_title_and_year_from_url(self) -> None:
        html = """
        <header class="entry-header">
          <h2 class="entry-title"><a href="https://bestofjazz.org/satoko-fujii-hyaku-one-hundred-dreams/"
          title="Permalink to Satoko Fujii – Hyaku: One Hundred Dreams" rel="bookmark">
          Satoko Fujii – Hyaku: One Hundred Dreams</a></h2>
        </header>
        """

        heading, year, albums = parse_albums(
            html,
            "https://bestofjazz.org/category/reviews/2022-reviews/",
        )

        self.assertEqual(heading, "Best Jazz 2022 Reviews")
        self.assertEqual(year, "2022")
        self.assertEqual(albums, [("Satoko Fujii", "Hyaku: One Hundred Dreams")])


if __name__ == "__main__":
    unittest.main()