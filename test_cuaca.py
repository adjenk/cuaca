# Copyright (C) 2026 Adam Jenkins
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests: a tiny region fixture and a fake BMKG response, no network needed."""
import json
import os
import sys
from datetime import datetime

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bmkg_weather as b  # noqa: E402

@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Never touch the real ~/.cache during tests."""
    monkeypatch.setenv("BMKG_CACHE_DIR", str(tmp_path / "cache"))


FIXTURE_CSV = """51,BALI
51.04,KAB. GIANYAR
51.04.03,Gianyar
51.04.03.1006,Gianyar
51.04.05,Ubud
51.04.05.1005,Ubud
51.04.05.1006,Lodtunduh
51.71,KOTA DENPASAR
51.71.01,Denpasar Selatan
51.71.01.1003,Sanur
51.03,KAB. BADUNG
51.03.01,Kuta
51.03.01.1001,Sanur Kaja
12,SUMATERA UTARA
12.07,KAB. DELI SERDANG
12.07.28,Lubuk Pakam
12.07.28.1007,"Lubuk Pakam I,II"
12.07.28.1008,Pasi Kuala Ba'u
"""


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("data") / "wilayah.csv"
    path.write_text(FIXTURE_CSV, encoding="utf-8")
    return b.load_wilayah(str(path))


def names(db, query):
    return [path["village"] for _, path in b.search_location(query, *db)]


# ---------------------------------------------------------------- search
def test_plain_search(db):
    assert names(db, "ubud") == ["Ubud", "Lodtunduh"]  # village first, then rest of the district


def test_comma_in_query_is_ignored(db):
    # This was the README's headline example and used to return nothing.
    assert names(db, "ubud, gianyar")[0] == "Ubud"
    assert names(db, "ubud,gianyar") == names(db, "ubud gianyar")


def test_kabupaten_matches_kab_prefix(db):
    assert "Gianyar" in names(db, "Kabupaten Gianyar")
    assert names(db, "kota denpasar") == ["Sanur"]


def test_village_name_match_ranks_first(db):
    # Lodtunduh is in Gianyar too, but "Ubud" the village must win.
    assert names(db, "ubud gianyar")[0] == "Ubud"


def test_exact_name_ranks_before_partial(db):
    assert names(db, "sanur") == ["Sanur", "Sanur Kaja"]


def test_apostrophes_and_punctuation_in_names(db):
    assert names(db, "bau") == ["Pasi Kuala Ba'u"]
    assert names(db, "ba'u") == ["Pasi Kuala Ba'u"]
    assert names(db, "lubuk pakam i ii") == ["Lubuk Pakam I,II"]


def test_empty_and_unmatched(db):
    assert names(db, "") == []
    assert names(db, "  ,, ") == []
    assert names(db, "zzzz") == []


def test_accents_and_case_ignored(db):
    assert names(db, "ÜBUD")[0] == "Ubud"


# ---------------------------------------------------------------- time handling
def slot(local, utc, **kw):
    d = {"local_datetime": local, "utc_datetime": utc, "weather_desc": "Cerah",
         "t": 29, "hu": 70, "ws": 5, "wd": "E", "wd_deg": 90, "tcc": 40, "vs_text": "< 10 km"}
    d.update(kw)
    return d


# WITA = UTC+8
SLOTS = [
    slot("2026-08-20 09:00:00", "2026-08-20 01:00:00"),
    slot("2026-08-20 12:00:00", "2026-08-20 04:00:00"),
    slot("2026-08-20 15:00:00", "2026-08-20 07:00:00"),
    slot("2026-08-21 06:00:00", "2026-08-20 22:00:00"),
    slot("2026-08-21 09:00:00", "2026-08-21 01:00:00"),
]


def test_region_now_uses_region_offset_not_machine_clock():
    now_utc = datetime(2026, 8, 20, 5, 30)  # 13:30 in WITA
    assert b.region_now(SLOTS, now_utc=now_utc) == datetime(2026, 8, 20, 13, 30)


