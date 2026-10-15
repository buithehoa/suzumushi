# Suzumushi implementation plan

Date: 2026-10-15

## 1. Confirmed requirements

Build a Python background daemon that reads Spotify playback across devices and displays GNOME desktop notifications through `notify-send`. Manage it with a systemd user service.

- Poll immediately on startup. If music is playing, display its title, artists, album, and available cover art.
- Subsequently notify only when the observed playing music track changes.
- Support music initially. Ignore podcasts, advertisements, and other unsupported content.
- During playback, wait for the current song's remaining duration before polling again; perform no intermediate playback polls.
- When nothing is playing, wait 300 seconds before polling again.
- Read Spotify credentials from environment variables.
- Keep all configurable values in a clearly labeled constants section at the top of the script so they can be changed in one place.

This document plans implementation. Installing dependencies, authenticating the account, and starting a service are future implementation and rollout steps.

## 2. Proposed behavior and assumptions

Schedule the next playback poll for the current song's estimated end, using its duration and reported progress. There is no five-second cap and no periodic polling during the remaining song duration. Treat observed paused playback as nothing playing, so it receives the 300-second delay.

| Observed state | Notification | Next action |
| --- | --- | --- |
| Startup with playing music A | Display A | Adaptive active wait |
| Startup without playing music | None | Wait 300 seconds |
| Same music A remains playing | None | Adaptive active wait |
| Playing music changes from A to B | Display B | Adaptive active wait |
| A pauses, then resumes | None if A was already displayed | Wait 300 seconds while paused; adaptive wait after detecting resume |
| Seek, repeat A, or transfer A to another device | None | Adaptive active wait |
| No playback response or no playable item | None | Wait 300 seconds |
| Playing episode, advertisement, or unsupported item | None | Wait 300 seconds |
| API request fails | None | Follow error recovery policy |

Keep the last successfully notified track identity in process memory through pauses, empty responses, and errors. Restarting the process clears it and intentionally permits a startup notification. Compare stable track IDs, with URI as a fallback. For local music without either, use a normalized metadata tuple containing title, artists, album, and duration; skip items with insufficient identity metadata rather than treating every null ID as the same track.

Only tracks observed during a poll can be reported. Playback starting during an idle wait can take almost five minutes to detect, plus request time. Manual skips, pauses, and seeks are detected at the previously scheduled poll, which can be minutes later. Tracks that start and finish between polls can be missed entirely. If an unobserved B plays between two observations of A, no change is detected. This delay is an accepted consequence of reducing polling.

## 3. Repository deliverables

Keep the first implementation small, with one executable module and focused functions.

| File | Purpose |
| --- | --- |
| `suzumushi.py` | Executable Python script with a Python shebang: configuration, explicit authentication command, playback loop, notification dispatch, and shutdown |
| `requirements.txt` | Compatible, selected versions of Spotipy and Requests |
| `systemd/spotify-notifier.service` | User service template with documented installation paths |
| `.env.example` | Placeholder environment variable names for reference |
| `.gitignore` | Exclude local credentials, virtual environments, Python caches, and accidental token files |
| `README.md` | Setup, authentication, installation, polling semantics, management, and troubleshooting |

Use a dedicated virtual environment during installation. Install the runtime script under `~/.local/share/suzumushi/` with its virtual environment alongside it. This gives the service stable executable paths independent of the checkout location. Do not put real secrets in the repository.

## 4. Configuration and OAuth

### Centralized constants

Define all configurable application values as descriptive `UPPER_SNAKE_CASE` module constants at the top of `suzumushi.py`. Functions must reference these constants instead of embedding settings as literals. Include units in names for durations and sizes. Apart from the confirmed polling behavior, the defaults below are proposed implementation choices.

