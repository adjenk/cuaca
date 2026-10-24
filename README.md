# cuaca.py

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-GPLv3-blue)

BMKG weather forecasts for **any village/subdistrict in Indonesia** — search by
place name instead of needing to know a region code like `51.04.03.1006`.

## Example

```
$ python cuaca.py --search "ubud"
Found 9 matching locations:
   1. Ubud, Ubud, KAB. GIANYAR, BALI  [51.04.05.1005]
   ...
Pick a number (or 'q' to cancel): 1
Weather Forecast for: Ubud, Ubud, KAB. GIANYAR, BALI
============================================================
🔴 NEXT UPCOMING FORECAST:
   Time        : 2026-08-20 11:00:00
   Weather     : Cerah (Sunny)
   Temperature : 29°C
   Humidity    : 67%
   Wind        : 7.6 km/h from E (112°)
   Rain        : 0 mm
   Cloud cover : 48%
   Visibility  : < 10 km
```

(Some locations don't report visibility; it then shows `not reported`.)

## Setup

```bash
pip install -r requirements.txt
```

Keep `cuaca.py` and `wilayah.csv` in the same folder. `wilayah.csv` is a
database of ~83,000 Indonesian villages/wards (from the open-source project
kodewilayah/permendagri-72-2019) used to turn a name into BMKG's region code.
If this file is missing, the script will download it automatically the first
time you run it. It's git-ignored on purpose — see [How it works](#how-it-works).

## Usage

Search for a location (shows a numbered list to pick from if the name matches
more than one place):
```bash
python cuaca.py --search "gianyar"
python cuaca.py --search "sanur"          # there are 5 "Sanur"s in Indonesia
python cuaca.py --search "ubud, gianyar"  # add the regency name to narrow it down
python cuaca.py --search "kota denpasar sanur"
```

Matching ignores case, accents, and punctuation, and understands "kabupaten" as
the database's `KAB.`. Every word you type must match somewhere in the village,
district, regency/city, or province name. Results whose village name matches
best are listed first.

Search and save the result as your default location:
```bash
python cuaca.py --search "gianyar" --save
```

After that, just run it with no arguments to reuse that default:
```bash
python cuaca.py
```

If you already know the adm4 code:
```bash
python cuaca.py --code 51.04.03.1006 --save
```
`--save` only stores a code after BMKG accepts it, and saves a readable name
(from `wilayah.csv`, or BMKG's own label) rather than the bare code.

Check your currently saved default location:
```bash
python cuaca.py --location
```

Non-interactive mode (auto-picks the top search result — handy for cron jobs):
```bash
python cuaca.py --search "gianyar" --yes
```

Get the full 3-day, 3-hourly forecast (wind speed/direction, cloud cover,
visibility, for every slot) instead of just the next slot + tomorrow morning:
```bash
python cuaca.py --full
python cuaca.py --search "gianyar" --full
```

### Will it rain?

```bash
python cuaca.py --rain            # next 24 hours (default)
python cuaca.py --rain 6          # next 6 hours (1-72)
python cuaca.py --search "sanur" --rain
```

```
🌧  Rain outlook for Ubud, Ubud, KAB. GIANYAR, BALI
   next 24 h · local time there: Thu 20 Aug, 13:30
============================================================
Rain expected from 18:00 (in about 4h 30m), until about 00:00 tomorrow.
Again from 12:00 tomorrow until about 18:00 tomorrow.
Heaviest: Hujan Petir (Thunderstorm) at 12:00 tomorrow

  12:00        Cerah Berawan (Partly Cloudy)  ← now
  15:00        Berawan (Cloudy)
  18:00     🌧 Hujan Ringan (Light Rain)
  21:00     🌧 Hujan Sedang (Moderate Rain)
  ...
```

BMKG's public forecast gives each 3-hour slot a weather *category* (e.g.
"Hujan Sedang") and a precipitation estimate in mm, but no probability of rain.
A slot counts as rainy if its category is a rain/thunder one, **or** its
precipitation estimate is at least 1 mm (`RAIN_MM_THRESHOLD` in the script).
Trace amounts such as 0.1 mm under "Berawan" are shown but not called rain.
Times are accurate to the 3-hour slots, not to the minute, and the feed starts
at the upcoming slot, so there is no "now" slot. Rainy spells that continue
past your window are followed to their end, and the window's total
precipitation is reported.

### One-line output

For status bars, tmux, shell prompts, or a quick glance:

```bash
$ python cuaca.py --oneline
Ubud: 28°C Partly Cloudy, wind 7.6 km/h E | rain 18:00 (in 4h 30m)

$ python cuaca.py --oneline --rain 6     # just the rain part
Ubud: rain 18:00 (in 4h 30m)
```

`--oneline` looks 12 hours ahead for rain (or `--rain HOURS` hours). If the
data is old it says so (`Ubud: forecast out of date`) rather than showing
stale weather as current. Example tmux status line:

```
set -g status-right '#(python3 /path/to/cuaca.py --oneline)'
```

`--oneline` can't be combined with `--json` or `--full`; `--rain` can't be
combined with `--full`.

Get JSON output instead of human-readable text — handy for piping into other
tools or dashboards:
```bash
python cuaca.py --json
python cuaca.py --search "gianyar" --json
python cuaca.py --json --full   # every 3-hourly slot, as JSON
python cuaca.py --location --json | jq
```

`--json` combines with `--search`, `--code`, `--location`, `--full`, `--rain`,
and the default (no-args) mode. With `--rain` the body is a `rain` object
(`will_rain`, `raining_now`, `starts`, `starts_in_minutes`, `ends`, `episodes`,
`heaviest`, `total_precipitation_mm`, and every slot in the window flagged `wet`).
Every slot also carries `precipitation_mm`. When combined with `--search`, it auto-picks the top
match instead of prompting (like `--yes`), since a JSON/piped workflow can't
answer an interactive prompt.

Example (`--json`, short mode):
```json
{
  "location": {
    "label": "Ubud, Ubud, KAB. GIANYAR, BALI",
    "code": "51.04.05.1005"
  },
  "generated_at": "2026-08-20T02:12:03Z",
  "source": "BMKG (Indonesia's Meteorological, Climatological, and Geophysical Agency)",
  "next_forecast": {
    "datetime": "2026-08-20 11:00:00",
    "weather_desc_id": "Cerah",
    "weather_desc_en": "Sunny",
    "temperature_c": 29,
    "humidity_pct": 67,
    "wind_speed_kmh": 7.6,
    "wind_direction": "E",
    "wind_direction_deg": 112,
    "cloud_cover_pct": 48,
    "precipitation_mm": 0,
    "visibility": "< 10 km"
  },
  "tomorrow_morning_forecast": { "...": "..." }
}
```

With `--full`, `next_forecast`/`tomorrow_morning_forecast` are replaced by a
`forecasts` array containing every 3-hourly slot in the same shape.
`generated_at` is in UTC.

With `--json`, errors (no location found, invalid code, BMKG unreachable, no
default saved) come back as `{"error": "..."}` on stdout with exit code 1.
Without it, error messages go to stderr, also with exit code 1.

## Caching

BMKG refreshes its forecasts only twice a day and allows 60 requests per
minute per IP, so downloaded forecasts are saved for 30 minutes and reused.
That makes `--oneline` in a status bar cheap: it only hits BMKG about every
30 minutes.

- `--refresh` ignores the saved copy and downloads a fresh one.
- If BMKG can't be reached, the last saved forecast is shown with a warning
  (and `"offline_fallback": true` under `"cache"` in `--json`) instead of an
  error. Without any saved copy you get the error. An unknown region code is
  always an error, never answered from the cache.
- Saved copies live in `~/.cache/cuaca/` (or `$XDG_CACHE_HOME/cuaca/`;
  set `BMKG_CACHE_DIR` to put them elsewhere). They're small and safe to delete.
  If that folder can't be written, the script just works without a cache.

## Time and stale data

BMKG forecast times are in the location's own timezone (WIB, WITA, or WIT).
"Next upcoming forecast" is the first slot at or after the *current time in
that location's timezone*, and "tomorrow" means tomorrow there too, regardless
of where your computer is. BMKG refreshes its data twice a day; if every slot
is already in the past the script shows the latest one with a warning (and a
`"warning"` field in `--json` mode).

## Weather term translations

BMKG's `weather_desc` field comes back in Indonesian. The English shown next to
it is BMKG's own `weather_desc_en` (e.g. "Berawan" → "Mostly Cloudy"). For slots
without one, a small built-in table is used as a fallback, and anything not in
it is left untranslated rather than guessed.

## How it works

- BMKG's API only accepts a region code (`adm4`, format `PP.KK.CC.DDDD`), not a
  place name.
- `wilayah.csv` maps code <-> name at every level (province, regency/city,
  district, village) using the exact same codes BMKG expects, so a local search
  match plugs straight into the BMKG API.
- Forecasts are cached on disk for 30 minutes (see [Caching](#caching)).
- Your default location is saved in `config.json` (created automatically next
  to the script). Both `config.json` and `wilayah.csv` are listed in
  `.gitignore` since they're either personal (your saved location) or
  regenerable (the region database), not project source.

## Data sources & attribution

- Weather data: **BMKG** (Badan Meteorologi, Klimatologi, dan Geofisika —
  Indonesia's Meteorological, Climatological, and Geophysical Agency),
  attribution required by BMKG's terms of use.
- Region codes: **kodewilayah/permendagri-72-2019**, derived from Indonesia's
  Permendagri No. 72/2019 government regulation. The upstream repo's own
  license terms aren't clearly stated, so this project only downloads the CSV
  at runtime rather than bundling/redistributing it — check the upstream repo
  yourself before relying on it for anything beyond personal use.

## Tests

The tests run offline (a small fixture region file and a fake BMKG response):

```bash
pip install pytest
python -m pytest tests
```

## Contributing

Issues and PRs welcome.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE). Copyright (C) 2026 Adam Jenkins.

In short: you're free to use, modify, and share this program. If you distribute
a modified version (including selling it), you must keep the copyright and
license notices, say that you changed it, and make your version's complete
source available under the same license. (This summary isn't legal text; the
LICENSE file is what counts.) The BMKG attribution requirement for the weather
data is separate and still applies to anything you build on it.