def test_region_now_across_midnight():
    now_utc = datetime(2026, 8, 20, 17, 0)  # already Aug 21, 01:00 in WITA
    assert b.region_now(SLOTS, now_utc=now_utc).date() == datetime(2026, 8, 21).date()


def test_pick_next_skips_past_slots():
    block, stale = b.pick_next_forecast(SLOTS, datetime(2026, 8, 20, 13, 30))
    assert block["local_datetime"] == "2026-08-20 15:00:00" and not stale


def test_pick_next_flags_stale_data():
    block, stale = b.pick_next_forecast(SLOTS, datetime(2026, 9, 1, 0, 0))
    assert stale and block["local_datetime"] == "2026-08-21 09:00:00"


def test_region_now_falls_back_without_utc_field():
    slots = [slot("2026-08-20 09:00:00", None)]
    assert isinstance(b.region_now(slots), datetime)


# ---------------------------------------------------------------- BMKG fetch / errors
class FakeResp:
    def __init__(self, payload=None, status=200, text_ok=True):
        self._payload, self.status_code, self._ok = payload, status, text_ok

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        if not self._ok:
            raise ValueError("not json")
        return self._payload


def payload(slots):
    return {"lokasi": {"desa": "Ubud", "kecamatan": "Ubud", "kotkab": "KAB. GIANYAR", "provinsi": "BALI"},
            "data": [{"cuaca": [slots]}]}


def test_invalid_code_rejected_before_any_request(monkeypatch):
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: pytest.fail("should not call BMKG"))
    with pytest.raises(b.BmkgError, match="not a valid region code"):
        b.get_forecast_data("nonsense")


def test_http_404_gives_friendly_error(monkeypatch):
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp(status=404))
    with pytest.raises(b.BmkgError, match="no forecast for region code"):
        b.get_forecast_data("99.99.99.9999")


def test_network_failure(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(b.requests, "get", boom)
    with pytest.raises(b.BmkgError, match="Failed to fetch"):
        b.get_forecast_data("51.04.05.1005")


def test_non_json_and_empty_responses(monkeypatch):
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp(text_ok=False))
    with pytest.raises(b.BmkgError, match="non-JSON"):
        b.get_forecast_data("51.04.05.1005")
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp({}))
    with pytest.raises(b.BmkgError, match="no data"):
        b.get_forecast_data("51.04.05.1005")


def test_json_error_goes_to_stdout_as_json(monkeypatch, capsys, db):
    monkeypatch.setattr(b, "load_wilayah", lambda *a, **k: db)
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp(status=404))
    rc = b.main(["--code", "99.99.99.9999", "--json"])
    out, err = capsys.readouterr()
    assert rc == 1 and err == ""
    assert "error" in json.loads(out)


def test_text_error_goes_to_stderr(monkeypatch, capsys, db):
    monkeypatch.setattr(b, "load_wilayah", lambda *a, **k: db)
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp(status=404))
    rc = b.main(["--code", "99.99.99.9999"])
    out, err = capsys.readouterr()
    assert rc == 1 and out == "" and "no forecast" in err


def test_json_output_shape(monkeypatch):
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp(payload(SLOTS)))
    monkeypatch.setattr(b, "region_now", lambda flat: datetime(2026, 8, 20, 13, 30))
    r = b.build_forecast_json("51.04.05.1005")
    assert r["next_forecast"]["datetime"] == "2026-08-20 15:00:00"
    assert r["tomorrow_morning_forecast"]["datetime"] == "2026-08-21 09:00:00"
    assert "warning" not in r and r["location"]["label"] == "Ubud, Ubud, KAB. GIANYAR, BALI"


# ---------------------------------------------------------------- --code --save
@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(b, "CONFIG_PATH", str(p))
    return p


def test_save_with_code_stores_a_real_label(monkeypatch, cfg_path, db):
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp(payload(SLOTS)))
    monkeypatch.setattr(b, "load_wilayah", lambda *a, **k: db)
    assert b.main(["--code", "51.04.05.1005", "--save"]) == 0
    saved = json.loads(cfg_path.read_text())["default_location"]
    assert saved == {"code": "51.04.05.1005", "label": "Ubud, Ubud, KAB. GIANYAR, BALI"}


