# Suzumushi maintenance guidance

## Connected tools first

For Jira, Confluence, GitHub, Databricks, or other connected-service records, discover available apps, plugins, skills, and MCP tools first. Retrieve source records through the dedicated connector before browser automation, shell/network fallbacks, or user questions. Explain any fallback and why the connector could not supply the data.

## Documentation responsibilities

`README.md` covers installation and runtime behavior; this file covers implementation, configuration, testing, and maintenance. `IMPLEMENTATION_PLAN.md` is historical; the current script defines implemented behavior.

## Architecture and dependencies

`suzumushi.py` is the executable: Spotipy polls `GET /v1/me/player` across account devices, Requests downloads artwork, and `notify-send` submits notifications. `systemd/spotify-notifier.service` manages it.

Require GNOME Shell, `notify-send` with `--print-id`/`--replace-id`, and `gdbus` for replacement identity queries.

`requirements.txt` pins Spotipy 2.26.0 and Requests 2.34.2. Use the dedicated virtual environment under `~/.local/share/suzumushi/`; the installed script is independent of the checkout.

Keep normalization, scheduling, authentication, artwork, and dispatch in focused functions. Put tunables in descriptive constants at the script's top; name units where applicable.

## Authentication and credentials

- Read `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET`, and `SPOTIPY_REDIRECT_URI` from the environment; never embed credentials or auto-load `.env` files.
- Use Authorization Code OAuth with `user-read-playback-state`; register the recommended redirect `http://127.0.0.1:8888/callback`.
- Only `--auth` is interactive. Validate the returned redirect URI and OAuth state before exchanging the code. Service mode uses cached authorization and refreshes tokens without browser launches or terminal prompts; disabling browser launch alone does not prevent Spotipy's interactive fallback.
- Cache tokens at `$XDG_CACHE_HOME/suzumushi/token.json` (default `~/.cache/suzumushi/token.json`), with directory/file modes `0700`/`0600`.
- Never commit or log credentials, tokens, or OAuth response URLs; `.env.example` contains placeholders only.
- Shell exports do not reach systemd automatically. Use the README's private environment file and service drop-in. Restore session environment imports after user-manager restarts.

## Playback state and scheduling

Compare stable track IDs, then URIs, then title/artist/album/duration for local music lacking both. Remember identity only after successful submission; retain it through pauses, empty responses, and API errors. Restarting clears track deduplication, allowing a fresh banner for the current track while reusing the persisted notification entry.

Poll at estimated song end using a monotonic deadline set on response arrival; artwork and dispatch time consume that wait. Apply the one-second minimum at/beyond the deadline to avoid busy loops. Add neither intermediate playback polls nor a maximum active wait.

Idle, paused, unsupported, or empty playback waits 120 seconds; invalid active timing has a separate 120-second fallback. Distinguish network failures from idle state. Use interruptible waits for prompt shutdown.

Notify only observed tracks. After dispatch failure, retry only if that track is still current at the next scheduled poll; never queue ended tracks.

## Notification rendering and artwork

Use label `Suzumushi`, heading `Artist · Album (year)`, and body `<b>Track</b>`, with layouts in constants. Escape metadata before inserting intentional markup. GNOME controls native heading weight; bold markup belongs in the body.

Read the album release year, accepting year-only and month-precision dates; use `Unknown year` when unusable or absent. Reissues may differ from the original recording's year.

Run `notify-send` with list arguments, no shell, checked exit status, and `NOTIFY_TIMEOUT_SECONDS` as the command timeout. Desktop settings/extensions control banner duration; successful submission does not prove visibility.

### Notification replacement

- Keep one latest-track list entry and request a banner per observed track change. Always use `--print-id`; add `--replace-id` when reusable.
- Store `notification_id`, `bus_id`, `server_owner`, and `shell_owner` in memory and `notification.json` in the cache directory, using `0700`/`0600` permissions and atomic writes.
- Reuse IDs only when bus ID and both owners match: GNOME's notification proxy and Shell have separate owners. Recheck after submission; discard state if changed.
- Always save the returned ID, which may change after manual dismissal. Failed submissions preserve state; invalid output or cache-write failures must not retry an already submitted track.
- Pauses retain the latest entry. Clear historical entries from before this feature manually once.

### Artwork

Send artwork as a file URI in the `image-path` hint; album art passed as `--icon` occupies GNOME's small app-icon slot. For `--icon`, prefer an explicit existing icon path, otherwise resolve an installed file across configured user/system icon directories and themes, falling back to the theme name.

Download art only as needed, with finite timeouts, a 5 MiB cap, signature validation, private permissions, and atomic writes. Bound the cache and reuse matching covers. Missing/failed art uses the application icon, never another album's stale cover.

## Configuration constants

Time constants use seconds unless noted; byte limits and image widths name their units. Apply edits using the maintenance steps below.

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

## Service maintenance

Configure the Python path, restart policy, environment, and `UMask` in `systemd/spotify-notifier.service`. Defaults: `Restart=on-failure`, `RestartSec=5`, `RestartPreventExitStatus=2 3`, `UMask=0077`, unbuffered Python output. Align permanent-failure exit codes with the script.

The unit starts with `default.target`, orders after `graphical-session.target`, and stops with it. Check session integration for startup/notification failures; if that target is unavailable, use the desktop's documented session target. The user manager needs the active display and D-Bus environment.

After script edits, copy to `~/.local/share/suzumushi/` and restart. After unit edits, copy to the installed unit location, run `systemctl --user daemon-reload`, then restart. Preserve private credentials and token caches.

To uninstall service startup:

```bash
systemctl --user disable --now spotify-notifier.service
rm ~/.config/systemd/user/spotify-notifier.service
rm -f ~/.config/systemd/user/spotify-notifier.service.d/environment.conf
systemctl --user daemon-reload
```

## Failure handling and verification

- Missing credentials, `notify-send`, or `gdbus`: clear error and permanent exit status.
- Missing, unusable, revoked, or persistently rejected authorization: exit with `--auth` instructions. HTTP 401 gets one refresh attempt; persistent 401 and HTTP 403 stop the service with an authorization error.
- HTTP 429: server `Retry-After`, falling back to 120 seconds if absent/invalid.
- Network failures/HTTP 5xx: 5, 10, 20, 40, 80, then at most 120 seconds; successful playback requests reset backoff.
- Malformed successful responses: retry after 120 seconds. Log artwork/notification failures and retain the last successful track identity.
- Missing banners: inspect the service journal, session D-Bus/display environment, and GNOME notification settings.
- Replacement verification: while the entry exists, confirm its saved ID survives a service restart. Observe two tracks showing banners while only the latest entry remains; an unchanged ID alone does not prove visibility.

Verified 2026-10-16: ID reused across a service restart and two dummy notifications seven seconds apart; user confirmed the dummy test and running service behavior.

Install `requirements.txt`, then run the standard-library test suite:

```bash
python3 -m unittest discover -s tests -v
```

`.github/workflows/tests.yml` installs `requirements.txt` and runs the suite on Ubuntu/Python 3.12 after every push to `main`; the README badge tracks it. Mocked tests need no Spotify credentials or desktop session.
