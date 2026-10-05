"""Public market adapters, move lenses and the shared cache, on real captured payloads.

`tests/fixtures/market/` holds FRED CSV and Google News RSS responses captured
on 2026-09-29 through `public_data._download`, so parsing is exercised against
what the providers actually return.
"""

import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from covenant_radar.ingestion.feeds import public_data
from covenant_radar.ingestion.feeds.public_data import (
    _redirect_url,
    fred_source,
    news_source,
    parse_news,
    parse_series,
)
from covenant_radar.services.market_intelligence import (
    DRIVERS_BY_KEY,
    SOURCES,
    MarketIntelligenceService,
    driver_move,
    news_feed,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "market"
CAPTURED_AT = datetime(2026, 9, 29, 15, 2, tzinfo=UTC)


def fixture_fetch(source, now):
    """A `fetcher` that replays the captured provider responses."""
    name = source.key.replace(":", "_") + (".csv" if source.kind == "series" else ".xml")
    body = (FIXTURES / name).read_bytes()
    if source.kind == "series":
        return parse_series(body, source)
    return parse_news(body, source, now)


def fixture_snapshot(tmp_path: Path) -> dict:
    service = MarketIntelligenceService(
        cache_path=tmp_path / "market.json",
        fetcher=fixture_fetch,
        clock=lambda: CAPTURED_AT,
        background=False,
    )
    return service.snapshot()


def _series(points, key="DCOILBRENTEU"):
    return {"series": {key: {"observations": [[d, v] for d, v in points]}}, "news": {}}


def test_fred_csv_parses_real_payload_and_rejects_foreign_headers():
    source = fred_source("DCOILBRENTEU", "Brent crude", "U.S. EIA")
    points = parse_series((FIXTURES / "DCOILBRENTEU.csv").read_bytes(), source)
    assert points[-1] == ["2026-09-22", 114.89]
    assert all(isinstance(value, float) for _, value in points)
    assert points == sorted(points)
    other = fred_source("DEXINUS", "USD/INR", "Fed")
    with pytest.raises(ValueError, match="header"):
        parse_series((FIXTURES / "DCOILBRENTEU.csv").read_bytes(), other)
    body = b"observation_date,DCOILBRENTEU\n2026-01-01,.\n2026-01-02,nan\n2026-01-03,70.5\n"
    assert parse_series(body, source) == [["2026-01-03", 70.5]]
    with pytest.raises(ValueError, match="No usable"):
        parse_series(b"observation_date,DCOILBRENTEU\n2026-01-01,.\n", source)


def test_news_keeps_publishers_strips_suffixes_and_drops_social_reposts():
    source = news_source("brent", "crude oil price")
    stories = parse_news((FIXTURES / "news_brent.xml").read_bytes(), source, CAPTURED_AT)
    assert stories
    for story in stories:
        assert story["publisher"] and not story["title"].endswith(f" - {story['publisher']}")
        assert story["url"].startswith("https://")
        published = datetime.fromisoformat(story["published_at"])
        assert CAPTURED_AT - timedelta(days=14) <= published <= CAPTURED_AT + timedelta(hours=1)
    publishers = {story["publisher"].casefold() for story in stories}
    assert not any("linkedin" in name or "tradingview" in name for name in publishers)
    # Stored newest first: the list feeds a live headline stream. Established
    # press is flagged for the views that prefer it, not ranked first here.
    published = [story["published_at"] for story in stories]
    assert published == sorted(published, reverse=True)
    assert any(story["established"] for story in stories)
    # The whole window ages out: nothing is shown as current news a month later.
    assert (
        parse_news(
            (FIXTURES / "news_brent.xml").read_bytes(), source, CAPTURED_AT + timedelta(days=30)
        )
        == []
    )


def test_news_rejects_entities_and_unsafe_links():
    source = news_source("brent", "crude oil price")
    with pytest.raises(ValueError, match="Unsafe"):
        parse_news(b'<!DOCTYPE x [<!ENTITY a "b">]><rss/>', source, CAPTURED_AT)
    item = (
        "<item><title>Brent rises on supply worries in the Gulf - Reuters</title>"
        "<link>{link}</link><pubDate>Mon, 28 Sep 2026 10:00:00 GMT</pubDate>"
        '<source url="https://www.reuters.com">Reuters</source></item>'
    )
    body = (
        "<rss><channel>"
        + item.format(link="javascript:alert(1)")
        + item.format(link="https://news.example/a")
        + item.format(link="https://news.example/b")
        + "</channel></rss>"
    ).encode()
    stories = parse_news(body, source, CAPTURED_AT)
    assert [story["url"] for story in stories] == ["https://news.example/a"]
    assert stories[0]["title"] == "Brent rises on supply worries in the Gulf"
    assert stories[0]["established"] is True


def test_redirects_stay_on_the_approved_host():
    hosts = {"fred.stlouisfed.org", "www.fred.stlouisfed.org"}
    base = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=X"
    assert _redirect_url(base, "/graph/x.csv", hosts).startswith("https://fred.stlouisfed.org/")
    for location in ("https://evil.example/", "http://fred.stlouisfed.org/x", ""):
        with pytest.raises(ValueError):
            _redirect_url(base, location, hosts)


def test_sources_never_carry_borrower_data():
    assert all(source.url.startswith("https://") for source in SOURCES)
    news = [source for source in SOURCES if source.kind == "news"]
    assert {source.key for source in news} >= {"news:brent", "news:usdinr", "news:call_rate"}
    assert public_data.SERIES_HISTORY >= timedelta(days=365)


def test_move_lenses_compare_against_the_reported_quarter_average():
    snapshot = _series(
        [
            ("2026-04-10", 100.0),
            ("2026-06-20", 110.0),  # reported-quarter average 105
            ("2026-07-10", 90.0),
            ("2026-08-10", 96.0),  # quarter so far average 93
            ("2026-09-22", 126.0),  # latest
        ]
    )
    brent = DRIVERS_BY_KEY["brent"]
    move = driver_move(snapshot, brent, date(2026, 4, 1), date(2026, 6, 30))
    assert move["base"] == pytest.approx(105)
    assert move["qtd"] == pytest.approx(104)
    assert move["qtd_change"] == pytest.approx((104 / 105 - 1) * 100)
    assert move["latest_change"] == pytest.approx(20)
    assert move["latest_is_after_period"] is True
    assert move["qtd_label"] == "10 Jul – 22 Sep 2026"
    # No USD/INR series: the rupee change falls back to the dollar change.
    assert move["latest_rupee"] == pytest.approx(20)


def test_dollar_prices_convert_to_rupees_over_the_same_windows():
    snapshot = _series([("2026-05-01", 100.0), ("2026-07-01", 110.0)], key="PIORECRUSDM")
    snapshot["series"]["DEXINUS"] = {"observations": [["2026-05-15", 80.0], ["2026-07-15", 84.0]]}
    move = driver_move(snapshot, DRIVERS_BY_KEY["iron_ore"], date(2026, 4, 1), date(2026, 6, 30))
    assert move["qtd_change"] == pytest.approx(10)
    assert move["qtd_rupee"] == pytest.approx((1.10 * 84 / 80 - 1) * 100)
    assert move["qtd_label"] == "Jul 2026"


def test_rates_move_in_basis_points_and_missing_series_is_none():
    snapshot = _series([("2026-05-01", 5.5), ("2026-08-01", 5.75)], key="IRSTCI01INM156N")
    move = driver_move(snapshot, DRIVERS_BY_KEY["call_rate"], date(2026, 4, 1), date(2026, 6, 30))
    assert move["unit"] == "bp"
    assert move["latest_change"] == pytest.approx(25)
    assert (
        driver_move(snapshot, DRIVERS_BY_KEY["brent"], date(2026, 4, 1), date(2026, 6, 30)) is None
    )


def test_real_snapshot_reflects_captured_markets(tmp_path):
    snapshot = fixture_snapshot(tmp_path)
    assert snapshot["loaded"] and not snapshot["refreshing"]
    assert all(source["status"] == "Current" for source in snapshot["sources"])
    brent = driver_move(snapshot, DRIVERS_BY_KEY["brent"], date(2026, 4, 1), date(2026, 6, 30))
    assert brent["latest_date"] == "2026-09-22"
    assert brent["latest"] == 114.89
    assert brent["qtd_change"] < 0 < brent["latest_change"]
    assert all(snapshot["news"][key] for key in ("brent", "usdinr", "call_rate"))


def test_cache_isolates_failures_survives_restarts_and_throttles(tmp_path):
    now = [CAPTURED_AT]
    calls: list[str] = []
    broken = {"DEXINUS"}

    def fetch(source, instant):
        calls.append(source.key)
        if source.key in broken:
            raise TimeoutError("proxy password leaked in message")
        return fixture_fetch(source, instant)

    path = tmp_path / "market.json"
    service = MarketIntelligenceService(
        cache_path=path, fetcher=fetch, clock=lambda: now[0], background=False
    )
    first = service.snapshot()
    health = {source["key"]: source for source in first["sources"]}
    assert health["DEXINUS"]["status"] == "Unavailable"
    assert health["DEXINUS"]["error"] == "Source unavailable"
    assert "password" not in path.read_text()
    assert health["DCOILBRENTEU"]["status"] == "Current"

    calls.clear()
    service.snapshot(force=True)
    assert calls == []  # the 60-second floor holds even for a forced refresh

    now[0] += timedelta(seconds=61)
    broken.clear()
    service.snapshot()
    assert calls == ["DEXINUS"]  # only the failed source retries early

    restarted = MarketIntelligenceService(
        cache_path=path, enabled=False, fetcher=fetch, clock=lambda: now[0]
    )
    offline = restarted.snapshot()
    assert offline["loaded"]
    assert {source["status"] for source in offline["sources"]} == {"Offline"}


def test_failed_refresh_keeps_last_good_data_and_marks_it_stale(tmp_path):
    now = [CAPTURED_AT]
    down = [False]

    def fetch(source, instant):
        if down[0]:
            raise ConnectionError()
        return fixture_fetch(source, instant)

    service = MarketIntelligenceService(
        cache_path=tmp_path / "m.json",
        fetcher=fetch,
        clock=lambda: now[0],
        refresh_seconds=900,
        background=False,
    )
    service.snapshot()
    down[0] = True
    now[0] += timedelta(seconds=901)
    snapshot = service.snapshot()
    assert {source["status"] for source in snapshot["sources"]} == {"Stale"}
    assert snapshot["series"]["DCOILBRENTEU"]["observations"][-1] == ["2026-09-22", 114.89]


def test_old_cache_versions_and_corrupt_files_are_ignored(tmp_path):
    path = tmp_path / "m.json"
    path.write_text('{"version": 1, "sources": {"rbi": {"items": []}}}')
    service = MarketIntelligenceService(cache_path=path, enabled=False)
    assert service.snapshot()["loaded"] is False
    path.write_text("{not json")
    assert MarketIntelligenceService(cache_path=path, enabled=False).snapshot()["series"] == {}


def test_background_refresh_answers_immediately_then_fills(tmp_path):
    import threading

    gate = threading.Event()

    def slow(source, instant):
        gate.wait(5)
        return fixture_fetch(source, instant)

    service = MarketIntelligenceService(
        cache_path=tmp_path / "m.json", fetcher=slow, clock=lambda: CAPTURED_AT
    )
    first = service.snapshot()
    assert first["refreshing"] is True and first["loaded"] is False
    assert service.snapshot()["refreshing"] is True  # no second refresh is started
    gate.set()
    service.wait()
    done = service.snapshot()
    assert done["loaded"] and not done["refreshing"]


def test_latest_lens_needs_an_observation_newer_than_its_base():
    # Right after a quarter closes, nothing is published past it yet.
    daily = _series([("2026-07-10", 90.0), ("2026-08-10", 100.0), ("2026-09-25", 110.0)])
    move = driver_move(daily, DRIVERS_BY_KEY["brent"], date(2026, 7, 1), date(2026, 9, 30))
    assert move["qtd"] is None and move["latest_is_after_period"] is False
    assert move["latest_beyond_base"] is True
    assert move["latest_change"] == pytest.approx(10)
    # A monthly series published only to the quarter's first month is its own base.
    first_month = _series([("2026-06-01", 90.0), ("2026-07-01", 100.0)], key="PIORECRUSDM")
    iron_ore = DRIVERS_BY_KEY["iron_ore"]
    move = driver_move(first_month, iron_ore, date(2026, 7, 1), date(2026, 9, 30))
    assert move["latest_beyond_base"] is False
    # So is the last print before a quarter the series has not reached at all.
    before = _series([("2026-05-01", 90.0), ("2026-06-01", 100.0)], key="PIORECRUSDM")
    move = driver_move(before, iron_ore, date(2026, 7, 1), date(2026, 9, 30))
    assert move["base"] == 100.0 and move["latest_beyond_base"] is False


def _story(story_id, published_at, url=None):
    return {
        "id": story_id,
        "title": f"Headline {story_id} about crude and the rupee",
        "url": url or f"https://news.example/{story_id}",
        "publisher": "Reuters",
        "publisher_url": "https://www.reuters.com",
        "published_at": published_at,
        "established": True,
    }


def _until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def test_feed_loop_fetches_from_start_and_keeps_news_on_its_own_cadence(tmp_path):
    instant = [CAPTURED_AT]
    calls = []

    def fetch(source, now):
        calls.append(source.key)
        if source.kind == "series":
            return [["2026-09-25", 100.0]]
        return [_story(f"s{len(calls)}", now.isoformat())]

    sources = (fred_source("DCOILBRENTEU", "Brent", "EIA"), news_source("brent", "crude oil"))
    service = MarketIntelligenceService(
        cache_path=tmp_path / "market.json",
        fetcher=fetch,
        clock=lambda: instant[0],
        background=False,
        sources=sources,
        news_refresh_seconds=120,
        feed_tick_seconds=0.01,
    )
    service.start()
    try:
        # Nobody has asked for a page: the process fetched on its own.
        _until(lambda: sorted(calls) == ["DCOILBRENTEU", "news:brent"])
        assert (tmp_path / "market.json").exists()
        instant[0] += timedelta(seconds=120)
        _until(lambda: calls.count("news:brent") == 2)
        # Headlines refresh on their own cadence; series wait for theirs.
        assert calls.count("DCOILBRENTEU") == 1
        assert service.snapshot()["news_fetched_at"] == instant[0].isoformat()
    finally:
        service.stop()
    assert service._feed is None


def test_failing_sources_back_off_and_log_without_their_message(tmp_path, caplog):
    instant = [CAPTURED_AT]
    calls = []

    def fetch(source, now):
        calls.append(now)
        raise RuntimeError("proxy https://user:secret@proxy.example refused")

    service = MarketIntelligenceService(
        cache_path=tmp_path / "market.json",
        fetcher=fetch,
        clock=lambda: instant[0],
        background=False,
        sources=(news_source("brent", "crude oil"),),
        news_refresh_seconds=600,
    )
    for offset, expected in ((0, 1), (59, 1), (60, 2), (60 + 119, 2), (60 + 120, 3)):
        instant[0] = CAPTURED_AT + timedelta(seconds=offset)
        service.snapshot()
        assert len(calls) == expected, offset
    assert "Market source news:brent unavailable (RuntimeError)" in caplog.text
    assert "secret" not in caplog.text


def test_news_feed_lists_each_story_once_newest_first():
    snapshot = {
        "news": {
            "brent": [
                _story("old", "2026-09-27T08:00:00+00:00"),
                _story("both", "2026-09-28T09:00:00+00:00", url="https://news.example/shared"),
            ],
            "usdinr": [
                _story("both", "2026-09-28T09:00:00+00:00", url="https://news.example/shared"),
                _story("new", "2026-09-29T10:00:00+00:00"),
            ],
        }
    }
    feed = news_feed(snapshot)
    assert [story["id"] for story in feed] == ["new", "both", "old"]
    assert feed[1]["driver_labels"] == ["Brent crude", "US dollar in rupees"]
    assert [story["id"] for story in news_feed(snapshot, ["brent"])] == ["both", "old"]
    assert news_feed(snapshot, ["not-a-driver"]) == news_feed(snapshot)


def test_a_stalled_series_download_never_holds_up_the_headlines(tmp_path):
    import threading

    instant = [CAPTURED_AT]
    release = threading.Event()
    news_calls = []

    def fetch(source, now):
        if source.kind == "series":
            release.wait(10)  # FRED trickling
            return [["2026-09-25", 100.0]]
        news_calls.append(now)
        return [_story(f"n{len(news_calls)}", now.isoformat())]

    service = MarketIntelligenceService(
        cache_path=tmp_path / "market.json",
        fetcher=fetch,
        clock=lambda: instant[0],
        sources=(fred_source("DCOILBRENTEU", "Brent", "EIA"), news_source("brent", "crude oil")),
        news_refresh_seconds=120,
    )
    try:
        service.snapshot()
        _until(lambda: len(news_calls) == 1)
        instant[0] += timedelta(seconds=120)
        fresh = instant[0].isoformat()
        _until(lambda: service.snapshot()["news_fetched_at"] == fresh)
        # The series download is still in flight, and the headlines moved on anyway.
        assert service.snapshot()["refreshing"] is True and not release.is_set()
    finally:
        release.set()
        service.wait()
    done = service.snapshot()
    assert done["loaded"] and not done["refreshing"]
