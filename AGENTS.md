# Suzumushi maintenance guidance

## Connected tools first

For tasks referencing Jira, Confluence, GitHub, Databricks, or another connected-service record:

1. Discover available connected apps, plugins, skills, and MCP tools first.
2. Use the dedicated connector as the source-of-record retrieval path before browser automation, shell/network fallbacks, or asking the user for details.
3. State any fallback and why the dedicated connector could not supply the data.

## Documentation responsibilities

Keep `README.md` focused on installation and how the script runs. Put implementation details, configuration reference, testing, and maintenance guidance here. `IMPLEMENTATION_PLAN.md` records the original plan; consult the current script for implemented behavior.

## Architecture and dependencies

`suzumushi.py` is the executable module. Spotipy polls `GET /v1/me/player` across the account's devices; Requests downloads album artwork; `notify-send` submits notifications. `systemd/spotify-notifier.service` manages the process.

`notify-send` must support `--print-id` and `--replace-id`. `gdbus` queries the session bus, notification proxy, and GNOME Shell identities for safe notification replacement across service restarts. GNOME Shell is required.

`requirements.txt` pins Spotipy 2.26.0 and Requests 2.34.2. Install dependencies into the dedicated virtual environment. The runtime copy is installed under `~/.local/share/suzumushi/`, independently of the checkout.

Keep normalization, scheduling, authentication, artwork handling, and notification dispatch in focused functions. Keep configurable values in descriptive module constants at the top of the script, with units in names where applicable.

## Authentication and credentials

- Read `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET`, and `SPOTIPY_REDIRECT_URI` from the process environment. Do not embed credentials or automatically load `.env` files.
- Use Authorization Code OAuth with `user-read-playback-state`. The recommended registered redirect is `http://127.0.0.1:8888/callback`.
- Keep `--auth` as the explicit interactive flow. Validate the returned redirect URI and OAuth state before exchanging the code.
- Normal service mode uses cached authorization and refreshes tokens without browser launches or terminal prompts. Disabling browser launch alone is insufficient to prevent Spotipy's interactive fallback.
- Cache tokens at `$XDG_CACHE_HOME/suzumushi/token.json`, defaulting to `~/.cache/suzumushi/token.json`. Use directory mode `0700` and file mode `0600`.
- Never commit or log credentials, tokens, or OAuth response URLs. `.env.example` contains placeholders only.
- Shell exports do not automatically reach systemd. The README uses a private environment file and a service drop-in. Session imports into the user manager must be restored if that manager restarts.

## Playback state and scheduling

Compare stable track IDs, then URIs, then a title/artist/album/duration identity for local music without either. Update the remembered identity only after successful notification submission. Preserve it through pauses, empty responses, and API errors for the process lifetime. A restart clears this track deduplication state and permits a fresh banner for the current track; the persisted notification ID is reused to replace its existing list entry.

Schedule the next playback request at the estimated song end using a monotonic deadline calculated when the response arrives. Subtract artwork and notification processing time from the wait. Use the one-second minimum to prevent a busy loop at or beyond the deadline. Do not introduce intermediate playback polling or a maximum active wait.

Idle, paused, unsupported, or empty playback waits 120 seconds. Invalid active timing uses its separate 120-second fallback. Keep network failures distinct from idle state. Use interruptible waits for prompt shutdown.

Only observed tracks can produce notifications. If notification dispatch fails, retry that track only if it is still current at the next scheduled poll. Do not queue notifications for tracks that have already ended.

## Notification rendering and artwork

Use application label `Suzumushi`. Format the heading as `Artist · Album (year)` and the body as `<b>Track</b>`. Keep the layout in constants. Escape Spotify metadata before interpolating intentional markup. The native heading's font weight is controlled by GNOME; bold markup belongs in the body.

Read the year from the album's release date, including year-only and month-precision dates. Use `Unknown year` for unavailable or unusable values. Reissue metadata can differ from the original recording's release year.

Pass `notify-send` arguments as a list with no shell, a checked exit status, and a subprocess timeout. `NOTIFY_TIMEOUT_SECONDS` limits command execution, not banner visibility. Desktop settings and extensions control visible duration. Successful submission does not prove a visible banner.

### Notification replacement

- Keep one latest-track entry in GNOME's notification list while requesting a banner for each observed track change. Use `notify-send --print-id` on every submission and `--replace-id` when a previous ID is available.
- Keep replacement state in memory and in `notification.json` under the existing cache directory (`0700` directory, `0600` file, atomic writes). The stored fields are `notification_id`, `bus_id`, `server_owner`, and `shell_owner`.
- Reuse an ID only when the session bus ID, notification service owner, and GNOME Shell owner match. GNOME's notification proxy and Shell have separate owners; both must be checked. Query the identity again after submission and discard state if it changed.
- Manually dismissed notifications can receive a new ID; always save the returned ID. Preserve state on failed submissions. Invalid output or cache-write failures must not retry an already submitted track.
- Pauses retain the latest entry. Existing historical notifications are not removed automatically and need one-time manual clearing.

### Artwork

Send artwork through the `image-path` hint using a file URI, separately from the Spotify application icon. Passing album art as `--icon` puts it in the small app-icon slot on GNOME. Resolve the configured application icon to an installed image file across the configured user/system icon directories, including other themes, before passing it as `--icon`. If no file is found, retain the theme-name fallback. An explicit existing icon path takes precedence.