def test_bad_code_is_never_saved(monkeypatch, cfg_path, db):
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp(status=404))
    monkeypatch.setattr(b, "load_wilayah", lambda *a, **k: db)
    assert b.main(["--code", "51.04.05.9999", "--save"]) == 1
    assert b.main(["--code", "typo", "--save"]) == 1
    assert not cfg_path.exists()


def test_code_unknown_to_csv_falls_back_to_bmkg_label(monkeypatch, cfg_path, db):
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp(payload(SLOTS)))
    monkeypatch.setattr(b, "load_wilayah", lambda *a, **k: db)
    assert b.main(["--code", "51.04.05.7777", "--save"]) == 0
    assert json.loads(cfg_path.read_text())["default_location"]["label"].startswith("Ubud")


def test_no_default_location_json(monkeypatch, cfg_path, capsys):
    assert b.main(["--json"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_download_progress_goes_to_stderr(monkeypatch, tmp_path, capsys):
    """Progress chatter on stdout would corrupt --json output on a first run."""
    class R:
        content = FIXTURE_CSV.encode()
        def raise_for_status(self): pass
    monkeypatch.setattr(b, "WILAYAH_CSV", str(tmp_path / "wilayah.csv"))
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: R())
    b.ensure_wilayah_csv()
    out, err = capsys.readouterr()
    assert out == "" and "Downloading" in err


# ====================================================================== cache
CODE = "51.04.05.1005"


def counting_get(monkeypatch, response_factory=None):
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return response_factory() if response_factory else FakeResp(payload(SLOTS))
    monkeypatch.setattr(b.requests, "get", fake)
    return calls


def test_second_call_within_ttl_uses_cache(monkeypatch):
    calls = counting_get(monkeypatch)
    first = b.load_forecast(CODE, now=1000.0)
    second = b.load_forecast(CODE, now=1000.0 + 600)
    assert len(calls) == 1
    assert first.cache_age is None and second.cache_age == 600
    assert second.slots == first.slots and second.label == first.label and not second.offline


def test_expired_cache_is_refetched(monkeypatch):
    calls = counting_get(monkeypatch)
    b.load_forecast(CODE, now=1000.0)
    fc = b.load_forecast(CODE, now=1000.0 + b.CACHE_TTL_SECONDS + 1)
    assert len(calls) == 2 and fc.cache_age is None


def test_refresh_bypasses_cache_but_updates_it(monkeypatch):
    calls = counting_get(monkeypatch)
    b.load_forecast(CODE, now=1000.0)
    b.load_forecast(CODE, refresh=True, now=1010.0)
    assert len(calls) == 2
    assert b.load_forecast(CODE, now=1020.0).cache_age == 10  # cache was rewritten


def test_network_failure_falls_back_to_expired_cache(monkeypatch):
    counting_get(monkeypatch)
    b.load_forecast(CODE, now=1000.0)

    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("no wifi")
    monkeypatch.setattr(b.requests, "get", boom)
    fc = b.load_forecast(CODE, now=1000.0 + 7200)
    assert fc.offline and fc.cache_age == 7200 and fc.slots == SLOTS


def test_network_failure_without_cache_raises(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("no wifi")
    monkeypatch.setattr(b.requests, "get", boom)
    with pytest.raises(b.BmkgNetworkError):
        b.load_forecast(CODE)


def test_unknown_region_does_not_fall_back_to_cache(monkeypatch):
    """A 404 means 'no such region', not 'BMKG is down' - don't mask it with old data."""
    counting_get(monkeypatch)
    b.load_forecast(CODE, now=1000.0)
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp(status=404))
    with pytest.raises(b.BmkgError) as exc:
        b.load_forecast(CODE, refresh=True, now=2000.0)
    assert not isinstance(exc.value, b.BmkgNetworkError)


def test_cache_is_per_region(monkeypatch):
    calls = counting_get(monkeypatch)
    b.load_forecast("51.04.05.1005", now=1.0)
    b.load_forecast("51.04.03.1006", now=2.0)
    assert len(calls) == 2


def test_corrupt_cache_is_ignored(monkeypatch):
    calls = counting_get(monkeypatch)
    b.load_forecast(CODE, now=1000.0)
    path = b._cache_path(CODE)
    for junk in ("{not json", "[]", json.dumps({"version": 99}), json.dumps(
            {"version": b.CACHE_VERSION, "fetched_at": "x", "slots": []})):
        with open(path, "w") as f:
            f.write(junk)
        assert b.load_forecast(CODE, now=1001.0).cache_age is None
    assert len(calls) == 5


def test_unwritable_cache_dir_does_not_break_forecasts(monkeypatch, tmp_path):
    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    monkeypatch.setenv("BMKG_CACHE_DIR", str(blocker / "sub"))  # can't mkdir under a file
    counting_get(monkeypatch)
    assert b.load_forecast(CODE).slots == SLOTS


def test_empty_forecast_is_not_cached(monkeypatch):
    counting_get(monkeypatch, lambda: FakeResp(payload([])))
    b.load_forecast(CODE, now=1.0)
    assert not os.path.exists(b._cache_path(CODE))


def test_cache_dir_resolution(monkeypatch):
    monkeypatch.delenv("BMKG_CACHE_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", "/xdg")
    assert b.cache_dir() == os.path.join("/xdg", "cuaca")
    monkeypatch.setenv("BMKG_CACHE_DIR", "/custom")
    assert b.cache_dir() == "/custom"


def test_human_duration():
    assert b.human_duration(5) == "5 s"
    assert b.human_duration(300) == "5 min"
    assert b.human_duration(5400) == "1h 30m"
    assert b.human_duration(7200) == "2h"
    assert b.human_duration(90000) == "1d 1h"
    assert b.human_duration(-5) == "0 s"


def test_json_reports_cache_age_and_offline_warning(monkeypatch):
    counting_get(monkeypatch)
    monkeypatch.setattr(b, "region_now", lambda flat: datetime(2026, 8, 20, 13, 30))
    fresh = b.build_forecast_json(CODE)
    assert "cache" not in fresh

    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(b.requests, "get", boom)
    # Age the cache entry by rewriting fetched_at.
    path = b._cache_path(CODE)
    entry = json.load(open(path))
    entry["fetched_at"] -= 7200
    json.dump(entry, open(path, "w"))
    old = b.build_forecast_json(CODE)
    assert old["cache"]["offline_fallback"] is True and old["cache"]["age_seconds"] >= 7200
    assert "could not be reached" in old["warning"]


def test_cache_footer_hidden_for_brand_new_copy():
    assert b.cache_notes(b.Forecast("x", [], 5.0, False)) == (None, None)
    assert "10 min" in b.cache_notes(b.Forecast("x", [], 600.0, False))[1]
    assert "Couldn't reach BMKG" in b.cache_notes(b.Forecast("x", [], 600.0, True))[0]


# ====================================================================== rain
def series(descs, start="2026-08-20 09:00:00"):
    """Consecutive 3-hourly WITA slots with the given weather descriptions."""
    from datetime import timedelta
    t0 = datetime.strptime(start, b.DATETIME_FMT)
    out = []
    for i, d in enumerate(descs):
        loc = t0 + timedelta(hours=3 * i)
        out.append(slot(loc.strftime(b.DATETIME_FMT),
                        (loc - timedelta(hours=8)).strftime(b.DATETIME_FMT), weather_desc=d))
    return out


NOW = datetime(2026, 8, 20, 10, 0)  # slot 09:00 is "current"; next slot is 12:00


def test_wet_detection():
    for d in ("Hujan Ringan", "Hujan Sedang", "Hujan Lebat", "Hujan Lokal", "Hujan Local",
              "Hujan Petir", "Hujan Petir Kencang", "Petir", "hujan ringan"):
        assert b.is_wet(d), d
    for d in ("Cerah", "Berawan", "Kabut", "Udara Kabur", None, ""):
        assert not b.is_wet(d), d


def test_severity_ordering():
    ranks = [b.rain_rank(d) for d in ("Hujan Ringan", "Hujan Lokal", "Hujan Sedang",
                                      "Hujan Lebat", "Hujan Petir", "Hujan Petir Kencang")]
    assert ranks == sorted(ranks) and len(set(ranks)) == 6


def test_dry_window():
    an = b.analyze_rain(series(["Cerah", "Berawan", "Cerah", "Cerah"]), NOW, 12)
    assert an["will_rain"] is False and an["episodes"] == [] and an["heaviest"] is None
    assert b.rain_headline(an, NOW) == "No rain forecast in the next 12 hours."


def test_rain_later_today():
    an = b.analyze_rain(series(["Cerah", "Berawan", "Hujan Sedang", "Hujan Ringan", "Cerah"]), NOW, 24)
    assert an["will_rain"] and not an["raining_now"]
    assert an["starts"] == "2026-08-20 15:00:00" and an["starts_in_minutes"] == 300
    assert an["ends"] == "2026-08-20 21:00:00" and an["ends_known"]
    assert an["heaviest"]["weather_desc_en"] == "Moderate Rain"
    assert an["wet_slots"] == 2
    assert b.rain_headline(an, NOW) == "Rain expected from 15:00 (in about 5h), until about 21:00."


def test_raining_now():
    an = b.analyze_rain(series(["Hujan Sedang", "Hujan Ringan", "Cerah"]), NOW, 12)
    assert an["raining_now"] and an["starts_in_minutes"] == 0
    assert an["ends"] == "2026-08-20 15:00:00"
    assert b.rain_headline(an, NOW).startswith("Rain is forecast right now")
    assert b.rain_phrase(an, NOW) == "raining until ~15:00"


def test_old_slot_is_not_treated_as_current():
    # Last slot is >3h old, so it no longer describes "now".
    an = b.analyze_rain(series(["Hujan Lebat"]), datetime(2026, 8, 20, 13, 0), 12)
    assert not an["raining_now"] and an["will_rain"] is None


def test_rain_beyond_horizon_is_ignored():
    slots = series(["Cerah", "Cerah", "Cerah", "Hujan Lebat"])  # rain at 18:00
    assert b.analyze_rain(slots, NOW, 3)["will_rain"] is False
    assert b.analyze_rain(slots, NOW, 12)["will_rain"] is True


def test_multiple_episodes_and_followups():
    slots = series(["Cerah", "Hujan Ringan", "Cerah", "Cerah", "Hujan Petir", "Hujan Petir", "Cerah"])
    an = b.analyze_rain(slots, NOW, 24)
    assert [e["starts"] for e in an["episodes"]] == ["2026-08-20 12:00:00", "2026-08-20 21:00:00"]
    assert an["episodes"][1]["ends"] == "2026-08-21 03:00:00"
    assert an["heaviest"]["weather_desc_en"] == "Thunderstorm"
    assert an["heaviest"]["datetime"] == "2026-08-20 21:00:00"  # earliest of equally-heavy slots
    assert b.rain_followups(an, NOW) == ["Again from 21:00 until about 03:00 tomorrow."]


def test_rainy_spell_can_outlast_the_window():
    slots = series(["Cerah", "Hujan Ringan", "Hujan Ringan", "Hujan Ringan", "Cerah"])
    an = b.analyze_rain(slots, NOW, 3)  # window ends 13:00; only the 12:00 slot is inside
    assert an["total_slots"] == 2 and an["wet_slots"] == 1
    assert an["ends"] == "2026-08-20 21:00:00"  # looks past the window to find the real end


def test_data_ending_while_still_raining():
    an = b.analyze_rain(series(["Cerah", "Hujan Sedang", "Hujan Sedang"]), NOW, 24)
    assert an["ends"] is None and an["ends_known"] is False
    assert "continuing to the end of BMKG's forecast" in b.rain_headline(an, NOW)
    assert b.rain_phrase(an, NOW).startswith("rain 12:00")


def test_all_slots_in_the_past():
    an = b.analyze_rain(series(["Cerah", "Hujan Sedang"]), datetime(2026, 9, 1, 0, 0), 24)
    assert an["will_rain"] is None and an["total_slots"] == 0
    assert "out of date" in b.rain_headline(an, NOW)
    assert b.rain_phrase(an, NOW) == "forecast out of date"


def test_empty_slot_list():
    an = b.analyze_rain([], NOW, 12)
    assert an["will_rain"] is None and an["forecast_ends"] is None


def test_unsorted_input_is_handled():
    slots = series(["Cerah", "Berawan", "Hujan Sedang"])
    an = b.analyze_rain(list(reversed(slots)), NOW, 24)
    assert an["starts"] == "2026-08-20 15:00:00"


def test_rain_tomorrow_wording():
    slots = series(["Cerah"] * 6 + ["Hujan Lebat"])  # 03:00 on the 21st
    an = b.analyze_rain(slots, datetime(2026, 8, 20, 22, 0), 12)
    assert an["will_rain"]
    assert "from 03:00 tomorrow" in b.rain_headline(an, datetime(2026, 8, 20, 22, 0))


# ---------------------------------------------------------------- CLI: --rain / --oneline
@pytest.fixture
def wired(monkeypatch, db):
    """CLI wired to a fake BMKG response; 'now' is fixed at 13:30 local."""
    descs = ["Cerah", "Cerah Berawan", "Hujan Sedang", "Hujan Ringan", "Berawan", "Cerah"]
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp(payload(series(descs))))
    monkeypatch.setattr(b, "load_wilayah", lambda *a, **k: db)
    monkeypatch.setattr(b, "region_now", lambda flat, now_utc=None: datetime(2026, 8, 20, 13, 30))


