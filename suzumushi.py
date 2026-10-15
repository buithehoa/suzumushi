#!/usr/bin/env python3
"""Notify the desktop when Spotify starts playing a different music track."""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import html
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit


# Configuration. Durations are seconds unless the name says otherwise.
IDLE_POLL_SECONDS = 120
MIN_POLL_SECONDS = 1
INVALID_TIMING_POLL_SECONDS = 120
MALFORMED_RESPONSE_RETRY_SECONDS = 120
RETRY_INITIAL_SECONDS = 5
RETRY_MULTIPLIER = 2
RETRY_MAX_SECONDS = 120
RATE_LIMIT_FALLBACK_SECONDS = 120
AUTH_REFRESH_ATTEMPTS = 1
API_TIMEOUT_SECONDS = 10
AUTH_TIMEOUT_SECONDS = 10
ART_TIMEOUT_SECONDS = 4
NOTIFY_TIMEOUT_SECONDS = 5
SDK_RETRIES = 0
SDK_STATUS_RETRIES = 0
ART_MAX_BYTES = 5 * 1024 * 1024
ART_DOWNLOAD_CHUNK_BYTES = 64 * 1024
ART_PREFERRED_WIDTH_PX = 300
ART_CACHE_MAX_ENTRIES = 1
NOTIFICATION_APP_NAME = "Suzumushi"
NOTIFICATION_ICON = "spotify"
NOTIFICATION_ICON_SEARCH_DIRS = (
    "~/.local/share/icons", "~/.icons", "/usr/local/share/icons",
    "/usr/share/icons", "/usr/share/pixmaps",
)
NOTIFICATION_ICON_EXTENSIONS = ("png", "svg", "xpm")
NOTIFICATION_HEADER_TEMPLATE = "{artists} · {album} ({year})"
NOTIFICATION_BODY_TEMPLATE = "<b>{title}</b>"
UNKNOWN_YEAR = "Unknown year"
UNKNOWN_TITLE = "Unknown Title"
UNKNOWN_ARTIST = "Unknown Artist"
UNKNOWN_ALBUM = "Unknown Album"
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
OAUTH_SCOPE = "user-read-playback-state"
CLIENT_ID_ENV = "SPOTIPY_CLIENT_ID"
CLIENT_SECRET_ENV = "SPOTIPY_CLIENT_SECRET"
REDIRECT_URI_ENV = "SPOTIPY_REDIRECT_URI"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
CACHE_APP_DIR = "suzumushi"
TOKEN_FILENAME = "token.json"
ART_FILENAME = "cover"
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
EXIT_CONFIG_ERROR = 2
EXIT_AUTH_ERROR = 3

LOGGER = logging.getLogger("suzumushi")
_STOP_EVENT = threading.Event()


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    redirect_uri: str
    cache_dir: Path
    token_path: Path
    art_path: Path


@dataclass(frozen=True)
class Playback:
    kind: str
    identity: tuple[Any, ...] | None = None
    title: str = UNKNOWN_TITLE
    artists: str = UNKNOWN_ARTIST
    album: str = UNKNOWN_ALBUM
    release_year: str = UNKNOWN_YEAR
    cover_url: str | None = None
    duration_ms: int | None = None
    progress_ms: int | None = None
    received_at_monotonic: float = 0.0


class ConfigurationError(Exception):
    """A permanent local configuration or dependency error."""


class AuthenticationError(Exception):
    """A permanent Spotify authorization error."""


class MalformedPlayback(Exception):
    """Spotify returned a successful response in an unexpected shape."""


class ApiFailure(Exception):
    def __init__(self, status: int | None, headers: Mapping[str, str] | None = None):
        super().__init__(f"Spotify API request failed (status {status or 'network'}).")
        self.status = status
        self.headers = headers or {}


class AuthRejected(Exception):
    """Authorization remained rejected after the configured refresh attempt."""


