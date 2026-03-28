"""Provider adapters for fetching earthquake events from multiple sources."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import aiohttp

logger = logging.getLogger("discord")

USGS_ALL_HOUR_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
EMSC_QUERY_URL = "https://www.seismicportal.eu/fdsnws/event/1/query"
EMSC_MAX_LIMIT = 20000


@dataclass(slots=True)
class EarthquakeEvent:
    source: str
    native_id: str
    place: str
    magnitude: float | None
    url: str | None
    event_time_utc: datetime | None
    updated_time_utc: datetime | None
    pager_alert_level: str | None
    tsunami_potential: bool | None
    depth_km: float | None
    latitude: float | None
    longitude: float | None
    significance: int | None

    @property
    def earthquake_id(self) -> str:
        return f"{self.source}:{self.native_id}"


class EarthquakeSource(Protocol):
    source: str

    async def fetch_events(self, session: aiohttp.ClientSession) -> list[EarthquakeEvent]:
        ...


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _extract_features(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        payload_type = payload.get("type")
        if payload_type == "FeatureCollection":
            features = payload.get("features")
            if isinstance(features, list):
                return [feature for feature in features if isinstance(feature, dict)]
            return []
        if payload_type == "Feature":
            return [payload]
        features = payload.get("features")
        if isinstance(features, list):
            return [feature for feature in features if isinstance(feature, dict)]
    elif isinstance(payload, list):
        return [feature for feature in payload if isinstance(feature, dict)]
    return []


class USGSEarthquakeSource:
    source = "usgs"

    def __init__(self, url: str = USGS_ALL_HOUR_URL):
        self.url = url

    async def fetch_events(self, session: aiohttp.ClientSession) -> list[EarthquakeEvent]:
        async with session.get(self.url) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error("USGS fetch failed (%s): %s", resp.status, body[:500])
                return []
            payload = await resp.json()

        events: list[EarthquakeEvent] = []
        for feature in _extract_features(payload):
            properties = feature.get("properties") or {}
            geometry = feature.get("geometry") or {}
            coordinates = geometry.get("coordinates") or []
            if not isinstance(coordinates, list):
                coordinates = []

            event_time_ms = properties.get("time")
            event_time = None
            if event_time_ms is not None:
                try:
                    event_time = datetime.fromtimestamp(float(event_time_ms) / 1000, tz=timezone.utc)
                except (TypeError, ValueError, OSError):
                    event_time = None

            updated_time_ms = properties.get("updated")
            updated_time = None
            if updated_time_ms is not None:
                try:
                    updated_time = datetime.fromtimestamp(float(updated_time_ms) / 1000, tz=timezone.utc)
                except (TypeError, ValueError, OSError):
                    updated_time = None

            native_id = str(
                feature.get("id")
                or properties.get("code")
                or properties.get("ids")
                or properties.get("time")
            )

            events.append(
                EarthquakeEvent(
                    source=self.source,
                    native_id=native_id,
                    place=str(properties.get("place") or "Unknown location"),
                    magnitude=_as_float(properties.get("mag")),
                    url=properties.get("url"),
                    event_time_utc=event_time,
                    updated_time_utc=updated_time,
                    pager_alert_level=properties.get("alert"),
                    tsunami_potential=bool(properties.get("tsunami"))
                    if properties.get("tsunami") is not None
                    else None,
                    depth_km=_as_float(coordinates[2] if len(coordinates) > 2 else properties.get("depth")),
                    latitude=_as_float(coordinates[1] if len(coordinates) > 1 else None),
                    longitude=_as_float(coordinates[0] if coordinates else None),
                    significance=_as_int(properties.get("sig")),
                )
            )

        return events


class EMSCFdsnEventSource:
    source = "emsc"

    def __init__(
        self,
        url: str = EMSC_QUERY_URL,
        limit: int = 500,
        initial_lookback: timedelta = timedelta(hours=1),
    ):
        self.url = url
        self.limit = max(1, min(limit, EMSC_MAX_LIMIT))
        self.initial_lookback = initial_lookback
        self._updated_after_utc: datetime | None = None

    def _cursor(self) -> datetime:
        if self._updated_after_utc is None:
            return datetime.now(timezone.utc) - self.initial_lookback
        return self._updated_after_utc

    async def fetch_events(self, session: aiohttp.ClientSession) -> list[EarthquakeEvent]:
        cursor = self._cursor()
        params_base = {
            "format": "json",
            "orderby": "time-asc",
            "limit": str(self.limit),
            "updatedafter": cursor.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }

        offset = 1
        max_seen_update = cursor
        events: list[EarthquakeEvent] = []

        while True:
            params = dict(params_base)
            if offset > 1:
                params["offset"] = str(offset)

            async with session.get(self.url, params=params) as resp:
                if resp.status in (204, 404):
                    break
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("EMSC fetch failed (%s): %s", resp.status, body[:500])
                    break
                payload = await resp.json()

            features = _extract_features(payload)
            if not features:
                break

            for feature in features:
                event = self._to_event(feature)
                if event is None:
                    continue
                events.append(event)
                if event.updated_time_utc and event.updated_time_utc > max_seen_update:
                    max_seen_update = event.updated_time_utc
                elif event.event_time_utc and event.event_time_utc > max_seen_update:
                    max_seen_update = event.event_time_utc

            if len(features) < self.limit:
                break

            offset += len(features)

        # Keep a small overlap window to avoid boundary misses between polls.
        self._updated_after_utc = max_seen_update - timedelta(seconds=5)
        return events

    def _to_event(self, feature: dict) -> EarthquakeEvent | None:
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        if not isinstance(coordinates, list):
            coordinates = []

        native_id = properties.get("unid") or feature.get("id") or properties.get("source_id")
        if not native_id:
            return None

        event_time = _parse_iso_datetime(properties.get("time"))
        updated_time = _parse_iso_datetime(properties.get("lastupdate"))
        latitude = _as_float(properties.get("lat"))
        longitude = _as_float(properties.get("lon"))
        if latitude is None and len(coordinates) > 1:
            latitude = _as_float(coordinates[1])
        if longitude is None and coordinates:
            longitude = _as_float(coordinates[0])

        return EarthquakeEvent(
            source=self.source,
            native_id=str(native_id),
            place=str(properties.get("flynn_region") or "Unknown location"),
            magnitude=_as_float(properties.get("mag")),
            url=f"{EMSC_QUERY_URL}?eventid={native_id}&format=json",
            event_time_utc=event_time,
            updated_time_utc=updated_time,
            pager_alert_level=None,
            tsunami_potential=None,
            depth_km=_as_float(properties.get("depth")),
            latitude=latitude,
            longitude=longitude,
            significance=None,
        )


def parse_source_names(raw_value: str | None) -> list[str]:
    if not raw_value:
        return ["usgs"]
    names = [value.strip().lower() for value in raw_value.split(",")]
    filtered = [name for name in names if name]
    return filtered or ["usgs"]


def build_earthquake_sources(source_names: list[str] | None = None) -> list[EarthquakeSource]:
    names = source_names or ["usgs"]
    sources: list[EarthquakeSource] = []
    seen = set()

    for name in names:
        normalized = name.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)

        if normalized == "usgs":
            sources.append(USGSEarthquakeSource())
            continue
        if normalized == "emsc":
            sources.append(EMSCFdsnEventSource())
            continue
        logger.warning("Unknown earthquake source '%s' skipped.", name)

    if not sources:
        sources.append(USGSEarthquakeSource())

    return sources


async def fetch_events_from_sources(
    session: aiohttp.ClientSession, sources: list[EarthquakeSource]
) -> list[EarthquakeEvent]:
    if not sources:
        return []

    tasks = [source.fetch_events(session) for source in sources]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    events_by_id: dict[str, EarthquakeEvent] = {}
    for source, result in zip(sources, results, strict=False):
        if isinstance(result, Exception):
            logger.error("Source '%s' failed: %s", source.source, result)
            continue

        for event in result:
            existing = events_by_id.get(event.earthquake_id)
            if existing is None:
                events_by_id[event.earthquake_id] = event
                continue

            current_update = event.updated_time_utc or event.event_time_utc or datetime.min.replace(
                tzinfo=timezone.utc
            )
            existing_update = (
                existing.updated_time_utc
                or existing.event_time_utc
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            if current_update > existing_update:
                events_by_id[event.earthquake_id] = event

    events = list(events_by_id.values())
    events.sort(
        key=lambda item: item.event_time_utc or item.updated_time_utc or datetime.min.replace(
            tzinfo=timezone.utc
        ),
        reverse=True,
    )
    return events