def test_rain_text_output(wired, capsys):
    assert b.main(["--code", CODE, "--rain"]) == 0
    out = capsys.readouterr().out
    assert "Rain outlook for Ubud, Ubud, KAB. GIANYAR, BALI" in out
    assert "Rain expected from 15:00" in out and "Hujan Sedang (Moderate Rain)" in out
    assert "\u2190 now" in out and "2 of " in out


def test_rain_json_output(wired, capsys):
    assert b.main(["--code", CODE, "--rain", "6", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["rain"]["window_hours"] == 6 and data["rain"]["will_rain"] is True
    assert data["rain"]["starts"] == "2026-08-20 15:00:00"
    assert data["location"]["code"] == CODE


def test_bare_rain_flag_defaults_to_24h_and_does_not_eat_next_flag():
    args = b.build_parser().parse_args(["--rain", "--json"])
    assert args.rain == b.DEFAULT_RAIN_HOURS and args.json
    assert b.build_parser().parse_args(["--rain", "9"]).rain == 9
    assert b.build_parser().parse_args([]).rain is None


def test_oneline(wired, capsys):
    assert b.main(["--code", CODE, "--oneline"]) == 0
    out = capsys.readouterr().out
    # 13:30 -> the 12:00 slot ("Cerah Berawan") is the one in effect
    assert out == "Ubud: 29\u00b0C Partly Cloudy, wind 5 km/h E | rain 15:00 (in 1h 30m)\n"
    assert out.count("\n") == 1


def test_oneline_with_rain_is_rain_only(wired, capsys):
    assert b.main(["--code", CODE, "--oneline", "--rain", "6"]) == 0
    assert capsys.readouterr().out == "Ubud: rain 15:00 (in 1h 30m)\n"


def test_oneline_dry(monkeypatch, db, capsys):
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp(payload(series(["Cerah"] * 6))))
    monkeypatch.setattr(b, "load_wilayah", lambda *a, **k: db)
    monkeypatch.setattr(b, "region_now", lambda flat, now_utc=None: datetime(2026, 8, 20, 13, 30))
    b.main(["--code", CODE, "--oneline"])
    assert capsys.readouterr().out.strip().endswith("| dry next 12h")


def test_oneline_stale_data(monkeypatch, db, capsys):
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp(payload(series(["Cerah"] * 3))))
    monkeypatch.setattr(b, "load_wilayah", lambda *a, **k: db)
    monkeypatch.setattr(b, "region_now", lambda flat, now_utc=None: datetime(2026, 9, 1, 0, 0))
    b.main(["--code", CODE, "--oneline"])
    assert capsys.readouterr().out == "Ubud: forecast out of date\n"


