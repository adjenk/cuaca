#!/usr/bin/env python3
# Copyright (C) 2026 Adam Jenkins
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file or
# <https://www.gnu.org/licenses/> for details.
"""
cuaca.py - BMKG weather forecasts for any village/subdistrict in Indonesia.

Instead of manually looking up a region code (adm4) like 51.04.03.1006, you can
search for a location by name, and (optionally) save it as your default location
so next time you can just run the script with no arguments.

Examples:
    python cuaca.py --search "gianyar"
    python cuaca.py --search "sanur" --save
    python cuaca.py --code 51.04.03.1006
    python cuaca.py --location
    python cuaca.py
    python cuaca.py --rain          # will it rain in the next 24 hours?
    python cuaca.py --oneline       # one short line, for status bars

The region-code lookup data (province / regency / district / village) comes
from the open-source project kodewilayah/permendagri-72-2019 (based on
Indonesia's Kepmendagri administrative codes), which uses the exact same
adm4 code format that BMKG's API expects.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from collections import namedtuple
from functools import lru_cache
from datetime import datetime, timedelta, timezone

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WILAYAH_CSV = os.path.join(SCRIPT_DIR, "wilayah.csv")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
WILAYAH_CSV_URL = (
    "https://raw.githubusercontent.com/kodewilayah/permendagri-72-2019/"
    "main/dist/base.csv"
)
BMKG_URL = "https://api.bmkg.go.id/publik/prakiraan-cuaca"
ADM4_RE = re.compile(r"^\d{2}\.\d{2}\.\d{2}\.\d{4}$")


class BmkgError(Exception):
    """A user-facing error (bad input, network failure, unusable BMKG response)."""


class BmkgNetworkError(BmkgError):
    """BMKG couldn't be reached or answered badly (as opposed to "no such region").
    Only these errors fall back to an old cached forecast."""

# Fallback Indonesian -> English translations for BMKG's weather_desc field.
# BMKG's own weather_desc_en is used whenever a slot has one (it's authoritative,
# e.g. "Berawan" is "Mostly Cloudy"); this table only covers slots without it.
# Anything not in here is left untranslated rather than guessed.
WEATHER_TRANSLATIONS = {
    "Cerah": "Sunny",
    "Cerah Berawan": "Partly Cloudy",
    "Berawan": "Mostly Cloudy",
    "Berawan Tebal": "Overcast",
    "Udara Kabur": "Hazy",
    "Asap": "Smoke",
    "Kabut": "Fog",
    "Hujan Ringan": "Light Rain",
    "Hujan Sedang": "Moderate Rain",
    "Hujan Lebat": "Heavy Rain",
    "Hujan Local": "Local Rain",
    "Hujan Lokal": "Local Rain",
    "Hujan Petir": "Thunderstorm",
    "Hujan Petir Kencang": "Severe Thunderstorm",
    "Petir": "Thunder",
    "Angin Kencang": "Strong Wind",
    "Udara Dingin": "Cold",
    "Suhu Dingin": "Cold Temperature",
}


def weather_en(desc, english=None):
    """English description: BMKG's own if given, else our fallback table, else None."""
    if english and str(english).strip():
        return str(english).strip()
    if not desc:
        return None
    return WEATHER_TRANSLATIONS.get(desc.strip())


def translate_weather(desc, english=None):
    """Return 'Berawan (Mostly Cloudy)' style label; falls back to the raw text if unknown."""
    if not desc:
        return desc
    en = weather_en(desc, english)
    return f"{desc} ({en})" if en else desc


# --------------------------------------------------------------------------
# Region database (name <-> adm4 code)
# --------------------------------------------------------------------------

def ensure_wilayah_csv():
    """Download the region-code database once, then cache it locally."""
    if os.path.exists(WILAYAH_CSV):
        return
    print("Downloading Indonesia region-code database (one-time, ~2 MB)...", file=sys.stderr)
    try:
        r = requests.get(WILAYAH_CSV_URL, timeout=30)
        r.raise_for_status()
        with open(WILAYAH_CSV, "wb") as f:
            f.write(r.content)
        print("Done.\n", file=sys.stderr)
    except requests.exceptions.RequestException as e:
        raise BmkgError(
            f"Failed to download the region database: {e}\n"
            f"Download it manually from:\n  {WILAYAH_CSV_URL}\n"
            f"and save it as:\n  {WILAYAH_CSV}"
        )