Download artwork only when needed, with finite timeouts, a 5 MiB size cap, image signature validation, private permissions, and atomic cache writes. Keep a bounded cache and reuse matching covers. Failed or missing art uses the application icon without stale artwork from another album.

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
| `DBUS_TIMEOUT_SECONDS` | 5 | Timeout for each notification server identity query |
| `SDK_RETRIES` / `SDK_STATUS_RETRIES` | 0 / 0 | Spotipy retries; application loop owns recovery |
| `ART_MAX_BYTES` / `ART_DOWNLOAD_CHUNK_BYTES` | 5 MiB / 64 KiB | Artwork size bound and read chunk |
| `ART_PREFERRED_WIDTH_PX` / `ART_CACHE_MAX_ENTRIES` | 300 / 1 | Preferred cover width and cache bound |
| `NOTIFICATION_APP_NAME` / `NOTIFICATION_ICON` | `Suzumushi` / `spotify` | Notification identity and fallback icon |
| `NOTIFICATION_ICON_SEARCH_DIRS` / `NOTIFICATION_ICON_EXTENSIONS` | User/system icon directories / PNG, SVG, XPM | Resolve installed application icon files independently of the active theme |
| `NOTIFICATION_HEADER_TEMPLATE` | `{artists} · {album} ({year})` | Notification heading layout |
| `NOTIFICATION_BODY_TEMPLATE` | `<b>{title}</b>` | Bold track title in the notification body |
| `UNKNOWN_YEAR` | `Unknown year` | Missing album release year |
| `UNKNOWN_TITLE` / `UNKNOWN_ARTIST` / `UNKNOWN_ALBUM` | Display fallback text | Missing metadata labels |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / timestamped | Journald logging |
| `OAUTH_SCOPE` | `user-read-playback-state` | Spotify permission requested |
| `CLIENT_ID_ENV` / `CLIENT_SECRET_ENV` / `REDIRECT_URI_ENV` | `SPOTIPY_*` names | Credential environment variable names |
| `DEFAULT_REDIRECT_URI` | `http://127.0.0.1:8888/callback` | Recommended registered redirect value |
| `CACHE_APP_DIR` / `TOKEN_FILENAME` / `ART_FILENAME` | `suzumushi` / `token.json` / `cover` | Cache paths |
| `NOTIFICATION_STATE_FILENAME` / `NOTIFICATION_STATE_MAX_BYTES` | `notification.json` / 4096 | Persistent notification replacement state and read limit |
| `PRIVATE_DIR_MODE` / `PRIVATE_FILE_MODE` | `0700` / `0600` | Cache permissions |
| `EXIT_CONFIG_ERROR` / `EXIT_AUTH_ERROR` | 2 / 3 | Permanent failure statuses used by systemd |

The service's Python path, `RestartSec`, restart policy, environment, and `UMask` are systemd settings in `systemd/spotify-notifier.service`, not Python constants. Change those values in the unit, copy it to the installed unit location, run `systemctl --user daemon-reload`, and restart the service.

## Service maintenance

The provided service uses `Restart=on-failure`, `RestartSec=5`, `RestartPreventExitStatus=2 3`, `UMask=0077`, and unbuffered Python output. Keep the permanent-failure exit codes aligned with the script.

The unit starts with `default.target`, orders itself after `graphical-session.target`, and stops with that target. Confirm desktop session integration when diagnosing startup or notification failures. If the desktop does not expose that target, adapt the installed unit to its documented session target. The user manager needs the active session's display and D-Bus environment.

After editing the script, copy it to the installed location and restart the service. After editing the unit, copy it, run `systemctl --user daemon-reload`, then restart. Do not overwrite private credentials or token caches during updates.

To uninstall service startup:

```bash
systemctl --user disable --now spotify-notifier.service
rm ~/.config/systemd/user/spotify-notifier.service
rm -f ~/.config/systemd/user/spotify-notifier.service.d/environment.conf
systemctl --user daemon-reload
```

## Failure handling and verification

- Missing credentials, `notify-send`, or `gdbus` produce a clear error and a permanent exit status.
- Missing, unusable, revoked, or persistently rejected authorization exits with instructions to run `--auth`; service mode never launches a browser or requests terminal input.
- HTTP 401 gets one token refresh attempt; persistent 401 and HTTP 403 stop the service with an authorization error.
- HTTP 429 waits for the server's `Retry-After` value, or 120 seconds when it is absent or invalid.
- Network failures and HTTP 5xx use 5, 10, 20, 40, 80, then at most 120 seconds. A successful playback request resets this schedule.
- Malformed successful responses retry after 120 seconds. Artwork and notification failures are logged and do not discard the last successful notification identity.
- For missing desktop banners, inspect the service journal, check the session D-Bus and display environment, and review GNOME notification settings.
- For replacement verification, check that the saved notification ID is unchanged after a service restart while its entry still exists. Then observe two different tracks: each should request a banner, and only the latest entry should remain. A successful command or unchanged ID alone does not prove visible banners.

Live verification on 2026-10-16 confirmed ID reuse across a service restart. Two dummy notifications seven seconds apart reused one ID, and the user confirmed both the dummy test and the running service behaved correctly.

Run local tests after installing `requirements.txt`; the tests use Python's standard library test runner:

```bash
python3 -m unittest discover -s tests -v
```

`.github/workflows/tests.yml` runs this suite on Ubuntu with Python 3.12 after every push to `main`. It installs dependencies from `requirements.txt`; tests use mocks and require no Spotify credentials or desktop session. The README badge tracks this workflow on `main`.
