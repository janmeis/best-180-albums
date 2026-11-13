"""Tests for scrobble_most_played module."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from scrobble_most_played import (
    LastFmClient,
    ensure_config_file,
    generate_api_sig,
    load_state,
    main,
    parse_folder_artist_album,
    read_tracks_from_db,
    reset_state,
    save_state,
    scrobble_database,
)


class ScrobbleMostPlayedTests(unittest.TestCase):
    def test_parse_folder_artist_album(self) -> None:
        self.assertEqual(
            parse_folder_artist_album("Anteloper-2022-Pink Dolphins"),
            ("Anteloper", "Pink Dolphins", "2022"),
        )
        self.assertEqual(
            parse_folder_artist_album(
                "Andrew Cyrille - Mark Dresser - Marty Ehrlich -2000-C-D-E"
            ),
            ("Andrew Cyrille - Mark Dresser - Marty Ehrlich", "C-D-E", "2000"),
        )
        self.assertEqual(
            parse_folder_artist_album("SomeArtist-SomeAlbum"),
            ("SomeArtist", "SomeAlbum", None),
        )
        self.assertEqual(
            parse_folder_artist_album("JustAnArtist"),
            ("JustAnArtist", "", None),
        )

    def test_generate_api_sig(self) -> None:
        params = {
            "method": "auth.getSession",
            "api_key": "my_key",
            "token": "my_token",
            "format": "json",  # Must be ignored in signature
        }
        sig = generate_api_sig(params, "my_secret")
        self.assertEqual(sig, "08118e2b8606ccc4a74a133cf7ca9292")

    def _create_sample_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            c = conn.cursor()
            c.execute(
                "CREATE TABLE most_played ("
                "path TEXT, name TEXT, info_id INT, frequency INT, begin_time INT, end_time INT,"
                "PRIMARY KEY(path, name, info_id))"
            )
            rows = [
                ("a:\\Artist A-2021-Album One\\01-Track One.flac\x00", "Track One\x00", 4080, 2, 0, 180000),
                ("a:\\Artist B-2022-Album Two\\02-Track Two.flac\x00", "Track Two\x00", 4080, 1, 0, 210000),
                ("a:\\Artist B-2022-Album Two\\03-Track Three.flac\x00", "Track Three\x00", 4080, 3, 0, 240000),
            ]
            c.executemany("INSERT INTO most_played VALUES (?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
        finally:
            conn.close()

    def test_read_tracks_from_db(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            self._create_sample_db(db_path)
            tracks = read_tracks_from_db(db_path)

            self.assertEqual(len(tracks), 3)
            self.assertEqual(tracks[0].order_no, 1)
            self.assertEqual(tracks[0].title, "Track One")
            self.assertEqual(tracks[0].artist, "Artist A")
            self.assertEqual(tracks[0].album, "Album One")
            self.assertEqual(tracks[0].year, "2021")

            self.assertEqual(tracks[2].order_no, 3)
            self.assertEqual(tracks[2].title, "Track Three")
            self.assertEqual(tracks[2].artist, "Artist B")

    def test_scrobble_database_dry_run_and_state(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            state_path = Path(tmp_dir) / "state.json"
            self._create_sample_db(db_path)

            config = {
                "lastfm": {
                    "url": "https://ws.audioscrobbler.com/2.0/",
                    "username": "user",
                    "password": "pass",
                    "api_key": "k",
                    "api_secret": "s",
                },
                "scrobbler": {
                    "database": str(db_path),
                    "state_file": str(state_path),
                },
            }

            # Dry run does not write state to disk, but returns proper stats
            log_path = Path(tmp_dir) / "scrobble_log.txt"
            uploaded, last_no, last_title = scrobble_database(
                db_path, config, state_path, log_path=log_path, dry_run=True
            )
            self.assertEqual(uploaded, 3)
            self.assertEqual(last_no, 3)
            self.assertEqual(last_title, "Track Three")
            self.assertFalse(state_path.exists())
            self.assertTrue(log_path.exists())
            self.assertIn("Track One", log_path.read_text(encoding="utf-8"))

    def test_scrobble_database_incremental_and_reset(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            state_path = Path(tmp_dir) / "state.json"
            self._create_sample_db(db_path)

            config = {
                "lastfm": {
                    "url": "https://ws.audioscrobbler.com/2.0/",
                    "username": "user",
                    "password": "pass",
                    "api_key": "k",
                    "api_secret": "s",
                    "session_key": "test_session",
                },
                "scrobbler": {
                    "database": str(db_path),
                    "state_file": str(state_path),
                },
            }

            # Mock LastFmClient batch scrobbling
            with patch("scrobble_most_played.LastFmClient.scrobble_batch", return_value=3) as mock_batch:
                uploaded, last_no, last_title = scrobble_database(
                    db_path, config, state_path, dry_run=False
                )
                self.assertEqual(uploaded, 3)
                self.assertEqual(last_no, 3)
                self.assertEqual(last_title, "Track Three")
                self.assertTrue(state_path.exists())
                self.assertEqual(mock_batch.call_count, 1)

            # Second run without reset should scrobble 0 items
            uploaded2, last_no2, last_title2 = scrobble_database(
                db_path, config, state_path, dry_run=False
            )
            self.assertEqual(uploaded2, 0)
            self.assertEqual(last_no2, 3)
            self.assertEqual(last_title2, "Track Three")

            # Run with reset should upload all 3 items again
            with patch("scrobble_most_played.LastFmClient.scrobble_batch", return_value=3) as mock_batch2:
                uploaded3, last_no3, last_title3 = scrobble_database(
                    db_path, config, state_path, reset=True, dry_run=False
                )
                self.assertEqual(uploaded3, 3)
                self.assertEqual(last_no3, 3)
                self.assertEqual(last_title3, "Track Three")
                self.assertEqual(mock_batch2.call_count, 1)

    def test_scrobble_log_appends_without_deleting_previous(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            state_path = Path(tmp_dir) / "state.json"
            log_path = Path(tmp_dir) / "scrobble_log.txt"
            self._create_sample_db(db_path)

            config = {
                "lastfm": {
                    "url": "https://ws.audioscrobbler.com/2.0/",
                    "username": "user",
                    "password": "pass",
                    "api_key": "k",
                    "api_secret": "s",
                    "session_key": "test_session",
                },
                "scrobbler": {
                    "database": str(db_path),
                    "state_file": str(state_path),
                },
            }

            # First run: scrobble 3 items and write initial log
            with patch("scrobble_most_played.LastFmClient.scrobble_batch", return_value=3):
                uploaded, last_no, _ = scrobble_database(
                    db_path, config, state_path, log_path=log_path, dry_run=False
                )
            self.assertEqual(uploaded, 3)
            content_batch1 = log_path.read_text(encoding="utf-8")
            self.assertIn("Track One", content_batch1)
            self.assertIn("Track Three", content_batch1)
            self.assertNotIn("Track Four", content_batch1)

            # Add a 4th track to the database
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO most_played VALUES ('a:\\\\Artist C-2023-Album Three\\\\04-Track Four.flac', 'Track Four', 4080, 1, 0, 190000)"
            )
            conn.commit()
            conn.close()

            # Second run: incremental upload of 1 new item
            with patch("scrobble_most_played.LastFmClient.scrobble_batch", return_value=1):
                uploaded2, last_no2, last_title2 = scrobble_database(
                    db_path, config, state_path, log_path=log_path, dry_run=False
                )
            self.assertEqual(uploaded2, 1)
            self.assertEqual(last_no2, 4)
            self.assertEqual(last_title2, "Track Four")

            # Verify that previous tracks are still present and track 4 was appended with a new header section
            content_batch2 = log_path.read_text(encoding="utf-8")
            self.assertIn("Track One", content_batch2)
            self.assertIn("Track Two", content_batch2)
            self.assertIn("Track Three", content_batch2)
            self.assertIn("Track Four", content_batch2)
            self.assertEqual(content_batch2.count("# HiBy R3 Pro - Scrobble Log"), 2)
            self.assertIn("# HiBy R3 Pro - Scrobble Log (3 tracks)", content_batch2)
            self.assertIn("# HiBy R3 Pro - Scrobble Log (1 tracks)", content_batch2)
            table_lines = [line for line in content_batch2.splitlines() if line.startswith("|")]
            # 2 table headers + 2 separators + 4 track rows = 8 lines
            self.assertEqual(len(table_lines), 8)
            self.assertTrue(table_lines[-1].startswith("| 4 |"))

    def test_ensure_config_file_creates_and_reads_json(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            cfg_path = Path(tmp_dir) / "scrobble_most_played.json"
            cfg = ensure_config_file(cfg_path)
            self.assertTrue(cfg_path.is_file())
            self.assertIn("lastfm", cfg)
            self.assertIn("scrobbler", cfg)

            cfg["lastfm"]["username"] = "my_user"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            reloaded = ensure_config_file(cfg_path)
            self.assertEqual(reloaded["lastfm"]["username"], "my_user")

    def test_lastfm_client_authentication(self) -> None:
        client = LastFmClient(
            url="https://ws.audioscrobbler.com/2.0/",
            api_key="test_key",
            api_secret="test_secret",
            username="test_user",
            password="test_password",
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "session": {"name": "test_user", "key": "mocked_session_key"}
        }

        with patch("requests.post", return_value=mock_resp) as mock_post:
            sk = client.authenticate()
            self.assertEqual(sk, "mocked_session_key")
            self.assertEqual(client.session_key, "mocked_session_key")

            # Verify parameters posted
            args, kwargs = mock_post.call_args
            data = kwargs.get("data", {})
            self.assertEqual(data.get("method"), "auth.getMobileSession")
            self.assertEqual(data.get("username"), "test_user")
            self.assertEqual(data.get("password"), "test_password")
            self.assertIn("api_sig", data)

    def test_scrobble_batch_sends_duration_and_timestamp(self) -> None:
        from scrobble_most_played import TrackItem

        client = LastFmClient(
            url="https://ws.audioscrobbler.com/2.0/",
            api_key="test_key",
            api_secret="test_secret",
            session_key="mocked_session_key",
        )

        track = TrackItem(
            order_no=1,
            title="Track One",
            artist="Artist A",
            album="Album One",
            year="2021",
            path="a:\\test.flac",
            info_id=4080,
            duration_ms=185000,
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"scrobbles": {"@attr": {"accepted": 1}}}

        with patch("requests.post", return_value=mock_resp) as mock_post:
            count = client.scrobble_batch([(track, 1700000000)])
            self.assertEqual(count, 1)

            args, kwargs = mock_post.call_args
            data = kwargs.get("data", {})
            self.assertEqual(data.get("method"), "track.scrobble")
            self.assertEqual(data.get("artist[0]"), "Artist A")
            self.assertEqual(data.get("track[0]"), "Track One")
            self.assertEqual(data.get("album[0]"), "Album One")
            self.assertEqual(data.get("timestamp[0]"), "1700000000")
            self.assertEqual(data.get("duration[0]"), "185")  # 185000 ms = 185 s

    def test_reset_state_clears_order_and_title(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "state.json"
            save_state(
                state_path,
                {"last_order_no": 491, "last_title": "Track 491", "session_key": "my_session"},
            )
            reset_state(state_path)
            loaded = load_state(state_path)
            self.assertEqual(loaded["last_order_no"], 0)
            self.assertEqual(loaded["last_title"], "")
            self.assertEqual(loaded["session_key"], "my_session")

    def test_cli_reset_does_not_require_db(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            cfg_path = Path(tmp_dir) / "scrobble_most_played.json"
            state_path = Path(tmp_dir) / "state.json"
            non_existent_db = Path(tmp_dir) / "does_not_exist.db"

            cfg_data = {
                "lastfm": {},
                "scrobbler": {
                    "database": str(non_existent_db),
                    "state_file": str(state_path),
                },
            }
            cfg_path.write_text(json.dumps(cfg_data), encoding="utf-8")
            save_state(
                state_path,
                {"last_order_no": 100, "last_title": "Track 100", "session_key": "abc"},
            )

            with patch("sys.argv", ["scrobble_most_played.py", "--config", str(cfg_path), "--reset"]):
                exit_code = main()
                self.assertEqual(exit_code, 0)

            loaded = load_state(state_path)
            self.assertEqual(loaded["last_order_no"], 0)
            self.assertEqual(loaded["last_title"], "")
            self.assertEqual(loaded["session_key"], "abc")


if __name__ == "__main__":
    unittest.main()
