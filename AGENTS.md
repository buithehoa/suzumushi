# Spotify Web API to Libnotify Notifier Setup

Architecture: A background daemon polls Spotify Web API (`GET /v1/me/player`) to detect active playback changes across any device (phone, speaker, desktop), downloads album art, and triggers GNOME desktop notifications via `libnotify`. It is managed as a `systemd --user` service.

---

## 1. Prerequisites

### System Packages & Python Dependencies
```bash
# Ubuntu / Debian
sudo apt install libnotify-bin python3-pip

# Arch Linux
sudo pacman -S libnotify python-pip

# Install Python modules
pip install --user spotipy requests
```

### Spotify Developer Credentials
1. Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Create an App.
3. In App Settings, add Redirect URI: `http://127.0.0.1:9090`.
4. Note your **Client ID** and **Client Secret**.

---

## 2. Daemon Script

Create `~/.local/bin/spotify_notifier.py`:

```python
#!/usr/bin/env python3
import os
import sys
import time
import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth

CLIENT_ID = "YOUR_CLIENT_ID"
CLIENT_SECRET = "YOUR_CLIENT_SECRET"
REDIRECT_URI = "http://127.0.0.1:9090"
SCOPE = "user-read-playback-state"

COVER_CACHE = "/tmp/spotify_current_art.jpg"

def get_spotify_client():
    cache_path = os.path.expanduser("~/.cache/spotify_token_cache.json")
    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            scope=SCOPE,
            cache_path=cache_path,
            open_browser=False
        )
    )

def dispatch_notification(title, artist, album, cover_url):
    icon_arg = "-i spotify"
    if cover_url:
        try:
            res = requests.get(cover_url, timeout=4)
            if res.status_code == 200:
                with open(COVER_CACHE, "wb") as f:
                    f.write(res.content)
                icon_arg = f"-i {COVER_CACHE}"
        except Exception:
            pass

    safe_title = title.replace('"', '\\"')
    safe_body = f"{artist} — {album}".replace('"', '\\"')
    os.system(f'notify-send {icon_arg} -a "Spotify" "{safe_title}" "{safe_body}"')

def main():
    sp = get_spotify_client()
    last_track_id = None

    while True:
        try:
            playback = sp.current_playback()
            if not playback or not playback.get("is_playing"):
                time.sleep(8)
                continue

            item = playback.get("item")
            if not item:
                time.sleep(5)
                continue

            track_id = item.get("id")
            if track_id != last_track_id:
                last_track_id = track_id
                title = item.get("name", "Unknown Title")
                artists = ", ".join([a["name"] for a in item.get("artists", [])])
                album = item.get("album", {}).get("name", "")
                images = item.get("album", {}).get("images", [])
                cover_url = images[0]["url"] if images else None

                dispatch_notification(title, artists, album, cover_url)

            # Adaptive poll: sleep based on remaining track duration (max 5s to catch manual skips)
            duration_ms = item.get("duration_ms", 0)
            progress_ms = playback.get("progress_ms", 0)
            remaining_s = max(2, (duration_ms - progress_ms) / 1000)
            time.sleep(min(remaining_s, 5))

        except Exception as err:
            time.sleep(5)

if __name__ == "__main__":
    main()
```

Make the script executable:
```bash
chmod +x ~/.local/bin/spotify_notifier.py
```

---

## 3. Initial Authentication

Run once interactively in a terminal to authenticate OAuth:
```bash
python3 ~/.local/bin/spotify_notifier.py
```
Paste the URL requested into your browser and copy back the redirected URL to authorize. The token cache will be stored in `~/.cache/spotify_token_cache.json`.

---

## 4. Systemd User Service Setup

Create `~/.config/systemd/user/spotify-notifier.service`:

```ini
[Unit]
Description=Spotify Libnotify Background Bridge
After=graphical-session.target

[Service]
Type=simple
ExecStart=%h/.local/bin/spotify_notifier.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

Enable and start the daemon:
```bash
systemctl --user daemon-reload
systemctl --user enable --now spotify-notifier.service
```

### Management Commands
```bash
# Check daemon status
systemctl --user status spotify-notifier.service

# View live execution logs
journalctl --user -u spotify-notifier.service -f

# Restart daemon
systemctl --user restart spotify-notifier.service
```