def test_oneline_marks_offline_copy(wired, monkeypatch, capsys):
    b.main(["--code", CODE, "--oneline"])  # populate cache
    capsys.readouterr()
    entry_path = b._cache_path(CODE)
    entry = json.load(open(entry_path))
    entry["fetched_at"] -= 7200
    json.dump(entry, open(entry_path, "w"))

    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(b.requests, "get", boom)
    assert b.main(["--code", CODE, "--oneline"]) == 0
    assert "(offline; saved 2h ago)" in capsys.readouterr().out


def test_oneline_is_a_single_line_with_search(wired, capsys, monkeypatch, db):
    monkeypatch.setattr(b, "load_wilayah", lambda *a, **k: db)
    assert b.main(["--search", "ubud", "--yes", "--oneline"]) == 0
    assert capsys.readouterr().out.count("\n") == 1


@pytest.mark.parametrize("argv", [
    ["--oneline", "--json"],
    ["--full", "--rain"],
    ["--full", "--oneline"],
    ["--rain", "0"],
    ["--rain", "73"],
])
def test_conflicting_or_invalid_flags_are_usage_errors(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        b.main(["--code", CODE, *argv])
    assert exc.value.code == 2
    assert capsys.readouterr().err


def test_refresh_flag_reaches_the_network(wired, monkeypatch, capsys):
    calls = counting_get(monkeypatch)
    b.main(["--code", CODE, "--oneline"])
    b.main(["--code", CODE, "--oneline"])
    assert len(calls) == 1
    b.main(["--code", CODE, "--oneline", "--refresh"])
    assert len(calls) == 2


def test_missing_fields_print_cleanly_not_as_None():
    """Live BMKG data had vs_text = None for some slots."""
    text = b.format_block(slot("2026-08-20 09:00:00", "2026-08-20 01:00:00", vs_text=None, t=None))
    assert "None" not in text
    assert "Visibility  : not reported" in text and "Temperature : ?" in text


# ====================================================== real BMKG data (captured live)
REAL_SLOT = {
    "datetime": "2026-10-24T13:00:00Z", "t": 25, "tcc": 93, "tp": 0.1, "weather": 3,
    "weather_desc": "Berawan", "weather_desc_en": "Mostly Cloudy", "wd_deg": 132, "wd": "E",
    "wd_to": "W", "ws": 3.7, "hu": 90, "vs": None, "vs_text": None, "time_index": "11-12",
    "analysis_date": "2026-10-24T00:00:00",
    "image": "https://api-apps.bmkg.go.id/storage/icon/cuaca/berawan-pm.svg",
    "utc_datetime": "2026-10-24 13:00:00", "local_datetime": "2026-10-24 21:00:00",
}


def test_real_slot_formats_without_none_and_with_bmkg_english():
    text = b.format_block(REAL_SLOT)
    assert "Berawan (Mostly Cloudy)" in text
    assert "Rain        : 0.1 mm" in text
    assert "Visibility  : not reported" in text and "None" not in text


def test_real_slot_json_dict():
    d = b.block_to_dict(REAL_SLOT)
    assert d["weather_desc_en"] == "Mostly Cloudy" and d["precipitation_mm"] == 0.1
    assert d["visibility"] is None and d["temperature_c"] == 25


def test_real_slot_timezone_offset_is_detected():
    assert b.parse_utc_dt(REAL_SLOT) == datetime(2026, 10, 24, 13, 0)
    # 12:34 UTC is 20:34 in WITA (UTC+8), whatever this machine's own timezone is
    assert b.region_now([REAL_SLOT], now_utc=datetime(2026, 10, 24, 12, 34)) == datetime(2026, 10, 24, 20, 34)


def test_trace_amount_under_cloudy_category_is_not_rain():
    assert not b.slot_is_wet(REAL_SLOT)


def test_bmkg_english_preferred_but_table_is_fallback():
    assert b.translate_weather("Berawan", "Mostly Cloudy") == "Berawan (Mostly Cloudy)"
    assert b.translate_weather("Berawan", "Totally Different") == "Berawan (Totally Different)"
    assert b.translate_weather("Berawan") == "Berawan (Mostly Cloudy)"      # fallback table
    assert b.translate_weather("Cerah Berawan", "") == "Cerah Berawan (Partly Cloudy)"
    assert b.translate_weather("Something New") == "Something New"
    assert b.weather_en("Something New") is None


@pytest.mark.parametrize("tp,wet", [(None, False), (0.1, False), (0.99, False), (1.0, True),
                                    (12.5, True), ("2.0", True), ("n/a", False)])
def test_dry_category_becomes_wet_by_amount(tp, wet):
    assert b.slot_is_wet({"weather_desc": "Berawan", "tp": tp}) is wet


def test_rain_category_is_wet_even_with_zero_or_missing_amount():
    assert b.slot_is_wet({"weather_desc": "Hujan Ringan", "tp": 0})
    assert b.slot_is_wet({"weather_desc": "Hujan Ringan"})


def with_tp(slots, tps):
    for sl, tp in zip(slots, tps):
        sl["tp"] = tp
    return slots


def test_rain_detected_from_amount_alone():
    slots = with_tp(series(["Cerah", "Berawan", "Berawan", "Cerah"]), [0, 0.2, 4.0, 0])
    an = b.analyze_rain(slots, NOW, 24)
    assert an["will_rain"] and an["starts"] == "2026-08-20 15:00:00"
    assert an["ends"] == "2026-08-20 18:00:00"


def test_total_precipitation_and_none_handling():
    slots = with_tp(series(["Cerah", "Hujan Sedang", "Hujan Ringan", "Cerah"]), [0, 3.25, 1.0, None])
    assert b.analyze_rain(slots, NOW, 24)["total_precipitation_mm"] == 4.2  # 3.25 + 1.0, rounded
    no_data = series(["Cerah", "Hujan Sedang"])
    assert b.analyze_rain(no_data, NOW, 24)["total_precipitation_mm"] is None


def test_heaviest_breaks_category_ties_by_amount():
    slots = with_tp(series(["Cerah", "Hujan Sedang", "Cerah", "Hujan Sedang"]), [0, 1.5, 0, 9.0])
    an = b.analyze_rain(slots, NOW, 24)
    assert an["heaviest"]["datetime"] == "2026-08-20 18:00:00" and an["heaviest"]["precipitation_mm"] == 9.0


def test_rain_output_shows_amounts(monkeypatch, db, capsys):
    slots = with_tp(series(["Cerah", "Cerah Berawan", "Hujan Sedang", "Berawan"]), [0, 0, 4.5, 0.3])
    slots[2]["weather_desc_en"] = "Moderate Rain"
    monkeypatch.setattr(b.requests, "get", lambda *a, **k: FakeResp(payload(slots)))
    monkeypatch.setattr(b, "load_wilayah", lambda *a, **k: db)
    monkeypatch.setattr(b, "region_now", lambda f, now_utc=None: datetime(2026, 8, 20, 13, 30))
    assert b.main(["--code", CODE, "--rain"]) == 0
    out = capsys.readouterr().out
    assert "Hujan Sedang (Moderate Rain)  4.5 mm" in out
    assert "precipitation estimate for the window: 4.8 mm" in out
    assert "0.3 mm" in out  # trace amounts are still shown, just not called rain


def test_full_output_survives_missing_precipitation(capsys):
    b.print_full_forecast([REAL_SLOT, dict(REAL_SLOT, tp=None, local_datetime="2026-10-24 00:00:00")])
    out = capsys.readouterr().out
    assert "None" not in out and "rain  0.1mm" in out


if __name__ == "__main__":  # allow `python test_cuaca.py`
    sys.exit(pytest.main([__file__, "-q"]))
