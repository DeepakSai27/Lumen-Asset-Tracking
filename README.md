# Lumen Asset Tracking

BLE presence and attendance system. Each employee carries an off-the-shelf BLE tag.
ESP32-C5 **checkpoints** around the office hear the tags and report them over Wi-Fi to a
small **server**. The server tracks who is in, when they arrived and left, and how long
they were present. A website shows this live and as daily attendance.

```
 [BLE tag] ))) adverts )))  [ESP32-C5 checkpoint]  --Wi-Fi, HTTP POST every 5 s-->  [Server: FastAPI + SQLite]  <-- browser
 (employee)                  scans for tags + iBeacon                                presence engine, website, CSV
```

| Folder | What |
|---|---|
| `Firmware/Checkpoint` | ESP-IDF 5.5 project for the ESP32-C5 (scanner + iBeacon + Wi-Fi uploader) |
| `Server` | Python server: checkpoint API, presence engine, website, CSV export |

## How presence and duration are worked out

* A checkpoint reports each tag it heard in the last 5 s, with the strongest RSSI and how long ago it was heard.
* While sightings keep arriving, the employee has an open **session**. Gaps shorter than the **absence
  timeout** (default 3 min) are bridged.
* When nothing has been heard for longer than the timeout, the session closes **at the last sighting**,
  so a timeout never adds time.
* **Attendance** for a day = first in, last out and the total time across that day's sessions.
  A session that runs past midnight is split between the two days.
* **Location** = the checkpoint that hears the tag strongest, with 3 dB of hysteresis so it doesn't flicker between rooms.
* Sightings weaker than `LUMEN_MIN_RSSI` (default −90 dBm) are ignored.

## Server

Requires Python 3.10+.

```powershell
cd Server
.\run.ps1                                  # http://<pc-ip>:8000
```

`run.ps1` reads settings from `Server\lumen.env` (one `KEY=value` per line, git-ignored).
`LUMEN_API_TOKEN` there must match the token in the firmware's `sdkconfig`.

| Env variable | Default | Meaning |
|---|---|---|
| `LUMEN_API_TOKEN` | `change-me` | Shared secret the checkpoints send (set in `lumen.env`) |
| `LUMEN_ADMIN_PASSWORD` | *(none)* | If set, the website asks for it (HTTP Basic, any user name) |
| `LUMEN_ABSENCE_TIMEOUT` | `180` | Seconds without a sighting before someone counts as gone |
| `LUMEN_MIN_RSSI` | `-90` | Ignore weaker sightings (dBm) |
| `LUMEN_RETENTION_DAYS` | `30` | Raw sightings kept for this long (sessions are kept forever) |
| `LUMEN_DB` | `Server/data/lumen.db` | SQLite database file |

Allow inbound TCP 8000 in Windows Firewall so the checkpoints can reach the PC.

**Try it without hardware:** with the server running, `.\.venv\Scripts\python.exe tools\simulate.py`
creates three demo employees and two simulated checkpoints.

**Tests:** `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt` then `.\.venv\Scripts\python.exe -m pytest -q`.

### Website

* **Live**: who is present, where, since when, and today's total.
* **Attendance**: pick a day to see each person's first in, last out, total time and a day timeline. Click
  a row to see their sessions. Export any date range as CSV.
* **People & Tags**: add employees. To register a tag, click **Scan for new tags**, hold the tag next to a
  checkpoint, and assign the strongest entry to the employee.
* **Checkpoints**: checkpoints appear on their own after their first report. Give each a location name.

## Firmware (ESP32-C5)

ESP-IDF v5.5 is installed under `C:\esp\v5.5.2`. ESP-IDF cannot build from a path with spaces
or brackets, so always use `build.ps1`. It mirrors the sources to `C:\esp-build\Lumen-Checkpoint`,
builds there, and copies `sdkconfig` back here. Edit the code here, never in the mirror.

```powershell
cd Firmware\Checkpoint
.\build.ps1 menuconfig                # "Lumen Checkpoint" menu: Wi-Fi, server URL, token, name, iBeacon IDs
.\build.ps1                           # build
.\build.ps1 -p COM7 flash monitor     # flash and watch the log
```

Settings in the **Lumen Checkpoint** menu:

* Wi-Fi SSID / password, **Server URL** (e.g. `http://192.168.1.10:8000`), **API token**
* Checkpoint name: the initial name on the website (it can be renamed there)
* Report interval (5 s), minimum RSSI, discovery RSSI (−65 dBm: only a tag held close shows up as "new")
* iBeacon advertising: UUID, major (site), minor (checkpoint number), measured power, interval

Behaviour: passive scan at 50 % duty to share the radio with Wi-Fi. iBeacon frames are always reported.
Other devices are reported only if their MAC is registered, or during "scan for new tags". The server
sends the list of registered MACs back in every reply.

## Choosing tags

* Pick tags with a **static MAC address** (most iBeacon/Eddystone key-fobs and cards). Phone-style
  randomised MACs cannot be matched.
* Tags are matched by MAC by default. Matching by iBeacon UUID/major/minor is also supported
  (add the tag manually), but only works if each tag has been given a **unique** major/minor.
  Cheap fobs often ship with identical factory IDs.
* An advertising interval of 1 s or faster and a TX power of about 0 dBm work well. Battery life is usually 1–2 years.

## Notes / next steps

* Traffic between checkpoints and server is plain HTTP on the LAN with a bearer token. Use HTTPS if it ever leaves the LAN.
* Tune `LUMEN_ABSENCE_TIMEOUT` and `LUMEN_MIN_RSSI` after a walk test in the real office.
