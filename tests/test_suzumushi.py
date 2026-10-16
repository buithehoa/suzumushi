import subprocess
import json
import tempfile
import threading
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import patch

import suzumushi as app


def payload(track_id="track-a", title="A", duration=180_000, progress=0, item_type="track"):
    return {
        "is_playing": True,
        "progress_ms": progress,
        "item": {
            "id": track_id,
            "uri": f"spotify:track:{track_id}" if track_id else None,
            "type": item_type,
            "name": title,
            "duration_ms": duration,
            "artists": [{"name": "Artist"}],
            "album": {"name": "Album", "images": []},
        },
    }


class FakeSpotify:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def current_playback(self):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class StopAfterWaits:
    def __init__(self, count):
        self.count = count
        self.waits = []

    def is_set(self):
        return False

    def wait(self, seconds):
        self.waits.append(seconds)
        return len(self.waits) >= self.count


class SuzumushiTests(unittest.TestCase):
    def test_load_configuration_uses_xdg_cache_home(self):
        env = {
            app.CLIENT_ID_ENV: "client",
            app.CLIENT_SECRET_ENV: "secret",
            app.REDIRECT_URI_ENV: app.DEFAULT_REDIRECT_URI,
            "XDG_CACHE_HOME": "/tmp/test-cache",
        }
        config = app.load_configuration(env)
        self.assertEqual(config.token_path, Path("/tmp/test-cache/suzumushi/token.json"))
        self.assertEqual(config.art_path, Path("/tmp/test-cache/suzumushi/cover"))

    def test_configuration_requires_credentials_and_local_redirect(self):
        with self.assertRaises(app.ConfigurationError):
            app.load_configuration({})
        env = {
            app.CLIENT_ID_ENV: "client",
            app.CLIENT_SECRET_ENV: "secret",
            app.REDIRECT_URI_ENV: "https://example.com/callback",
        }
        with self.assertRaises(app.ConfigurationError):
            app.load_configuration(env)
        env[app.REDIRECT_URI_ENV] = "http://127.0.0.1:not-a-port"
        with self.assertRaises(app.ConfigurationError):
            app.load_configuration(env)

    def test_noninteractive_oauth_refuses_terminal_fallback(self):
        class OAuthBase:
            def __init__(self, **_kwargs):
                pass

            def get_auth_response(self, open_browser=None):
                return input("should not prompt")

        oauth = app.NonInteractiveSpotifyOAuth.build(OAuthBase)
        with self.assertRaises(app.AuthenticationError):
            oauth.get_auth_response()

    def test_normalize_music_identity_and_cover_selection(self):
        item = payload()
        item["item"]["album"]["images"] = [
            {"url": "https://img.example/large", "width": 640},
            {"url": "https://img.example/small", "width": 300},
        ]
        playback = app.normalize_playback(item, 100.0)
        self.assertEqual(playback.kind, "music")
        self.assertEqual(playback.identity, ("id", "track-a"))
        self.assertEqual(playback.cover_url, "https://img.example/small")
        self.assertEqual(playback.received_at_monotonic, 100.0)

    def test_identity_falls_back_to_uri_then_complete_metadata(self):
        by_uri = payload(track_id=None)
        by_uri["item"]["uri"] = "spotify:track:local"
        self.assertEqual(app.normalize_playback(by_uri).identity, ("uri", "spotify:track:local"))
        by_metadata = payload(track_id=None)
        by_metadata["item"]["uri"] = None
        self.assertEqual(app.normalize_playback(by_metadata).identity[0], "metadata")
        by_metadata["item"]["name"] = ""
        self.assertEqual(app.normalize_playback(by_metadata).kind, "unsupported")

    def test_normalize_idle_unsupported_and_malformed(self):
        self.assertEqual(app.normalize_playback(None).kind, "idle")
        self.assertEqual(app.normalize_playback({"is_playing": False}).kind, "idle")
        self.assertEqual(app.normalize_playback(payload(item_type="episode")).kind, "unsupported")
        with self.assertRaises(app.MalformedPlayback):
            app.normalize_playback({"is_playing": "yes"})

    def test_active_wait_uses_deadline_and_minimum(self):
        playback = app.normalize_playback(payload(), 100.0)
        self.assertEqual(app.choose_delay_seconds(playback, now_monotonic=100.0), 180.0)
        self.assertEqual(app.choose_delay_seconds(playback, now_monotonic=110.0), 170.0)
        short = app.normalize_playback(payload(duration=1_000, progress=0), 100.0)
        self.assertEqual(app.choose_delay_seconds(short, now_monotonic=102.0), app.MIN_POLL_SECONDS)

    def test_idle_unsupported_and_invalid_timing_waits(self):
        idle = app.normalize_playback(None, 0.0)
        self.assertEqual(app.choose_delay_seconds(idle), app.IDLE_POLL_SECONDS)
        no_timing = app.normalize_playback(payload(duration=None, progress=None), 0.0)
        self.assertEqual(app.choose_delay_seconds(no_timing), app.INVALID_TIMING_POLL_SECONDS)
        bad_timing = app.normalize_playback(payload(duration=1_000, progress=2_000), 0.0)
        self.assertEqual(app.choose_delay_seconds(bad_timing), app.INVALID_TIMING_POLL_SECONDS)

    @patch.object(app, "notification_server_identity", return_value=("bus", ":1.2", ":1.3"))
    def test_notification_passes_literal_metadata_without_a_shell(self, identity):
        data = payload(title='Lucifer\'s "$(touch nope) & <title>')
        data["item"]["artists"][0]["name"] = "Artist's & Co"
        data["item"]["album"]["name"] = "Album's <Edition>"
        playback = app.normalize_playback(data)
        with tempfile.TemporaryDirectory() as temp_dir:
            config = app.Config("id", "secret", app.DEFAULT_REDIRECT_URI, Path(temp_dir),
                                Path(temp_dir) / "token.json", Path(temp_dir) / "cover")
            with patch.object(app, "download_artwork", return_value=None), patch.object(app.subprocess, "run") as run:
                run.return_value.stdout = "42\n"
                app.dispatch_notification(playback, config, object(), OrderedDict())
        args, kwargs = run.call_args
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(args[0][args[0].index("--app-name") + 1], "Suzumushi")
        self.assertEqual(args[0][-2], "Artist's &amp; Co · Album's &lt;Edition&gt; (Unknown year)")
        self.assertEqual(args[0][-1], '<b>Lucifer\'s "$(touch nope) &amp; &lt;title&gt;</b>')
        self.assertTrue(kwargs["check"])
        self.assertEqual(kwargs["timeout"], app.NOTIFY_TIMEOUT_SECONDS)

    def test_release_year_accepts_spotify_date_precisions_and_missing_metadata(self):
        for release_date, expected in [("2011", "2011"), ("2011-03", "2011"),
                                       ("2011-03-15", "2011"), (None, app.UNKNOWN_YEAR),
                                       ("bad", app.UNKNOWN_YEAR), (2011, app.UNKNOWN_YEAR)]:
            with self.subTest(release_date=release_date):
                data = payload()
                data["item"]["album"]["release_date"] = release_date
                self.assertEqual(app.normalize_playback(data).release_year, expected)

    @patch.object(app, "notification_server_identity", return_value=("bus", ":1.2", ":1.3"))
    def test_album_art_uses_image_hint_and_keeps_application_icon(self, identity):
        playback = app.normalize_playback(payload())
        with tempfile.TemporaryDirectory() as temp_dir:
            config = app.Config("id", "secret", app.DEFAULT_REDIRECT_URI, Path(temp_dir),
                                Path(temp_dir) / "token.json", Path(temp_dir) / "cover")
            for artwork in (str(Path(temp_dir) / "cover image.png"), None):
                with self.subTest(artwork=artwork), patch.object(app, "download_artwork", return_value=artwork), patch.object(app, "resolve_notification_icon", return_value="/icons/spotify.png"), patch.object(app.subprocess, "run") as run:
                    run.return_value.stdout = "42\n"
                    app.dispatch_notification(playback, config, object(), OrderedDict())
                    command = run.call_args.args[0]
                    self.assertEqual(command[command.index("--icon") + 1], "/icons/spotify.png")
                    if artwork:
                        self.assertEqual(command[command.index("--hint") + 1],
                                         f"string:image-path:{Path(artwork).as_uri()}")
                    else:
                        self.assertNotIn("--hint", command)

    def notification_config(self, directory):
        root = Path(directory)
        return app.Config("id", "secret", app.DEFAULT_REDIRECT_URI, root,
                          root / "token.json", root / "cover")

    def test_notification_replaces_saved_entry_after_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.notification_config(directory)
            with patch.object(app, "notification_server_identity", return_value=("bus", ":1.2", ":1.3")), patch.object(app, "download_artwork", return_value=None), patch.object(app, "resolve_notification_icon", return_value="spotify"), patch.object(app.subprocess, "run") as run:
                run.return_value.stdout = "42\n"
                app.dispatch_notification(app.normalize_playback(payload()), config, object(), OrderedDict())
                self.assertIn("--print-id", run.call_args.args[0])
                self.assertNotIn("--replace-id", run.call_args.args[0])
                # No in-memory state is passed: the second call must load it from disk.
                app.dispatch_notification(app.normalize_playback(payload("b", "B")), config, object(), OrderedDict())
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("--replace-id") + 1], "42")
                # A dismissed entry is assigned a new ID by the server.
                run.return_value.stdout = "43\n"
                app.dispatch_notification(app.normalize_playback(payload("c", "C")), config, object(), OrderedDict())
            self.assertEqual(app.load_notification_state(config), app.NotificationState(("bus", ":1.2", ":1.3"), 43))
            state_path = config.cache_dir / app.NOTIFICATION_STATE_FILENAME
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.cache_dir.stat().st_mode & 0o777, 0o700)
            self.assertFalse(list(config.cache_dir.glob(".notification-*")))

    def test_notification_does_not_reuse_id_from_another_bus_or_server(self):
        for identity in (("other-bus", ":1.2", ":1.3"), ("bus", ":1.4", ":1.3"), ("bus", ":1.2", ":1.5"), None):
            with self.subTest(identity=identity), tempfile.TemporaryDirectory() as directory:
                config = self.notification_config(directory)
                state = app.NotificationState(("bus", ":1.2", ":1.3"), 42)
                with patch.object(app, "notification_server_identity", return_value=identity), patch.object(app, "download_artwork", return_value=None), patch.object(app, "resolve_notification_icon", return_value="spotify"), patch.object(app.subprocess, "run") as run:
                    run.return_value.stdout = "43\n"
                    app.dispatch_notification(app.normalize_playback(payload()), config, object(), OrderedDict(), state)
                    self.assertNotIn("--replace-id", run.call_args.args[0])
                expected = app.NotificationState(identity, 43) if identity else app.NotificationState()
                self.assertEqual(app.load_notification_state(config), expected)

    def test_notification_failure_preserves_previous_replacement_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.notification_config(directory)
            state = app.NotificationState(("bus", ":1.2", ":1.3"), 42)
            app.save_notification_state(config, state)
            with patch.object(app, "notification_server_identity", return_value=state.server_identity), patch.object(app, "download_artwork", return_value=None), patch.object(app, "resolve_notification_icon", return_value="spotify"), patch.object(app.subprocess, "run", side_effect=subprocess.TimeoutExpired("notify-send", 5)):
                with self.assertRaises(subprocess.TimeoutExpired):
                    app.dispatch_notification(app.normalize_playback(payload()), config, object(), OrderedDict(), state)
            self.assertEqual(state, app.NotificationState(("bus", ":1.2", ":1.3"), 42))
            self.assertEqual(app.load_notification_state(config), state)

    def test_invalid_returned_id_does_not_repeat_successful_track(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.notification_config(directory)
            app.save_notification_state(config, app.NotificationState(("bus", ":1.2", ":1.3"), 42))
            with patch.object(app, "notification_server_identity", return_value=("bus", ":1.2", ":1.3")), patch.object(app, "download_artwork", return_value=None), patch.object(app, "resolve_notification_icon", return_value="spotify"), patch.object(app.subprocess, "run") as run:
                run.return_value.stdout = "not an ID\n"
                app.run_daemon(config, FakeSpotify([payload(), payload()]), object(), StopAfterWaits(2))
                self.assertEqual(run.call_count, 1)
            self.assertEqual(app.load_notification_state(config), app.NotificationState())

    def test_notification_state_rejects_corrupt_or_invalid_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.notification_config(directory)
            path = config.cache_dir / app.NOTIFICATION_STATE_FILENAME
            for contents in ("broken", "[]", json.dumps({"bus_id": "bus", "server_owner": ":1.2", "notification_id": True}), json.dumps({"bus_id": "bus", "server_owner": ":1.2", "notification_id": 0})):
                with self.subTest(contents=contents):
                    path.write_text(contents)
                    self.assertEqual(app.load_notification_state(config), app.NotificationState())

    def test_server_change_during_submission_discards_saved_id(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.notification_config(directory)
            state = app.NotificationState(("bus", ":1.2", ":1.3"), 42)
            app.save_notification_state(config, state)
            with patch.object(app, "notification_server_identity", side_effect=[state.server_identity, ("bus", ":1.2", ":1.5")]), patch.object(app, "download_artwork", return_value=None), patch.object(app, "resolve_notification_icon", return_value="spotify"), patch.object(app.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "43\n")):
                app.dispatch_notification(app.normalize_playback(payload()), config, object(), OrderedDict(), state)
            self.assertEqual(state, app.NotificationState())
            self.assertFalse((config.cache_dir / app.NOTIFICATION_STATE_FILENAME).exists())

    def test_cache_write_failure_does_not_repeat_successful_track(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.notification_config(directory)
            with patch.object(app, "notification_server_identity", return_value=("bus", ":1.2", ":1.3")), patch.object(app, "download_artwork", return_value=None), patch.object(app, "resolve_notification_icon", return_value="spotify"), patch.object(app.os, "replace", side_effect=OSError("write failed")), patch.object(app.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "42\n")) as run:
                app.run_daemon(config, FakeSpotify([payload(), payload()]), object(), StopAfterWaits(2))
                self.assertEqual(run.call_count, 1)
            self.assertFalse(list(config.cache_dir.glob(".notification-*")))

    def test_notification_server_identity_parses_gdbus_and_handles_failure(self):
        with patch.object(app.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, "('bus',)\n"), subprocess.CompletedProcess([], 0, "(':1.2',)\n"), subprocess.CompletedProcess([], 0, "(':1.3',)\n")]):
            self.assertEqual(app.notification_server_identity(), ("bus", ":1.2", ":1.3"))
        for output in ("garbage", "('bus', 'extra')", "(42,)"):
            with self.subTest(output=output), patch.object(app.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output)):
                self.assertIsNone(app.notification_server_identity())

    def test_application_icon_resolves_from_another_installed_theme(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            icon = Path(temp_dir) / "other-theme/apps/240/spotify.png"
            icon.parent.mkdir(parents=True)
            icon.touch()
            with patch.object(app, "NOTIFICATION_ICON", "spotify"), patch.object(app, "NOTIFICATION_ICON_SEARCH_DIRS", (temp_dir,)):
                self.assertEqual(app.resolve_notification_icon(), str(icon))
            with patch.object(app, "NOTIFICATION_ICON", str(icon)):
                self.assertEqual(app.resolve_notification_icon(), str(icon))

    def test_missing_application_icon_retains_theme_name_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(app, "NOTIFICATION_ICON", "spotify"), patch.object(app, "NOTIFICATION_ICON_SEARCH_DIRS", (temp_dir,)):
                self.assertEqual(app.resolve_notification_icon(), "spotify")

    def test_failed_notification_retries_track_only_if_still_current(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = app.Config("id", "secret", app.DEFAULT_REDIRECT_URI, Path(temp_dir),
                                Path(temp_dir) / "token.json", Path(temp_dir) / "cover")
            stop = StopAfterWaits(2)
            spotify = FakeSpotify([payload(), payload()])
            with patch.object(app, "dispatch_notification", side_effect=[subprocess.CalledProcessError(1, "notify-send"), None]) as dispatch:
                app.run_daemon(config, spotify, object(), stop)
            self.assertEqual(dispatch.call_count, 2)
            self.assertEqual(spotify.calls, 2)

    def test_successful_notification_deduplicates_same_track(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = app.Config("id", "secret", app.DEFAULT_REDIRECT_URI, Path(temp_dir),
                                Path(temp_dir) / "token.json", Path(temp_dir) / "cover")
            stop = StopAfterWaits(2)
            spotify = FakeSpotify([payload(), payload()])
            with patch.object(app, "dispatch_notification") as dispatch:
                app.run_daemon(config, spotify, object(), stop)
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(spotify.calls, 2)
            self.assertAlmostEqual(stop.waits[0], 180.0, delta=0.1)

    def test_failed_track_is_not_queued_after_playback_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = app.Config("id", "secret", app.DEFAULT_REDIRECT_URI, Path(temp_dir),
                                Path(temp_dir) / "token.json", Path(temp_dir) / "cover")
            stop = StopAfterWaits(2)
            spotify = FakeSpotify([payload(), payload("track-b", "B")])
            with patch.object(app, "dispatch_notification", side_effect=[OSError("failure"), None]) as dispatch:
                app.run_daemon(config, spotify, object(), stop)
            submitted = [call.args[0].title for call in dispatch.call_args_list]
            self.assertEqual(submitted, ["A", "B"])

    def test_malformed_successful_response_uses_malformed_retry_wait(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = app.Config("id", "secret", app.DEFAULT_REDIRECT_URI, Path(temp_dir),
                                Path(temp_dir) / "token.json", Path(temp_dir) / "cover")
            stop = StopAfterWaits(1)
            spotify = FakeSpotify([{"is_playing": "unexpected"}])
            app.run_daemon(config, spotify, object(), stop)
            self.assertEqual(spotify.calls, 1)
            self.assertEqual(stop.waits, [app.MALFORMED_RESPONSE_RETRY_SECONDS])

    def test_transient_api_failures_use_exponential_backoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = app.Config("id", "secret", app.DEFAULT_REDIRECT_URI, Path(temp_dir),
                                Path(temp_dir) / "token.json", Path(temp_dir) / "cover")
            stop = StopAfterWaits(3)
            spotify = FakeSpotify([app.ApiFailure(None), app.ApiFailure(None), payload()])
            with patch.object(app, "dispatch_notification"):
                app.run_daemon(config, spotify, object(), stop)
            self.assertEqual(stop.waits[:2], [5, 10])
            self.assertEqual(spotify.calls, 3)

    def test_rate_limit_uses_retry_after_or_named_fallback(self):
        self.assertEqual(app._retry_after_seconds({"Retry-After": "17"}), 17.0)
        self.assertEqual(app._retry_after_seconds({"Retry-After": "invalid"}), app.RATE_LIMIT_FALLBACK_SECONDS)

    def test_401_refreshes_once_before_retrying_request(self):
        class Cache:
            def get_cached_token(self):
                return {"refresh_token": "refresh"}

        class OAuth:
            cache_handler = Cache()

            def refresh_access_token(self, refresh_token):
                self.asserted_refresh_token = refresh_token

        with tempfile.TemporaryDirectory() as temp_dir:
            config = app.Config("id", "secret", app.DEFAULT_REDIRECT_URI, Path(temp_dir),
                                Path(temp_dir) / "token.json", Path(temp_dir) / "cover")
            oauth = OAuth()
            spotify = FakeSpotify([app.ApiFailure(401), None])
            stop = StopAfterWaits(1)
            app.run_daemon(config, spotify, oauth, stop)
            self.assertEqual(spotify.calls, 2)
            self.assertEqual(oauth.asserted_refresh_token, "refresh")


if __name__ == "__main__":
    unittest.main()
