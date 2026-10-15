# suzumushi

Suzumushi watches Spotify playback across the account's devices and submits a GNOME desktop notification when the observed music track changes. It runs as a `systemd --user` service. It does not notify for podcasts, advertisements, paused playback, or unsupported items.

## Requirements

- Python 3.10 or newer and `python3-venv`
- `notify-send`: install `libnotify-bin` on Debian/Ubuntu or `libnotify` on Arch
- A Spotify Developer app with `http://127.0.0.1:8888/callback` registered as a Redirect URI

The runtime dependencies are pinned in `requirements.txt`: Spotipy 2.26.0 and Requests 2.34.2.

## Credentials and authentication

Set these process environment variables. The redirect URI must exactly match one registered in the Spotify Developer Dashboard; the default shown below is recommended.

```bash
export SPOTIPY_CLIENT_ID='your-client-id'
export SPOTIPY_CLIENT_SECRET='your-client-secret'
export SPOTIPY_REDIRECT_URI='http://127.0.0.1:8888/callback'
```

Run the explicit interactive authorization once as the desktop user:

```bash
~/.local/share/suzumushi/.venv/bin/python ~/.local/share/suzumushi/suzumushi.py --auth
```

Open the printed URL, approve access, then paste the redirected URL into the terminal. The callback path does not need a local listener because authorization uses the manual paste flow. Suzumushi checks the redirect URI and OAuth state before saving the token. The token is stored at `$XDG_CACHE_HOME/suzumushi/token.json`, or `~/.cache/suzumushi/token.json` when `XDG_CACHE_HOME` is unset. The directory and file use user-only permissions. Credentials and tokens must not be committed to this repository.

## Install the runtime

From the repository root:

```bash
mkdir -p ~/.local/share/suzumushi
python3 -m venv ~/.local/share/suzumushi/.venv
~/.local/share/suzumushi/.venv/bin/pip install -r requirements.txt
install -m 0755 suzumushi.py ~/.local/share/suzumushi/suzumushi.py
```

Export the credentials in the desktop session, then complete the `--auth` step above. Shell exports do not automatically reach a systemd user service. Import credential and graphical-session variables into the user manager and restart the service after changing them:

```bash
systemctl --user import-environment \
  SPOTIPY_CLIENT_ID SPOTIPY_CLIENT_SECRET SPOTIPY_REDIRECT_URI \
  DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR XDG_CACHE_HOME
```

Imports live in the user manager's environment and need to be restored if that manager restarts. For persistent provisioning, put the three Spotify variables in a private file such as `~/.config/suzumushi/environment` with mode `0600`, then add this under `[Service]` in a systemd drop-in:

```ini
EnvironmentFile=%h/.config/suzumushi/environment
```

The repository's `.env.example` lists variable names only. Suzumushi does not load `.env` files.

## Install and manage the service

Copy the provided unit and start it:

```bash
mkdir -p ~/.config/systemd/user
install -m 0644 systemd/spotify-notifier.service ~/.config/systemd/user/spotify-notifier.service
systemctl --user daemon-reload
systemctl --user import-environment \
  SPOTIPY_CLIENT_ID SPOTIPY_CLIENT_SECRET SPOTIPY_REDIRECT_URI \
  DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR XDG_CACHE_HOME
systemctl --user enable --now spotify-notifier.service
```

The unit starts with the user manager, orders itself after `graphical-session.target` when available, and stops with that target. `WantedBy=default.target` supports login startup. Check that your desktop exposes the target with `systemctl --user status graphical-session.target`; if it does not, remove `PartOf=graphical-session.target` from the installed unit and use the desktop's documented session target. The service relies on the user manager receiving the active session's D-Bus and display environment for `notify-send` to reach the desktop.

```bash
systemctl --user status spotify-notifier.service
journalctl --user -u spotify-notifier.service -f
systemctl --user restart spotify-notifier.service
systemctl --user stop spotify-notifier.service
```

To uninstall, stop and disable the service, remove the unit, and reload systemd:

```bash
systemctl --user disable --now spotify-notifier.service
rm ~/.config/systemd/user/spotify-notifier.service
systemctl --user daemon-reload
```

The installed copy is independent of the checkout. After editing Python settings, copy the updated script to `~/.local/share/suzumushi/suzumushi.py` and restart the service. The virtual environment and installed dependency pins are beside the script.

## Playback and notification behavior

The daemon polls immediately at startup. It compares stable Spotify track IDs, then URIs, then a title/artist/album/duration identity for local music without an ID or URI. It notifies only after a successful `notify-send` submission and remembers the last successfully submitted track across pauses, empty responses, and API errors for the life of the process. A process restart clears that memory and intentionally causes a startup notification for a currently playing song.

When music is playing, the next poll is scheduled for the estimated end of that track. Processing time for artwork and notification is subtracted from the wait, and a one-second minimum prevents a busy loop. There are no intermediate polls during the song. When playback is idle, paused, unsupported, or has no playable item, the daemon waits 120 seconds. Invalid track timing also uses the 120-second fallback. API errors use the retry schedule described below and are not treated as idle playback.

