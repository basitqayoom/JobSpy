# Scheduled scrape + dashboard

Automates `scrape 3.py` on a configurable interval and shows the latest results
in a static, auto-refreshing web page. Each run produces a **fresh** list
(`web/data.json` is overwritten every time, never merged with older runs).

```
scheduler (launchd/cron)  ->  run_scrape.sh  ->  scrape 3.py  ->  web/data.json
browser  <-  serve.sh (http.server)  <-  web/index.html  <-  web/data.json
```

## Files

| File | Purpose |
| --- | --- |
| `scrape 3.py` | The scrape. Reads `web/config.json`; writes `web/data.json` (and `scrape 3.csv`) each run. |
| `run_scrape.sh` | Runs the scrape once; logs to `scrape.log`. Called by the scheduler. |
| `com.jobspy.scrape.plist` | launchd template; `StartInterval` sets the interval. |
| `server.py` | Serves `web/` and handles saving settings + "run now" (stdlib only). |
| `serve.sh` | Launches `server.py`. |
| `web/index.html` | The dashboard (vanilla HTML/JS, no dependencies). |
| `web/config.json` | Search settings, editable from the page. |
| `web/data.json` | Latest results (regenerated each run). |

## Configure the search from the page

Click **Settings** in the dashboard header to edit the search term, location,
results wanted, and the recency window (hours old / minutes old). Leave hours/
minutes blank to ignore that filter.

- **Save** writes `web/config.json`; it takes effect on the next scheduled run.
- **Save & run now** saves and immediately triggers a scrape so results refresh
  within about a minute.

These settings are read by `scrape 3.py` on every run, so the scheduler always
uses the latest values. (Editing the variables at the top of `scrape 3.py`
still works as the defaults when no config file is present.)

## 1. View the dashboard

The page uses `fetch()`, which browsers block over `file://`, so serve it:

```bash
./serve.sh                 # listens on all interfaces, port 8000
PORT=9000 ./serve.sh       # custom port
HOST=127.0.0.1 ./serve.sh  # restrict to this machine only
```

On startup it prints both a local and a network URL, e.g.:

```
local:   http://localhost:8000
network: http://192.168.29.35:8000
```

The page re-fetches `data.json` every 60 seconds, shows the last-updated time
and job count, lets you filter by company/title/location, and pins the priority
companies at the top (highlighted).

### Access from other devices on the LAN

Open the printed `network:` URL (e.g. `http://192.168.29.35:8000`) from any
phone/laptop on the same Wi-Fi/network.

- The machine's LAN IP can change after a reboot/reconnect; re-run `./serve.sh`
  to see the current one.
- If other devices can't connect, allow incoming connections for Python in
  macOS firewall: System Settings -> Network -> Firewall. (Disabled firewall =
  no prompt needed.)

## 2. Run the scrape manually (optional)

```bash
./run_scrape.sh            # writes web/data.json + scrape 3.csv, logs to scrape.log
```

## 3. Schedule it (configurable interval)

### Option A: launchd (recommended on macOS)

1. Edit `com.jobspy.scrape.plist`:
   - Replace `__PROJECT_DIR__` (3 places) with this folder's absolute path:
     `/Users/iambqc/Desktop/system/JobSpy`
   - Set `StartInterval` (in **seconds**) to your interval:
     `1800` = 30 min, `3600` = 1 hour, `7200` = 2 hours (default).
2. Install and start:

```bash
cp com.jobspy.scrape.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.jobspy.scrape.plist
```

Change the interval later:

```bash
launchctl unload ~/Library/LaunchAgents/com.jobspy.scrape.plist
# edit StartInterval in ~/Library/LaunchAgents/com.jobspy.scrape.plist
launchctl load ~/Library/LaunchAgents/com.jobspy.scrape.plist
```

Stop it:

```bash
launchctl unload ~/Library/LaunchAgents/com.jobspy.scrape.plist
```

### Option B: cron

Run `crontab -e` and add (every 2 hours shown; adjust the schedule for your
interval):

```cron
0 */2 * * * /Users/iambqc/Desktop/system/JobSpy/run_scrape.sh
```

## Tuning

- Keep the scrape's recency window roughly in sync with the interval. In
  `scrape 3.py`, `HOURS_OLD = 2` pairs with a 2-hour interval; for a 30-minute
  interval set `HOURS_OLD = None` and `MINUTES_OLD = 30`.
- Change the search via `SEARCH_TERM` / `LOCATION` / `RESULTS_WANTED` at the top
  of `scrape 3.py`.
- Logs: `scrape.log` (scrape output), `launchd.out.log` / `launchd.err.log`
  (scheduler).
```