class NonInteractiveSpotifyOAuth:
    """Factory wrapper that disables Spotipy's terminal/browser auth fallback."""

    @staticmethod
    def build(spotify_oauth_class: Any, **kwargs: Any) -> Any:
        class Manager(spotify_oauth_class):
            def get_auth_response(self, open_browser: bool | None = None) -> str:
                raise AuthenticationError("Spotify authorization is not cached. Run --auth in a terminal.")

            def _get_auth_response_interactive(self, open_browser: bool = False) -> str:
                raise AuthenticationError("Spotify authorization is not cached. Run --auth in a terminal.")

        return Manager(**kwargs)


def _cache_root(env: Mapping[str, str]) -> Path:
    xdg_cache_home = env.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser() / CACHE_APP_DIR
    return Path.home() / ".cache" / CACHE_APP_DIR


def load_configuration(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    missing = [name for name in (CLIENT_ID_ENV, CLIENT_SECRET_ENV, REDIRECT_URI_ENV)
               if not env.get(name, "").strip()]
    if missing:
        raise ConfigurationError("Set the required environment variables: " + ", ".join(missing))

    redirect_uri = env[REDIRECT_URI_ENV].strip()
    try:
        parsed = urlsplit(redirect_uri)
        hostname, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ConfigurationError(f"{REDIRECT_URI_ENV} is not a valid local redirect URI.") from exc
    if (parsed.scheme != "http" or hostname not in {"127.0.0.1", "localhost"}
            or port is None or parsed.query or parsed.fragment):
        raise ConfigurationError(
            f"{REDIRECT_URI_ENV} must be a registered local HTTP redirect URI "
            f"(recommended: {DEFAULT_REDIRECT_URI})."
        )

    cache_dir = _cache_root(env)
    return Config(
        client_id=env[CLIENT_ID_ENV].strip(),
        client_secret=env[CLIENT_SECRET_ENV].strip(),
        redirect_uri=redirect_uri,
        cache_dir=cache_dir,
        token_path=cache_dir / TOKEN_FILENAME,
        art_path=cache_dir / ART_FILENAME,
    )


def ensure_private_cache(config: Config) -> None:
    config.cache_dir.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    config.cache_dir.chmod(PRIVATE_DIR_MODE)
    if config.token_path.exists():
        config.token_path.chmod(PRIVATE_FILE_MODE)


def clear_stale_artwork(config: Config) -> None:
    for cached_art in config.cache_dir.glob(f"{config.art_path.name}-*.img"):
        try:
            cached_art.unlink()
        except OSError:
            LOGGER.warning("Could not clear artwork left by a previous daemon run.")


def create_oauth(config: Config) -> Any:
    try:
        from spotipy.oauth2 import SpotifyOAuth
    except ImportError as exc:
        raise ConfigurationError("Install the dependencies from requirements.txt.") from exc

    ensure_private_cache(config)
    return NonInteractiveSpotifyOAuth.build(
        SpotifyOAuth,
        client_id=config.client_id,
        client_secret=config.client_secret,
        redirect_uri=config.redirect_uri,
        scope=OAUTH_SCOPE,
        cache_path=str(config.token_path),
        open_browser=False,
        requests_timeout=AUTH_TIMEOUT_SECONDS,
    )


def run_authentication(config: Config) -> None:
    oauth = create_oauth(config)
    state = secrets.token_urlsafe(32)
    authorization_url = oauth.get_authorize_url(state=state)
    print("Open this Spotify authorization URL in your browser:")
    print(authorization_url)
    response_url = input("Paste the redirected URL: ").strip()

    response = urlsplit(response_url)
    expected = urlsplit(config.redirect_uri)
    if (response.scheme, response.netloc, response.path) != (expected.scheme, expected.netloc, expected.path):
        raise AuthenticationError("The redirected URL does not match SPOTIPY_REDIRECT_URI.")
    params = parse_qs(response.query)
    if params.get("state", [None])[0] != state:
        raise AuthenticationError("OAuth state validation failed. Run --auth again.")
    if params.get("error"):
        raise AuthenticationError("Spotify authorization was denied. Run --auth again to retry.")
    code = params.get("code", [None])[0]
    if not code:
        raise AuthenticationError("The redirected URL did not contain an authorization code.")

    try:
        old_umask = os.umask(0o777 ^ PRIVATE_FILE_MODE)
        try:
            oauth.get_access_token(code=code, check_cache=False)
        finally:
            os.umask(old_umask)
        if config.token_path.exists():
            config.token_path.chmod(PRIVATE_FILE_MODE)
    except Exception as exc:
        raise AuthenticationError("Spotify authorization failed. Check the app settings and run --auth again.") from exc
    print("Spotify authorization saved to the private application cache.")


def validate_cached_authorization(oauth: Any) -> None:
    try:
        token = oauth.validate_token(oauth.cache_handler.get_cached_token())
    except Exception as exc:
        raise AuthenticationError("Could not read the Spotify token cache. Run --auth again.") from exc
    if not isinstance(token, dict) or not token.get("access_token"):
        raise AuthenticationError("No usable Spotify authorization is cached. Run --auth first.")
    if token.get("scope") and OAUTH_SCOPE not in str(token["scope"]).split():
        raise AuthenticationError("The cached Spotify authorization lacks the required scope. Run --auth again.")
    expires_at = token.get("expires_at")
    if expires_at is not None and expires_at <= time.time() and not token.get("refresh_token"):
        raise AuthenticationError("The Spotify token expired without a refresh token. Run --auth again.")


def create_spotify_client(oauth: Any) -> Any:
    try:
        import spotipy
    except ImportError as exc:
        raise ConfigurationError("Install the dependencies from requirements.txt.") from exc
    return spotipy.Spotify(
        auth_manager=oauth,
        requests_timeout=API_TIMEOUT_SECONDS,
        retries=SDK_RETRIES,
        status_retries=SDK_STATUS_RETRIES,
    )


def _metadata_identity(item: Mapping[str, Any], artists: list[str]) -> tuple[Any, ...] | None:
    title = item.get("name")
    album = (item.get("album") or {}).get("name") if isinstance(item.get("album") or {}, Mapping) else None
    duration = item.get("duration_ms")
    if (not isinstance(title, str) or not title.strip() or not artists or not isinstance(album, str)
            or not album.strip() or not isinstance(duration, int) or isinstance(duration, bool) or duration < 0):
        return None
    return ("metadata", title.strip().casefold(), tuple(a.casefold() for a in artists),
            str(album).strip().casefold(), duration)


def normalize_playback(payload: Any, received_at_monotonic: float | None = None) -> Playback:
    received = time.monotonic() if received_at_monotonic is None else received_at_monotonic
    if payload is None:
        return Playback(kind="idle", received_at_monotonic=received)
    if not isinstance(payload, Mapping):
        raise MalformedPlayback
    is_playing = payload.get("is_playing")
    if not isinstance(is_playing, bool):
        raise MalformedPlayback
    if not is_playing:
        return Playback(kind="idle", received_at_monotonic=received)

    item = payload.get("item")
    if item is None:
        return Playback(kind="idle", received_at_monotonic=received)
    if not isinstance(item, Mapping):
        raise MalformedPlayback
    if item.get("type") != "track":
        return Playback(kind="unsupported", received_at_monotonic=received)

    artist_values = item.get("artists", [])
    if not isinstance(artist_values, list):
        raise MalformedPlayback
    artists = [a.get("name", "").strip() for a in artist_values
               if isinstance(a, Mapping) and isinstance(a.get("name"), str) and a.get("name", "").strip()]
    album_obj = item.get("album")
    album = album_obj.get("name", "").strip() if isinstance(album_obj, Mapping) and isinstance(album_obj.get("name", ""), str) else ""
    release_date = album_obj.get("release_date") if isinstance(album_obj, Mapping) else None
    release_year = UNKNOWN_YEAR
    if isinstance(release_date, str):
        year = release_date.split("-", 1)[0]
        if len(year) == 4 and year.isascii() and year.isdigit() and int(year) > 0:
            release_year = year
    title = item.get("name", "").strip() if isinstance(item.get("name", ""), str) else ""
    duration = item.get("duration_ms")
    progress = payload.get("progress_ms")

    spotify_id = item.get("id")
    uri = item.get("uri")
    if isinstance(spotify_id, str) and spotify_id:
        identity = ("id", spotify_id)
    elif isinstance(uri, str) and uri:
        identity = ("uri", uri)
    else:
        identity = _metadata_identity(item, artists)
    if identity is None:
        return Playback(kind="unsupported", received_at_monotonic=received)

    images = album_obj.get("images", []) if isinstance(album_obj, Mapping) else []
    cover_url = choose_cover_url(images)
    return Playback(
        kind="music", identity=identity,
        title=title or UNKNOWN_TITLE,
        artists=", ".join(artists) or UNKNOWN_ARTIST,
        album=album or UNKNOWN_ALBUM,
        release_year=release_year,
        cover_url=cover_url,
        duration_ms=duration if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0 else None,
        progress_ms=progress if isinstance(progress, int) and not isinstance(progress, bool) and progress >= 0 else None,
        received_at_monotonic=received,
    )


def choose_cover_url(images: Any) -> str | None:
    if not isinstance(images, list):
        return None
    valid = [image for image in images if isinstance(image, Mapping)
             and isinstance(image.get("url"), str) and image["url"].startswith("https://")]
    if not valid:
        return None
    with_width = [image for image in valid if isinstance(image.get("width"), int)]
    selected = min(with_width, key=lambda image: abs(image["width"] - ART_PREFERRED_WIDTH_PX)) if with_width else valid[0]
    return selected["url"]


def choose_delay_seconds(playback: Playback, now_monotonic: float | None = None) -> float:
    if playback.kind != "music":
        return IDLE_POLL_SECONDS
    duration = playback.duration_ms
    progress = playback.progress_ms
    if duration is None or progress is None or progress > duration:
        return INVALID_TIMING_POLL_SECONDS
    now = time.monotonic() if now_monotonic is None else now_monotonic
    deadline = playback.received_at_monotonic + (duration - progress) / 1000.0
    return max(MIN_POLL_SECONDS, deadline - now)


def _valid_image_signature(data: bytes) -> bool:
    return (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")
            or data.startswith((b"GIF87a", b"GIF89a")) or (data.startswith(b"RIFF") and data[8:12] == b"WEBP"))


def download_artwork(url: str | None, config: Config, session: Any,
                     cache_state: OrderedDict[str, str]) -> str | None:
    if not url:
        return None
    if url in cache_state and Path(cache_state[url]).is_file():
        cache_state.move_to_end(url)
        return str(cache_state[url])

    response = None
    try:
        response = session.get(url, timeout=ART_TIMEOUT_SECONDS, stream=True)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if not content_type.startswith("image/"):
            return None
        length = response.headers.get("Content-Length")
        if length and int(length) > ART_MAX_BYTES:
            return None

        target_path = config.cache_dir / f"{config.art_path.name}-{hashlib.sha256(url.encode('utf-8')).hexdigest()}.img"
        fd, temp_name = tempfile.mkstemp(prefix=".cover-", dir=config.cache_dir)
        total = 0
        prefix = bytearray()
        try:
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), PRIVATE_FILE_MODE)
                for chunk in response.iter_content(chunk_size=ART_DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > ART_MAX_BYTES:
                        return None
                    if len(prefix) < 16:
                        prefix.extend(chunk[:16 - len(prefix)])
                    target.write(chunk)
            if not _valid_image_signature(bytes(prefix)):
                return None
            os.replace(temp_name, target_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        cache_state[url] = str(target_path)
        cache_state.move_to_end(url)
        while len(cache_state) > ART_CACHE_MAX_ENTRIES:
            _, evicted_path = cache_state.popitem(last=False)
            try:
                Path(evicted_path).unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("Could not remove an old cached artwork file.")
        return str(target_path)
    except Exception:
        LOGGER.warning("Album artwork download failed; using the Spotify icon.")
        return None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def resolve_notification_icon() -> str:
    configured_path = Path(NOTIFICATION_ICON).expanduser()
    if configured_path.is_file():
        return str(configured_path.resolve())
    if configured_path.is_absolute():
        return NOTIFICATION_ICON
    for directory in NOTIFICATION_ICON_SEARCH_DIRS:
        root = Path(directory).expanduser()
        if not root.is_dir():
            continue
        for extension in NOTIFICATION_ICON_EXTENSIONS:
            for candidate in sorted(root.rglob(f"{NOTIFICATION_ICON}.{extension}")):
                if candidate.is_file():
                    return str(candidate.resolve())
    return NOTIFICATION_ICON


def dispatch_notification(playback: Playback, config: Config, session: Any,
                          cache_state: OrderedDict[str, str]) -> None:
    artwork = download_artwork(playback.cover_url, config, session, cache_state)
    # Keep intentional formatting while escaping untrusted Spotify metadata.
    summary = NOTIFICATION_HEADER_TEMPLATE.format(
        artists=html.escape(playback.artists, quote=True),
        year=html.escape(playback.release_year, quote=True),
        album=html.escape(playback.album, quote=True),
    )
    safe_body = NOTIFICATION_BODY_TEMPLATE.format(title=html.escape(playback.title, quote=True))
    command = ["notify-send", "--app-name", NOTIFICATION_APP_NAME, "--icon", resolve_notification_icon()]
    if artwork:
        command.extend(["--hint", f"string:image-path:{Path(artwork).resolve().as_uri()}"])
    command.extend([summary, safe_body])
    subprocess.run(
        command,
        check=True, timeout=NOTIFY_TIMEOUT_SECONDS, capture_output=True, text=True,
    )


def _retry_after_seconds(headers: Mapping[str, str]) -> float:
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value:
        try:
            seconds = float(value)
            if seconds >= 0:
                return seconds
        except ValueError:
            try:
                retry_time = email.utils.parsedate_to_datetime(value).timestamp()
                return max(0.0, retry_time - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    return RATE_LIMIT_FALLBACK_SECONDS


def _api_exception_status(exc: Exception) -> tuple[int | None, Mapping[str, str]]:
    status = getattr(exc, "http_status", None) or getattr(exc, "status", None)
    headers = getattr(exc, "headers", None) or {}
    if isinstance(exc, ApiFailure):
        status, headers = exc.status, exc.headers
    if not status:
        import requests
        if isinstance(exc, requests.exceptions.Timeout | requests.exceptions.ConnectionError):
            return None, {}
    return status, headers


def _sleep_interruptibly(seconds: float) -> bool:
    """Return True when shutdown interrupted the wait."""
    return _STOP_EVENT.wait(max(0.0, seconds))


def _refresh_once(oauth: Any) -> bool:
    try:
        token = oauth.cache_handler.get_cached_token()
        refresh_token = token.get("refresh_token") if isinstance(token, dict) else None
        if not refresh_token:
            return False
        old_umask = os.umask(0o777 ^ PRIVATE_FILE_MODE)
        try:
            oauth.refresh_access_token(refresh_token)
        finally:
            os.umask(old_umask)
        return True
    except Exception:
        return False


def run_daemon(config: Config, spotify: Any, oauth: Any,
               stop_event: threading.Event | None = None) -> None:
    stop_event = _STOP_EVENT if stop_event is None else stop_event
    session = None
    try:
        import requests
        session = requests.Session()
    except ImportError as exc:
        raise ConfigurationError("Install the dependencies from requirements.txt.") from exc

    last_notified_identity: tuple[Any, ...] | None = None
    cache_state: OrderedDict[str, str] = OrderedDict()
    clear_stale_artwork(config)
    transient_failures = 0
    auth_rejections = 0
    LOGGER.info("Spotify notifier started.")

    while not stop_event.is_set():
        try:
            payload = spotify.current_playback()
            response_received = time.monotonic()
            playback = normalize_playback(payload, response_received)
            transient_failures = 0
            auth_rejections = 0
        except AuthenticationError:
            raise
        except MalformedPlayback:
            LOGGER.warning("Spotify returned a malformed playback response; retrying after %s seconds.", MALFORMED_RESPONSE_RETRY_SECONDS)
            if stop_event.wait(MALFORMED_RESPONSE_RETRY_SECONDS):
                break
            continue
        except Exception as exc:
            status, headers = _api_exception_status(exc)
            if status == 401:
                auth_rejections += 1
                if auth_rejections <= AUTH_REFRESH_ATTEMPTS and _refresh_once(oauth):
                    LOGGER.warning("Spotify rejected authorization; refreshed the token and will retry now.")
                    continue
                raise AuthRejected from exc
            if status == 403:
                raise ConfigurationError("Spotify denied playback access. Reauthorize with --auth and check the requested scope.") from exc
            if status == 429:
                delay = _retry_after_seconds(headers)
                LOGGER.warning("Spotify rate limited the request; retrying after %.1f seconds.", delay)
            elif status is not None and status < 500:
                LOGGER.error("Spotify API request failed with status %s; retrying after %s seconds.", status, IDLE_POLL_SECONDS)
                delay = IDLE_POLL_SECONDS
            else:
                transient_failures += 1
                delay = min(RETRY_INITIAL_SECONDS * (RETRY_MULTIPLIER ** (transient_failures - 1)), RETRY_MAX_SECONDS)
                LOGGER.warning("Spotify request failed; retry %s in %.1f seconds.", transient_failures, delay)
            if stop_event.wait(delay):
                break
            continue

        if playback.kind == "music":
            if playback.identity != last_notified_identity:
                try:
                    dispatch_notification(playback, config, session, cache_state)
                except (subprocess.SubprocessError, OSError):
                    LOGGER.warning("Notification dispatch failed; this track remains eligible at the next scheduled poll.")
                else:
                    last_notified_identity = playback.identity
                    LOGGER.info("Notification submitted for the current track.")
        elif playback.kind == "unsupported":
            LOGGER.info("Unsupported playback content observed; skipping notification.")
        else:
            LOGGER.debug("No music is playing.")
        delay = choose_delay_seconds(playback)

        LOGGER.debug("Next playback poll in %.1f seconds.", delay)
        if stop_event.wait(delay):
            break

    if session is not None:
        session.close()
    LOGGER.info("Spotify notifier stopped.")


def _handle_signal(signum: int, _frame: Any) -> None:
    LOGGER.info("Shutdown requested by signal %s.", signum)
    _STOP_EVENT.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth", action="store_true", help="authorize the Spotify account and save a token")
    args = parser.parse_args(argv)
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    logging.getLogger("spotipy").setLevel(logging.WARNING)
    os.umask(0o777 ^ PRIVATE_FILE_MODE)

    try:
        config = load_configuration()
        if args.auth:
            run_authentication(config)
            return 0
        executable = shutil.which("notify-send")
        if not executable:
            raise ConfigurationError("notify-send was not found. Install libnotify-bin (Debian/Ubuntu) or libnotify (Arch).")
        oauth = create_oauth(config)
        validate_cached_authorization(oauth)
        spotify = create_spotify_client(oauth)
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
        run_daemon(config, spotify, oauth)
    except ConfigurationError as exc:
        LOGGER.error("%s", exc)
        return EXIT_CONFIG_ERROR
    except AuthenticationError as exc:
        LOGGER.error("%s", exc)
        return EXIT_AUTH_ERROR
    except AuthRejected:
        LOGGER.error("Spotify authorization was rejected after refresh. Run --auth to authorize again.")
        return EXIT_AUTH_ERROR
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