def load_wilayah(path=None):
    """
    Load wilayah.csv into a searchable structure.

    Code format: PP[.KK[.CC[.DDDD]]]
      PP   = province (2 digits)
      KK   = regency/city
      CC   = district (kecamatan)
      DDDD = village/urban ward (desa/kelurahan)  <- this is BMKG's `adm4` param
    """
    if path is None:
        ensure_wilayah_csv()
        path = WILAYAH_CSV
    provinsi, kabupaten, kecamatan = {}, {}, {}
    desa = []  # list of (code, name)
    with open(path, encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 2:
                continue
            code, name = row[0].strip(), row[1].strip()
            depth = code.count(".")
            if depth == 0:
                provinsi[code] = name
            elif depth == 1:
                kabupaten[code] = name
            elif depth == 2:
                kecamatan[code] = name
            elif depth == 3:
                desa.append((code, name))
    return provinsi, kabupaten, kecamatan, desa


def full_path(code, provinsi, kabupaten, kecamatan, desa_name):
    parts = code.split(".")
    prov_code = parts[0]
    kab_code = ".".join(parts[:2])
    kec_code = ".".join(parts[:3])
    return {
        "village": desa_name,
        "district": kecamatan.get(kec_code, "?"),
        "regency": kabupaten.get(kab_code, "?"),
        "province": provinsi.get(prov_code, "?"),
    }


@lru_cache(maxsize=None)
def normalize(s):
    """Lowercase, strip accents, drop apostrophes, turn other punctuation into spaces."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\'\u2019`]", "", s.lower())
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


# Words people type that differ from how the region database spells them.
_QUERY_ALIASES = {"kabupaten": "kab", "kota": "kota", "kotamadya": "kota"}


def query_terms(query):
    return [_QUERY_ALIASES.get(t, t) for t in normalize(query).split()]


def search_location(query, provinsi, kabupaten, kecamatan, desa):
    """
    Search villages by name, district, regency/city, or province.

    Every word in the query must appear somewhere in "village district regency
    province". Punctuation is ignored, so "ubud, gianyar" works, and
    "kabupaten" is understood as the database's "KAB.".

    Ranking: an exact village-name match first; then by how well each query word
    matches (whole word in the village name > whole word in the district/regency/
    province > mere substring, e.g. "ubud" inside "Wubudu").
    """
    terms = query_terms(query)
    if not terms:
        return []
    whole_query = " ".join(terms)
    results = []
    for code, name in desa:
        path = full_path(code, provinsi, kabupaten, kecamatan, name)
        name_n = normalize(name)
        rest_n = normalize(f"{path['district']} {path['regency']} {path['province']}")
        haystack = f"{name_n} {rest_n}"
        if not all(t in haystack for t in terms):
            continue
        name_words, rest_words = set(name_n.split()), set(rest_n.split())
        score = sum(
            3 if t in name_words else 2 if t in rest_words else 1 for t in terms
        )
        results.append((name_n != whole_query, -score, code, path))
    results.sort(key=lambda r: r[:3])
    return [(code, path) for (_, _, code, path) in results]


def choose_location(matches, auto_first=False):
    if not matches:
        return None
    if len(matches) == 1 or auto_first:
        return matches[0]

    print(f"\nFound {len(matches)} matching locations:")
    shown = matches[:20]
    for i, (code, path) in enumerate(shown, 1):
        print(
            f"  {i:2d}. {path['village']}, {path['district']}, "
            f"{path['regency']}, {path['province']}  [{code}]"
        )
    if len(matches) > 20:
        print(f"      ...and {len(matches) - 20} more. Try narrowing your search.")

    while True:
        try:
            choice = input("\nPick a number (or 'q' to cancel): ").strip()
        except EOFError:  # stdin closed, e.g. piped; treat as cancel
            return None
        if choice.lower() == "q":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(shown):
            return shown[int(choice) - 1]
        print("Invalid input, try again.")


# --------------------------------------------------------------------------
# Config (default location)
# --------------------------------------------------------------------------

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg if isinstance(cfg, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not read {CONFIG_PATH} ({e}); ignoring it.", file=sys.stderr)
    return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# Fetching & printing the forecast
# --------------------------------------------------------------------------

SOURCE = "BMKG (Indonesia's Meteorological, Climatological, and Geophysical Agency)"


def get_forecast_data(adm4_code):
    """Fetch BMKG data and return (label_from_api, flat_forecasts). Raises BmkgError."""
    if not ADM4_RE.match(adm4_code):
        raise BmkgError(
            f"'{adm4_code}' is not a valid region code. Expected the format "
            "PP.KK.CC.DDDD, e.g. 51.04.03.1006. Use --search to find one by name."
        )
    try:
        response = requests.get(BMKG_URL, params={"adm4": adm4_code}, timeout=15)
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (400, 404):
            raise BmkgError(f"BMKG has no forecast for region code {adm4_code}.")
        raise BmkgNetworkError(f"BMKG returned an error: {e}")
    except requests.exceptions.RequestException as e:
        raise BmkgNetworkError(f"Failed to fetch data from BMKG: {e}")
    try:
        data = response.json()
    except ValueError:
        raise BmkgNetworkError("BMKG returned an unreadable (non-JSON) response. Try again shortly.")

    if not isinstance(data, dict):
        raise BmkgError(f"Unexpected response shape from BMKG for adm4={adm4_code}.")

    lokasi = data.get("lokasi") or {}
    api_label = ", ".join(
        str(lokasi[k]) for k in ("desa", "kecamatan", "kotkab", "provinsi") if lokasi.get(k)
    )

    data_entries = data.get("data") or []
    first_entry = data_entries[0] if data_entries else {}
    forecast_list = first_entry.get("cuaca", []) if isinstance(first_entry, dict) else []
    flat_forecasts = (
        [item for sublist in forecast_list for item in sublist]
        if forecast_list and isinstance(forecast_list[0], list)
        else forecast_list
    )
    if not api_label and not flat_forecasts:
        raise BmkgError(f"BMKG returned no data for region code {adm4_code}.")
    return api_label or adm4_code, flat_forecasts


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
# BMKG refreshes its forecasts only twice a day and allows 60 requests/minute
# per IP, so there's no point re-downloading the same forecast every run.
# Parsed forecasts are kept on disk for CACHE_TTL_SECONDS. If BMKG can't be
# reached, an older cached copy is shown (with a warning) instead of failing.

CACHE_TTL_SECONDS = 30 * 60
CACHE_VERSION = 1

Forecast = namedtuple("Forecast", "label slots cache_age offline")
# cache_age: seconds since the data was fetched when it came from the cache, else None
# offline:   True if BMKG was unreachable and an expired cached copy is being shown


def cache_dir():
    """~/.cache/cuaca (or $XDG_CACHE_HOME/cuaca); $BMKG_CACHE_DIR overrides."""
    override = os.environ.get("BMKG_CACHE_DIR")
    if override:
        return override
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "cuaca")


def _cache_path(adm4_code):
    return os.path.join(cache_dir(), f"forecast_{adm4_code}.json")


def _read_cache(adm4_code):
    """Return the cached entry dict, or None if missing/corrupt. Never raises."""
    try:
        with open(_cache_path(adm4_code), encoding="utf-8") as f:
            entry = json.load(f)
        if (
            entry.get("version") == CACHE_VERSION
            and isinstance(entry.get("fetched_at"), (int, float))
            and isinstance(entry.get("slots"), list)
            and entry["slots"]
        ):
            entry["label"] = entry.get("label") or adm4_code
            return entry
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _write_cache(adm4_code, label, slots, now):
    """Best-effort atomic write; a read-only disk must never break the forecast."""
    if not slots:
        return
    path = _cache_path(adm4_code)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": CACHE_VERSION, "fetched_at": now,
                       "label": label, "slots": slots}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def load_forecast(adm4_code, refresh=False, now=None):
    """
    Forecast for a region: fresh cache if available, else BMKG (then cached).
    refresh=True skips reading the cache. If BMKG is unreachable, falls back to
    an expired cached copy when there is one. Raises BmkgError otherwise.
    """
    now = time.time() if now is None else now
    cached = _read_cache(adm4_code)
    if cached and not refresh:
        age = max(0.0, now - cached["fetched_at"])
        if age < CACHE_TTL_SECONDS:
            return Forecast(cached["label"], cached["slots"], age, False)
    try:
        label, slots = get_forecast_data(adm4_code)
    except BmkgNetworkError:
        if cached:
            return Forecast(cached["label"], cached["slots"],
                            max(0.0, now - cached["fetched_at"]), True)
        raise
    _write_cache(adm4_code, label, slots, now)
    return Forecast(label, slots, None, False)


def human_duration(seconds):
    """3 -> '3 s', 300 -> '5 min', 5400 -> '1h 30m', 90000 -> '1d 1h'."""
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins:02d}m" if mins else f"{hours}h"
    days, hrs = divmod(hours, 24)
    return f"{days}d {hrs}h" if hrs else f"{days}d"


def cache_notes(fc):
    """(top_warning, footer) lines describing where the data came from."""
    if fc.offline:
        return (f"\u26a0\ufe0f  Couldn't reach BMKG; showing a saved forecast from "
                f"{human_duration(fc.cache_age)} ago.", None)
    if fc.cache_age is not None and fc.cache_age >= 60:
        return None, f"(Saved copy from {human_duration(fc.cache_age)} ago; add --refresh to re-download.)"
    return None, None


def _add_warning(result, msg):
    result["warning"] = f"{result['warning']} {msg}" if result.get("warning") else msg


def _add_issued(result, slots):
    issued = forecast_issued(slots)
    if issued is not None:
        result["forecast_issued_utc"] = issued.strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_cache_info(result, fc):
    if fc.cache_age is not None:
        result["cache"] = {"age_seconds": int(fc.cache_age), "offline_fallback": fc.offline}
    if fc.offline:
        _add_warning(result, "BMKG could not be reached; this is an older saved forecast.")


def parse_local_dt(block):
    return datetime.strptime(block["local_datetime"], "%Y-%m-%d %H:%M:%S")


def parse_utc_dt(block):
    """Naive UTC datetime of a slot, or None if BMKG didn't supply a parsable one."""
    raw = block.get("utc_datetime")
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except (TypeError, ValueError):
            continue
    return None


def region_now(flat_forecasts, now_utc=None):
    """
    The current time *in the forecast location's own timezone* (WIB/WITA/WIT).

    BMKG's local_datetime is region-local, so comparing it with the machine's
    clock is wrong unless you happen to be in the same zone. Each slot carries
    both a UTC and a local timestamp, so their difference gives the UTC offset.
    Falls back to the machine's local time if that info is missing.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    for block in flat_forecasts:
        utc = parse_utc_dt(block)
        if utc is not None:
            try:
                return now_utc + (parse_local_dt(block) - utc)
            except (KeyError, ValueError):
                continue
    return datetime.now()


def forecast_issued(slots):
    """When BMKG's model run behind this forecast was issued (naive UTC), or None."""
    for block in slots:
        raw = block.get("analysis_date")
        if raw:
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
                try:
                    return datetime.strptime(raw, fmt)
                except (TypeError, ValueError):
                    continue
        break
    return None


def issued_note(slots, now_utc=None):
    """'Forecast issued 26 Oct 00:00 UTC (11h 23m ago).' or None if BMKG didn't say."""
    issued = forecast_issued(slots)
    if issued is None:
        return None
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    age = human_duration((now_utc - issued).total_seconds())
    return f"BMKG forecast issued {issued:%d %b %H:%M} UTC ({age} ago)."


def pick_next_forecast(flat_forecasts, now_local):
    """
    First slot at or after `now_local`. Returns (block, is_stale). If every slot is
    already in the past (BMKG data not refreshed), returns the last slot, stale=True.
    """
    for block in flat_forecasts:
        if parse_local_dt(block) >= now_local:
            return block, False
    return flat_forecasts[-1], True


def _fmt(v):
    return "?" if v is None else str(v)


def format_block(block, indent="   "):
    """One forecast slot with every detail BMKG gives us."""
    wind = f"{_fmt(block.get('ws'))} km/h from {_fmt(block.get('wd'))} ({_fmt(block.get('wd_deg'))}\u00b0)"
    lines = [
        f"{indent}Time        : {block.get('local_datetime')}",
        f"{indent}Weather     : {translate_weather(block.get('weather_desc'), block.get('weather_desc_en')) or 'not reported'}",
        f"{indent}Temperature : {_fmt(block.get('t'))}\u00b0C",
        f"{indent}Humidity    : {_fmt(block.get('hu'))}%",
        f"{indent}Wind        : {wind}",
        *( [f"{indent}Rain        : {precip_mm(block):g} mm"] if precip_mm(block) is not None else [] ),
        f"{indent}Cloud cover : {_fmt(block.get('tcc'))}%",
        f"{indent}Visibility  : {block.get('vs_text') or 'not reported'}",
    ]
    return "\n".join(lines)


def closest_to_time(flat_forecasts, target_date, target_hour):
    """Find the forecast slot on target_date closest to target_hour (0-23)."""
    target = datetime.combine(target_date, datetime.min.time()).replace(hour=target_hour)
    same_day = [b for b in flat_forecasts if parse_local_dt(b).date() == target_date]
    if not same_day:
        return None
    return min(same_day, key=lambda b: abs((parse_local_dt(b) - target).total_seconds()))


def print_full_forecast(flat_forecasts):
    """Print every 3-hourly slot for the whole period, grouped by day."""
    current_day = None
    for block in flat_forecasts:
        dt = parse_local_dt(block)
        if dt.date() != current_day:
            current_day = dt.date()
            print(f"\n\U0001F4C5 {current_day.strftime('%A, %d %B %Y')}")
            print("-" * 60)
        time_str = dt.strftime("%H:%M")
        wind = f"{_fmt(block.get('ws'))} km/h {_fmt(block.get('wd'))}"
        weather = translate_weather(block.get("weather_desc"), block.get("weather_desc_en")) or "?"
        print(
            f"  {time_str}  {weather:<30}"
            f" {_fmt(block.get('t')):>3}\u00b0C  hum {_fmt(block.get('hu')):>3}%"
            f"  wind {wind:<12}  cloud {_fmt(block.get('tcc')):>3}%"
            f"  rain {_fmt(precip_mm(block)):>4}mm"
            f"  vis {_fmt(block.get('vs_text'))}"
        )


def block_to_dict(block):
    """One forecast slot as a clean, machine-readable dict."""
    return {
        "datetime": block.get("local_datetime"),
        "weather_desc_id": block.get("weather_desc"),
        "weather_desc_en": weather_en(block.get("weather_desc"), block.get("weather_desc_en")),
        "temperature_c": block.get("t"),
        "humidity_pct": block.get("hu"),
        "wind_speed_kmh": block.get("ws"),
        "wind_direction": block.get("wd"),
        "wind_direction_deg": block.get("wd_deg"),
        "cloud_cover_pct": block.get("tcc"),
        "precipitation_mm": precip_mm(block),
        "visibility": block.get("vs_text"),
    }


STALE_WARNING = (
    "All forecast slots from BMKG are in the past, so this data looks out of date "
    "(BMKG refreshes twice a day). Showing the most recent slot."
)


def build_forecast_json(adm4_code, display_name=None, full=False, refresh=False):
    """Build a JSON-serializable dict of the forecast, mirroring the printed output."""
    fc = load_forecast(adm4_code, refresh=refresh)
    flat_forecasts = fc.slots
    label = display_name or fc.label

    result = {
        "location": {"label": label, "code": adm4_code},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": SOURCE,
    }
    _add_cache_info(result, fc)
    _add_issued(result, flat_forecasts)

    if not flat_forecasts:
        result["forecasts"] = []
        return result

    if full:
        result["forecasts"] = [block_to_dict(b) for b in flat_forecasts]
        return result

    now_local = region_now(flat_forecasts)
    next_block, stale = pick_next_forecast(flat_forecasts, now_local)
    tomorrow_morning = closest_to_time(flat_forecasts, now_local.date() + timedelta(days=1), 8)

    result["next_forecast"] = block_to_dict(next_block)
    result["tomorrow_morning_forecast"] = (
        block_to_dict(tomorrow_morning) if tomorrow_morning else None
    )
    if stale:
        _add_warning(result, STALE_WARNING)
    return result


def fetch_and_print_weather(adm4_code, display_name=None, full=False, refresh=False):
    """Print the forecast and return the location label that was shown."""
    fc = load_forecast(adm4_code, refresh=refresh)
    flat_forecasts = fc.slots
    label = display_name or fc.label
    top_note, footer_note = cache_notes(fc)
    if top_note:
        print(top_note + "\n")
    print(f"Weather Forecast for: {label}")
    print("=" * 60)

    if not flat_forecasts:
        print("No forecast data available.")
        return label

    if full:
        print_full_forecast(flat_forecasts)
        print(f"\nSource: {SOURCE}")
        if footer_note:
            print(footer_note)
        return label

    now_local = region_now(flat_forecasts)
    next_block, stale = pick_next_forecast(flat_forecasts, now_local)
    if stale:
        print(f"\u26a0\ufe0f  {STALE_WARNING}\n")
    print("\U0001F534 NEXT UPCOMING FORECAST:")
    print(format_block(next_block))
    print("-" * 60)

    tomorrow_morning = closest_to_time(flat_forecasts, now_local.date() + timedelta(days=1), 8)
    if tomorrow_morning:
        print("\U0001F305 TOMORROW MORNING FORECAST:")
        print(format_block(tomorrow_morning))
    else:
        print("\U0001F305 TOMORROW MORNING FORECAST: Data not available yet.")

    print(
        "\n(Tip: add --full to see the complete 3-day, 3-hourly forecast "
        "with wind, cloud cover, and visibility for every slot.)"
    )
    print(f"\nSource: {SOURCE}")
    if issued_note(flat_forecasts):
        print(issued_note(flat_forecasts))
    if footer_note:
        print(footer_note)
    return label


# --------------------------------------------------------------------------
# Rain outlook (--rain) and one-line summary (--oneline)
# --------------------------------------------------------------------------
# BMKG's public forecast gives a weather *category* per 3-hour slot (e.g. "Hujan
# Sedang"), not a probability or rainfall amount, so "rain" here means "BMKG
# forecasts a rainy/thundery category for that slot".

SLOT = timedelta(hours=3)
DATETIME_FMT = "%Y-%m-%d %H:%M:%S"
# 48, not 24: "will it rain?" almost always means tonight *and* tomorrow. A 24-hour
# window run in the evening stops mid-afternoon tomorrow and misses evening rain.
DEFAULT_RAIN_HOURS = 48
MAX_RAIN_HOURS = 72
ONELINE_RAIN_HOURS = 12


# BMKG's `tp` is a precipitation estimate in mm. A slot also counts as rainy when
# it reaches this amount even if its category isn't a rain one (BMKG reports
# trace amounts like 0.1 mm under plain "Berawan", which we don't call rain).
RAIN_MM_THRESHOLD = 1.0


def is_wet(desc):
    """Is this weather category (weather_desc) a rainy/thundery one?"""
    d = (desc or "").lower()
    return "hujan" in d or "petir" in d


def precip_mm(block):
    """Precipitation in mm for a slot, or None if BMKG didn't give a usable number."""
    try:
        v = block.get("tp")
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def slot_is_wet(block):
    mm = precip_mm(block)
    return is_wet(block.get("weather_desc")) or (mm is not None and mm >= RAIN_MM_THRESHOLD)


def rain_rank(desc):
    """Rough severity so we can name the 'heaviest' slot."""
    d = (desc or "").lower()
    if "kencang" in d:
        return 6
    if "petir" in d:
        return 5
    if "lebat" in d:
        return 4
    if "sedang" in d:
        return 3
    if "lokal" in d or "local" in d:
        return 2
    return 1


def _sorted_slots(slots):
    return sorted(slots, key=parse_local_dt)


def current_slot_index(slots, now_local):
    """Index of the slot in effect now (latest slot <= now, less than 3h old), else None."""
    best = None
    for i, b in enumerate(slots):
        dt = parse_local_dt(b)
        if dt <= now_local:
            best = i if now_local - dt < SLOT else None
        else:
            break
    return best


def analyze_rain(slots, now_local, hours):
    """
    Will it rain in the next `hours` hours? Pure function: slots + the current
    time in the forecast region -> a dict (also used as the --rain JSON body).
    `slots` must be BMKG slot dicts; they are sorted defensively.
    """
    slots = _sorted_slots(slots)
    n = len(slots)
    horizon = now_local + timedelta(hours=hours)
    cur = current_slot_index(slots, now_local)
    idxs = ([cur] if cur is not None else []) + [
        i for i, b in enumerate(slots) if now_local < parse_local_dt(b) <= horizon
    ]
    out = {
        "window_hours": hours,
        "now_local": now_local.strftime(DATETIME_FMT),
        "forecast_ends": slots[-1]["local_datetime"] if n else None,
        "will_rain": None, "raining_now": False,
        "starts": None, "starts_in_minutes": None,
        "ends": None, "ends_known": False, "episodes": [],
        "heaviest": None, "total_precipitation_mm": None, "wet_slots": 0, "total_slots": len(idxs), "slots": [],
    }
    if not idxs:
        return out  # will_rain stays None: nothing to judge (BMKG data out of date)

    wet = {i for i in idxs if slot_is_wet(slots[i])}
    mms = [precip_mm(slots[i]) for i in idxs if precip_mm(slots[i]) is not None]
    out["total_precipitation_mm"] = round(sum(mms), 1) if mms else None
    out["will_rain"] = bool(wet)
    out["wet_slots"] = len(wet)
    out["slots"] = [
        dict(block_to_dict(slots[i]), wet=i in wet, current=(i == cur)) for i in idxs
    ]
    if wet:
        runs = []  # consecutive rainy slots -> [first_index, last_index]
        for i in sorted(wet):
            if runs and i == runs[-1][1] + 1:
                runs[-1][1] = i
            else:
                runs.append([i, i])
        for first_i, last_i in runs:
            while last_i + 1 < n and slot_is_wet(slots[last_i + 1]):
                last_i += 1  # a rainy spell can continue past the requested window
            out["episodes"].append({
                "starts": slots[first_i]["local_datetime"],
                # first dry slot after the spell; None if BMKG's data ends while still wet
                "ends": slots[last_i + 1]["local_datetime"] if last_i + 1 < n else None,
            })
        first = runs[0][0]
        out["raining_now"] = cur is not None and cur == first
        start = parse_local_dt(slots[first])
        out["starts"] = slots[first]["local_datetime"]
        out["starts_in_minutes"] = (
            0 if out["raining_now"] else max(0, int((start - now_local).total_seconds() // 60))
        )
        out["ends"] = out["episodes"][0]["ends"]
        out["ends_known"] = out["ends"] is not None
        heaviest = max(wet, key=lambda i: (rain_rank(slots[i].get("weather_desc")), precip_mm(slots[i]) or 0.0, -i))
        out["heaviest"] = block_to_dict(slots[heaviest])
    return out


def _day_word(dt, now):
    days = (dt.date() - now.date()).days
    return "today" if days == 0 else "tomorrow" if days == 1 else dt.strftime("%a %d %b")


def _time_with_day(dt, now):
    """'15:00' if today, else '02:00 tomorrow' / '02:00 Sat 29 Aug'."""
    return dt.strftime("%H:%M") if dt.date() == now.date() else f"{dt:%H:%M} {_day_word(dt, now)}"


def rain_headline(an, now_local):
    if an["will_rain"] is None:
        return "No forecast slots cover this window (BMKG's data looks out of date)."
    if not an["will_rain"]:
        return f"No rain forecast in the next {an['window_hours']} hours."
    start = datetime.strptime(an["starts"], DATETIME_FMT)
    if an["raining_now"]:
        text = "Rain is forecast right now"
    else:
        wait = human_duration(an["starts_in_minutes"] * 60)
        text = f"Rain expected from {_time_with_day(start, now_local)} (in about {wait})"
    if an["ends"]:
        text += f", until about {_time_with_day(datetime.strptime(an['ends'], DATETIME_FMT), now_local)}"
    else:
        text += ", continuing to the end of BMKG's forecast"
    return text + "."


def rain_followups(an, now_local):
    """One line per rainy spell after the first (the headline covers the first)."""
    lines = []
    for ep in an["episodes"][1:]:
        start = datetime.strptime(ep["starts"], DATETIME_FMT)
        line = f"Again from {_time_with_day(start, now_local)}"
        if ep["ends"]:
            line += f" until about {_time_with_day(datetime.strptime(ep['ends'], DATETIME_FMT), now_local)}"
        lines.append(line + ".")
    return lines


def rain_phrase(an, now_local):
    """Very short version for --oneline."""
    if an["will_rain"] is None:
        return "forecast out of date"
    if not an["will_rain"]:
        return f"no rain forecast ({an['window_hours']}h)"
    end = datetime.strptime(an["ends"], DATETIME_FMT) if an["ends"] else None
    if an["raining_now"]:
        return f"raining until ~{_time_with_day(end, now_local)}" if end else "raining, no end in forecast"
    start = datetime.strptime(an["starts"], DATETIME_FMT)
    return f"rain {_time_with_day(start, now_local)} (in {human_duration(an['starts_in_minutes'] * 60)})"


def build_rain_json(adm4_code, display_name=None, hours=DEFAULT_RAIN_HOURS, refresh=False):
    fc = load_forecast(adm4_code, refresh=refresh)
    now_local = region_now(fc.slots)
    an = analyze_rain(fc.slots, now_local, hours)
    result = {
        "location": {"label": display_name or fc.label, "code": adm4_code},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": SOURCE,
        "rain": an,
    }
    _add_cache_info(result, fc)
    _add_issued(result, fc.slots)
    if an["will_rain"] is None:
        _add_warning(result, STALE_WARNING)
    return result


def print_rain(adm4_code, display_name=None, hours=DEFAULT_RAIN_HOURS, refresh=False):
    """Print the rain outlook; return the location label shown."""
    fc = load_forecast(adm4_code, refresh=refresh)
    label = display_name or fc.label
    top_note, footer_note = cache_notes(fc)
    if top_note:
        print(top_note + "\n")
    now = region_now(fc.slots)
    an = analyze_rain(fc.slots, now, hours)

    print(f"\U0001F327  Rain outlook for {label}")
    print(f"   next {hours} h \u00b7 local time there: {now:%a %d %b, %H:%M}")
    print("=" * 60)
    print(rain_headline(an, now))
    for line in rain_followups(an, now):
        print(line)
    if an["heaviest"]:
        h = an["heaviest"]
        when = _time_with_day(datetime.strptime(h["datetime"], DATETIME_FMT), now)
        print(f"Heaviest: {translate_weather(h['weather_desc_id'], h['weather_desc_en'])} at {when}")
    if an["slots"]:
        print()
        for sl in an["slots"]:
            dt = datetime.strptime(sl["datetime"], DATETIME_FMT)
            stamp = f"{dt:%H:%M}" if dt.date() == now.date() else f"{dt:%a %H:%M}"
            mark = "\U0001F327" if sl["wet"] else "  "
            tag = "  \u2190 now" if sl["current"] else ""
            amount = f"  {sl['precipitation_mm']:g} mm" if sl["precipitation_mm"] else ""
            desc = translate_weather(sl["weather_desc_id"], sl["weather_desc_en"])
            print(f"  {stamp:<9} {mark} {desc}{amount}{tag}")
        print(f"\n  {an['wet_slots']} of {an['total_slots']} slots rainy.", end="")
        total = an["total_precipitation_mm"]
        print(f" BMKG's precipitation estimate for the window: {total:g} mm." if total is not None else "")
    if an["forecast_ends"] and now + timedelta(hours=hours) > datetime.strptime(an["forecast_ends"], DATETIME_FMT):
        print(f"  (BMKG's forecast only runs until {an['forecast_ends']}.)")
    if an["will_rain"] is False:
        print(
            "\nNote: BMKG gives one weather snapshot per 3 hours for this village. A brief or "
            "very local shower between slots, or one happening right now, won't show up here; "
            "check BMKG's radar if it looks wet outside."
        )
    print(f"\nSource: {SOURCE}")
    if issued_note(fc.slots):
        print(issued_note(fc.slots))
    if footer_note:
        print(footer_note)
    return label


def print_oneline(adm4_code, display_name=None, hours=ONELINE_RAIN_HOURS,
                  rain_only=False, refresh=False):
    """One short line (status bars, tmux, shell prompts); return the label."""
    fc = load_forecast(adm4_code, refresh=refresh)
    label = display_name or fc.label
    short = label.split(",")[0].strip()
    slots = _sorted_slots(fc.slots)
    if not slots:
        print(f"{short}: no forecast data")
        return label
    now = region_now(slots)
    rain = rain_phrase(analyze_rain(slots, now, hours), now)
    if rain_only:
        line = f"{short}: {rain}"
    else:
        cur = current_slot_index(slots, now)
        if cur is not None:
            block = slots[cur]
        else:
            block, stale = pick_next_forecast(slots, now)
            if stale:
                block = None
        if block is None:
            line = f"{short}: forecast out of date"
        else:
            desc = weather_en(block.get("weather_desc"), block.get("weather_desc_en")) or block.get("weather_desc") or "?"
            line = (f"{short}: {_fmt(block.get('t'))}\u00b0C {desc}, "
                    f"wind {_fmt(block.get('ws'))} km/h {_fmt(block.get('wd'))} | {rain}")
    if fc.offline:
        line += f" (offline; saved {human_duration(fc.cache_age)} ago)"
    print(line)
    return label


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def label_for_code(code, provinsi, kabupaten, kecamatan, desa):
    """'Village, District, Regency, Province' for a code, or None if unknown."""
    for c, name in desa:
        if c == code:
            p = full_path(c, provinsi, kabupaten, kecamatan, name)
            return f"{p['village']}, {p['district']}, {p['regency']}, {p['province']}"
    return None


def show_forecast(code, label, args):
    """Print (text or JSON) the forecast; return the label actually shown."""
    rain = args.rain is not None
    if args.json:
        if rain:
            result = build_rain_json(code, label, hours=args.rain, refresh=args.refresh)
        else:
            result = build_forecast_json(code, label, full=args.full, refresh=args.refresh)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result["location"]["label"]
    if args.oneline:
        hours = args.rain if rain else ONELINE_RAIN_HOURS
        return print_oneline(code, label, hours=hours, rain_only=rain, refresh=args.refresh)
    if rain:
        return print_rain(code, label, hours=args.rain, refresh=args.refresh)
    return fetch_and_print_weather(code, label, full=args.full, refresh=args.refresh)


def run(args):
    cfg = load_config()

    if args.location:
        loc = cfg.get("default_location")
        if args.json:
            if not loc:
                raise BmkgError("No default location saved yet.")
            print(json.dumps(loc, ensure_ascii=False, indent=2))
        elif loc:
            print(f"Current default location: {loc['label']}  [{loc['code']}]")
        else:
            print("No default location set yet. Use --search <name> --save to set one.")
        return

    if args.code:
        code = args.code.strip()
        if not ADM4_RE.match(code):
            raise BmkgError(
                f"'{code}' is not a valid region code. Expected the format "
                "PP.KK.CC.DDDD, e.g. 51.04.03.1006."
            )
        try:
            label = label_for_code(code, *load_wilayah())
        except BmkgError:
            label = None  # region database unavailable; BMKG's own label is used instead
        shown = show_forecast(code, label, args)
        if args.save:  # only reached if BMKG accepted the code
            cfg["default_location"] = {"code": code, "label": shown}
            save_config(cfg)
            if not args.json:
                print(f"\nLocation '{shown}' saved as default.")
        return

    if args.search:
        provinsi, kabupaten, kecamatan, desa = load_wilayah()
        matches = search_location(args.search, provinsi, kabupaten, kecamatan, desa)
        if not matches:
            raise BmkgError(f"No location found matching '{args.search}'.")
        # --json implies non-interactive: auto-pick the first match, since a prompt
        # would break a piped/dashboard workflow.
        chosen = choose_location(matches, auto_first=(args.yes or args.json))
        if not chosen:
            if not args.json:
                print("Cancelled.")
            return
        code, path = chosen
        label = f"{path['village']}, {path['district']}, {path['regency']}, {path['province']}"
        show_forecast(code, label, args)
        if args.save:
            cfg["default_location"] = {"code": code, "label": label}
            save_config(cfg)
            if not args.json:
                print(f"\nLocation '{label}' saved as default.")
        return

    # No arguments -> use the saved default location
    loc = cfg.get("default_location")
    if loc and "code" in loc:
        show_forecast(loc["code"], loc.get("label"), args)
    elif args.json:
        raise BmkgError("No default location saved yet.")
    else:
        print(
            "No default location saved yet.\n\n"
            "Set one with either:\n"
            '  python cuaca.py --search "place name" --save\n'
            "  python cuaca.py --code 51.04.03.1006 --save\n"
        )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Check BMKG weather forecasts for any village/subdistrict in Indonesia."
    )
    parser.add_argument("--search", "-s", metavar="NAME",
                         help="Search for a location by name (village/district/city/province)")
    parser.add_argument("--code", "-k", metavar="ADM4",
                         help="Use a region code (adm4) directly, e.g. 51.04.03.1006")
    parser.add_argument("--save", action="store_true",
                         help="Save the chosen search result as your new default location")
    parser.add_argument("--location", "-l", action="store_true",
                         help="Show the currently saved default location")
    parser.add_argument("--yes", "-y", action="store_true",
                         help="Automatically pick the first search result (non-interactive mode)")
    parser.add_argument("--full", "-f", action="store_true",
                         help="Show the complete 3-day, 3-hourly forecast (wind, cloud cover, visibility) "
                              "instead of just the next slot and tomorrow morning")
    parser.add_argument("--rain", "-r", nargs="?", type=int, const=DEFAULT_RAIN_HOURS,
                         default=None, metavar="HOURS",
                         help=f"Will it rain? Look ahead HOURS hours (default {DEFAULT_RAIN_HOURS}, "
                              f"max {MAX_RAIN_HOURS}): when it starts, when it eases, how heavy")
    parser.add_argument("--oneline", "-o", action="store_true",
                         help="Print a single short line (for status bars, tmux, shell prompts). "
                              "Combine with --rain for just the rain part")
    parser.add_argument("--refresh", action="store_true",
                         help="Ignore the saved copy of the forecast and download a fresh one")
    parser.add_argument("--json", "-j", action="store_true",
                         help="Output the forecast as JSON instead of human-readable text "
                              "(errors are reported as {\"error\": ...} on stdout)")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.rain is not None and not 1 <= args.rain <= MAX_RAIN_HOURS:
        parser.error(f"--rain HOURS must be between 1 and {MAX_RAIN_HOURS}")
    if args.oneline and args.json:
        parser.error("--oneline and --json can't be combined (use --json for machine-readable output)")
    if args.full and (args.rain is not None or args.oneline):
        parser.error("--full can't be combined with --rain or --oneline")
    try:
        run(args)
    except BmkgError as e:
        if args.json:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
        else:
            print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