| Constant | Default / purpose |
| --- | --- |
| `IDLE_POLL_SECONDS` | `300`; no playing music, including paused or unsupported content |
| `MIN_POLL_SECONDS` | `1`; minimum wait when the estimated song end has passed |
| `INVALID_TIMING_POLL_SECONDS` | `300`; playing music with unusable timing |
| `MALFORMED_RESPONSE_RETRY_SECONDS` | `300`; malformed playback responses |
| `RETRY_INITIAL_SECONDS` | `5`; first transient failure retry |
| `RETRY_MULTIPLIER` | `2`; exponential backoff growth |
| `RETRY_MAX_SECONDS` | `300`; transient failure retry ceiling |
| `RATE_LIMIT_FALLBACK_SECONDS` | `300`; fallback when `Retry-After` is unusable |
| `AUTH_REFRESH_ATTEMPTS` | `1`; refresh attempt after an authorization rejection |
| `API_TIMEOUT_SECONDS` / `AUTH_TIMEOUT_SECONDS` | `10` each; playback and OAuth request timeouts |
| `ART_TIMEOUT_SECONDS` | `4`; artwork request timeout |
| `NOTIFY_TIMEOUT_SECONDS` | `5`; notification subprocess timeout |
| `SDK_RETRIES` / `SDK_STATUS_RETRIES` | `0` each; application loop owns retries |
| `ART_MAX_BYTES` | `5 * 1024 * 1024`; maximum artwork download size |
| `ART_PREFERRED_WIDTH_PX` | `300`; preferred artwork width |
| `ART_CACHE_MAX_ENTRIES` | `1`; bounded artwork cache |
| `NOTIFICATION_APP_NAME` / `NOTIFICATION_ICON` | `"Spotify"` / `"spotify"` |
| `UNKNOWN_TITLE` / `UNKNOWN_ARTIST` / `UNKNOWN_ALBUM` | Display fallback strings |
| `LOG_LEVEL` / `LOG_FORMAT` | Default logging level and timestamped format |
| `OAUTH_SCOPE` | `"user-read-playback-state"` |
| `CLIENT_ID_ENV` / `CLIENT_SECRET_ENV` / `REDIRECT_URI_ENV` | Names of the three Spotify environment variables listed below |
| `CACHE_APP_DIR` / `TOKEN_FILENAME` / `ART_FILENAME` | Application cache directory and file names |
| `PRIVATE_DIR_MODE` / `PRIVATE_FILE_MODE` | `0o700` / `0o600` |
| `EXIT_CONFIG_ERROR` / `EXIT_AUTH_ERROR` | Named permanent-failure exit statuses, aligned with the service template |

Also name any additional configurable value introduced during implementation, including notification formatting or download chunk sizes. Ordinary algorithm operations and protocol-defined response codes do not need to become user settings. Compute cache paths from the constants and the applicable XDG environment at startup. Keep actual credential values exclusively in the environment.

Use `MIN_POLL_SECONDS` in the playback deadline calculation and the appropriate named constants for every idle, fallback, timeout, and recovery path. Keep separate settings for independently tunable behavior even when their defaults match. Do not introduce a maximum active polling interval: the wait remains the song's remaining duration.

Document the constants and their units in the README, including that changes take effect after updating the installed script and restarting the service. Settings interpreted by systemd, such as `RestartSec` and service paths, must remain clearly documented in the unit's configuration section; Python constants cannot configure systemd itself.

### Environment and authentication

Read and validate these variables before contacting Spotify:

| Variable | Requirement |
| --- | --- |
| `SPOTIPY_CLIENT_ID` | Required, nonempty |
| `SPOTIPY_CLIENT_SECRET` | Required, nonempty |
| `SPOTIPY_REDIRECT_URI` | Required; document `http://127.0.0.1:8888/callback` and require the same registered URI |