Only playback observed by a poll can be reported. Idle detection can take nearly two minutes. A skip, pause, seek, or device transfer is noticed at the next scheduled poll; a track that begins and ends between polls can be missed. If notification dispatch for A fails, A is retried only if it is still playing at the next scheduled poll. When another track is playing, the daemon handles the current track and does not queue a stale notification for A.

Artwork downloads have a four-second timeout and a 5 MiB cap, are checked for an image signature, and are stored atomically in the private cache. Failed or missing artwork falls back to the Spotify icon. Notification arguments are passed directly to `notify-send` without a shell. A successful command submission does not guarantee that GNOME displays a banner; desktop notification settings may suppress it.

Notifications use `Suzumushi` as the title and `Track - Artist - Album` as the body, alongside available album artwork.

## Configuration constants

Change constants at the top of `suzumushi.py`. Changes take effect after copying the updated file into the install directory and restarting the service. All time constants are seconds unless noted; byte limits and image widths are explicit in their names.

| Constant | Default | Purpose |
| --- | ---: | --- |
| `IDLE_POLL_SECONDS` | 120 | Wait after idle, paused, or unsupported playback |
| `MIN_POLL_SECONDS` | 1 | Minimum active wait |
| `INVALID_TIMING_POLL_SECONDS` | 120 | Fallback for invalid active track timing |
| `MALFORMED_RESPONSE_RETRY_SECONDS` | 120 | Retry after malformed successful response |
| `RETRY_INITIAL_SECONDS` / `RETRY_MULTIPLIER` / `RETRY_MAX_SECONDS` | 5 / 2 / 120 | Exponential transient failure schedule |
| `RATE_LIMIT_FALLBACK_SECONDS` | 120 | Retry-After fallback for HTTP 429 |
| `AUTH_REFRESH_ATTEMPTS` | 1 | Refresh attempts after an authorization rejection |
| `API_TIMEOUT_SECONDS` / `AUTH_TIMEOUT_SECONDS` | 10 / 10 | Spotify API and OAuth request timeouts |
| `ART_TIMEOUT_SECONDS` / `NOTIFY_TIMEOUT_SECONDS` | 4 / 5 | Artwork and notification timeouts |
| `SDK_RETRIES` / `SDK_STATUS_RETRIES` | 0 / 0 | Spotipy retries; application loop owns recovery |
| `ART_MAX_BYTES` / `ART_DOWNLOAD_CHUNK_BYTES` | 5 MiB / 64 KiB | Artwork size bound and read chunk |
| `ART_PREFERRED_WIDTH_PX` / `ART_CACHE_MAX_ENTRIES` | 300 / 1 | Preferred cover width and cache bound |
| `NOTIFICATION_APP_NAME` / `NOTIFICATION_ICON` | `Spotify` / `spotify` | Notification identity and fallback icon |
| `NOTIFICATION_TITLE` / `NOTIFICATION_BODY_SEPARATOR` | `Suzumushi` / ` - ` | Notification title and separator between track, artist, and album |
| `UNKNOWN_TITLE` / `UNKNOWN_ARTIST` / `UNKNOWN_ALBUM` | Display fallback text | Missing metadata labels |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / timestamped | Journald logging |
| `OAUTH_SCOPE` | `user-read-playback-state` | Spotify permission requested |
| `CLIENT_ID_ENV` / `CLIENT_SECRET_ENV` / `REDIRECT_URI_ENV` | `SPOTIPY_*` names | Credential environment variable names |
| `DEFAULT_REDIRECT_URI` | `http://127.0.0.1:8888/callback` | Recommended registered redirect value |
| `CACHE_APP_DIR` / `TOKEN_FILENAME` / `ART_FILENAME` | `suzumushi` / `token.json` / `cover` | Cache paths |
| `PRIVATE_DIR_MODE` / `PRIVATE_FILE_MODE` | `0700` / `0600` | Cache permissions |
| `EXIT_CONFIG_ERROR` / `EXIT_AUTH_ERROR` | 2 / 3 | Permanent failure statuses used by systemd |

The service's Python path, `RestartSec`, restart policy, environment, and `UMask` are systemd settings in `systemd/spotify-notifier.service`, not Python constants. Change those values in the unit, copy it to the installed unit location, run `systemctl --user daemon-reload`, and restart the service.

## Failure handling and troubleshooting

- Missing credentials or `notify-send` produce a clear error and a permanent exit status.
- Missing, unusable, revoked, or persistently rejected authorization exits with instructions to run `--auth`; service mode never launches a browser or requests terminal input.
- HTTP 401 gets one token refresh attempt; persistent 401 and HTTP 403 stop the service with an authorization error.
- HTTP 429 waits for the server's `Retry-After` value, or 120 seconds when it is absent or invalid.
- Network failures and HTTP 5xx use 5, 10, 20, 40, 80, then at most 120 seconds. A successful playback request resets this schedule.
- Malformed successful responses retry after 120 seconds. Artwork and notification failures are logged and do not discard the last successful notification identity.
- For missing desktop banners, inspect the service journal, check the session D-Bus and display environment, and review GNOME notification settings.

Run local tests after installing `requirements.txt`; the tests use Python's standard library test runner:

```bash
python3 -m unittest discover -s tests -v
```