Use Authorization Code OAuth with the `user-read-playback-state` scope. Spotify documents this scope for the playback state endpoint. [Playback reference](https://developer.spotify.com/documentation/web-api/reference/get-information-about-the-users-current-playback)

Implementation steps:

1. Add an explicit `--auth` command for interactive account authorization. Display the authorization URL, accept the redirected URL, validate OAuth state, and save the resulting token. This command exits after authentication.
2. Store the token at `$XDG_CACHE_HOME/suzumushi/token.json`, defaulting to `~/.cache/suzumushi/token.json`. Create the private cache directory and token file with user-only permissions.
3. In normal daemon mode, use cached authorization and refresh tokens as needed. Prevent implicit browser launches or terminal prompts. `open_browser=False` alone must not be treated as sufficient to prevent interactive input.
4. On missing, revoked, or unusable authorization, log a concise instruction to run `--auth` and exit with a distinct configuration/authentication status.
5. Avoid logging tokens, authorization response URLs, or secrets.

Spotipy documents the proposed environment names, OAuth manager, cache handling, and timeout/retry controls. Select and record the dependency versions during implementation. [Spotipy documentation](https://spotipy.readthedocs.io/en/latest/)

## 5. Playback loop and scheduling

Separate the module into functions for loading configuration, constructing the client, normalizing playback, choosing a delay, dispatching a notification, and running the loop. Keep normalization and delay selection independent of network calls.

Each iteration:

1. Request current playback through Spotipy's `current_playback()` wrapper for `GET /v1/me/player`.
2. Normalize the result into playing music, idle, or unsupported content. Handle empty responses, missing fields, and explicit nulls.
3. For playing music, compare track identity with the last successfully notified identity.
4. For a changed identity, assemble display metadata and dispatch the notification. Update remembered identity only after `notify-send` succeeds.
5. Calculate the appropriate wait and use an interruptible wait so SIGTERM can stop the daemon promptly even during a long song or five-minute idle sleep.

For valid duration and progress:

```text
remaining_seconds = max(0, (duration_ms - progress_ms) / 1000)
next_poll_deadline = response_received_monotonic + remaining_seconds
active_wait = max(MIN_POLL_SECONDS, next_poll_deadline - current_monotonic)
```

Examples before notification processing time: 180 seconds remaining gives a 180-second wait; three seconds gives a three-second wait; one second gives a one-second wait. Set the monotonic deadline when the playback response arrives, so artwork downloads and notification processing consume part of the wait instead of extending it. The one-second minimum prevents a busy loop when the deadline has passed or Spotify still reports a track at its end. Recalculate the deadline from each new response; do not wake periodically to check progress. If duration or progress is absent or invalid, log a warning and use a 300-second fallback instead of frequent polling.

Use the 300-second idle delay for a successful response showing no playing music. Do not interpret network failures as idle playback. Detect a successful empty playback response as a normal idle state; Spotify lists HTTP 204 among the endpoint responses. [Playback responses](https://developer.spotify.com/documentation/web-api/reference/get-information-about-the-users-current-playback)

## 6. Notifications and album art

- Invoke `notify-send` with a subprocess argument list, a timeout, and a checked exit status. Track metadata must never pass through a shell command string.
- Use application name `Spotify`, track title as the summary, and `artist — album` as the body. Escape notification body markup and use sensible fallbacks for missing metadata.
- Prefer an available album image suitable for a desktop notification; tolerate empty image lists.
- Download art only when needed for a notification. Use a Requests session, finite timeouts, a download size limit, and atomic writes into the private application cache.
- Retain a bounded cache, such as the current cover and its source URL. Reuse it for another track sharing the same cover.
- If download or image validation fails, send the notification with the Spotify icon. Never attach stale art from another album.
- On notification dispatch failure, log the reason and keep the track eligible for retry. Retry only if that same track is still playing at the next scheduled poll; if another track is playing, notify the new track and do not queue a stale notification for the previous one. Do not add intermediate polls to retry a notification.

## 7. Failure recovery and logs

| Failure | Planned handling |
| --- | --- |
| Missing configuration, missing `notify-send`, invalid token, or persistent authorization failure | Explain the corrective action and exit with a permanent-failure status |
| HTTP 401 | Refresh authorization once; exit with authentication instructions if still rejected |
| HTTP 403 | Log the access/scope failure and exit; avoid an endless retry loop |
| HTTP 429 | Honor a valid `Retry-After` value; use a conservative fallback if missing or invalid |
| Network timeout or HTTP 5xx | Exponential retry waits of 5, 10, 20, 40, 80, 160, then at most 300 seconds; reset after a successful playback request |
| Malformed successful response | Log a sanitized warning and retry after 300 seconds without discarding track identity |
| Album image failure | Fall back to the application icon |
| Notification process failure | Log; retry on the next scheduled poll only if the same track is still playing. If playback has changed, do not queue a notification for the earlier track. |

Set explicit request timeouts. Coordinate Spotipy retry settings with application retries so nested retries do not multiply requests or conceal rate-limit responses. Spotify explicitly recommends respecting the `Retry-After` header on HTTP 429. [Rate limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits)

Send timestamped logs to standard output/error for journald. Log startup, state transitions, successful notification dispatch, failures, and recovery. Reserve per-poll metadata and calculated delays for debug logging. Do not claim successful display merely because the notification command accepted the request; GNOME may suppress banners through notification settings.

## 8. Systemd user service and installation

1. Document installing `libnotify-bin` on Debian/Ubuntu or `libnotify` on Arch, plus Python virtual environment support.
2. Document creating `~/.local/share/suzumushi/.venv`, installing the selected dependencies, and copying the script to the application directory.
3. Document setting the three environment variables and running the explicit authentication command as the desktop user.
4. Provide a user service whose `ExecStart` uses the installed virtual environment's Python and the installed script via `%h` paths.
5. Use `Restart=on-failure`, a restart delay, and `RestartPreventExitStatus` for permanent configuration/authentication errors. Use unbuffered Python output and a restrictive file-creation mask.
6. Tie service lifecycle to the graphical session where supported and document the desktop D-Bus/session environment needed by `notify-send`. Confirm the target desktop's graphical-session integration before choosing the final enablement target.
7. Explain that shell exports are not automatically inherited by a systemd user service. Provide a concrete session environment import workflow using variable names, followed by a service restart. Explain that imports must be restored after the user manager restarts; optionally document a private systemd `EnvironmentFile` for persistent provisioning if desired.
8. Document copying the unit to `~/.config/systemd/user/`, reloading units, enabling/starting the service, viewing journal logs, restarting, stopping, and uninstalling it.

The script reads the process environment in every launch mode. It does not automatically load `.env` files. `.env.example` documents variable names only.

## 9. Implementation order

1. Create the centralized constants section, configuration parsing, private cache paths, dependency declarations, and explicit OAuth setup.
2. Implement playback normalization, track identity, startup behavior, and adaptive/idle scheduling.
3. Implement subprocess notification dispatch and bounded artwork caching.
4. Add error classification, coordinated retries, logging, and interruptible shutdown.
5. Add the service template and complete installation and troubleshooting documentation.
6. Review the resulting behavior against the acceptance scenarios below before rollout.

## 10. Acceptance scenarios for later verification

The implementation should be evaluated against these scenarios; this planning task does not authenticate Spotify, run the daemon, or exercise the desktop.

- Starting while music A is playing submits exactly one notification for A.
- Repeated observations of A, seeks within A, repeat playback of A, and device transfers of A do not submit duplicates.
- Observing B after A submits one notification for B; observing A again after B submits one for A.
- Pausing or stopping submits no notification and selects a 300-second wait. Resuming the same previously displayed A does not submit another notification.
- Starting idle immediately performs one request and then waits 300 seconds. Once music is detected, active scheduling resumes.
- Episodes, advertisements, null items, and incomplete metadata do not crash the daemon or produce podcast notifications.
- A track with 180 seconds remaining schedules its next playback poll approximately 180 seconds later, accounting for processing time, with no intermediate playback requests.
- Active delays match the documented deadline calculation, including the one-second minimum and 300-second fallback for missing or invalid timing values.
- Changing an individual constants-section setting changes the corresponding scheduling, retry, timeout, cache, or notification behavior without editing function bodies. Credentials continue to come from environment variables.
- Manual skips, seeks, and pauses do not trigger intermediate playback polls; the next scheduled response determines subsequent behavior.
- Missing album art or a failed download still permits a notification with a fallback icon.
- Titles and artist names containing quotes, shell syntax, ampersands, or markup remain literal text.
- Failed notification dispatch leaves the track eligible for retry.
- After a failed dispatch for A, the next scheduled poll retries A only if A is still playing; if B is playing, it notifies B without sending a stale notification for A.
- Rate limits and transient failures use their respective waits and preserve the last notified identity.
- Missing credentials and expired/revoked authorization produce useful instructions without prompting in service mode.
- Service shutdown interrupts both active and idle waits promptly.
- The installed service receives its environment and reaches the graphical notification session; actual visible banners are confirmed on the desktop during rollout.
