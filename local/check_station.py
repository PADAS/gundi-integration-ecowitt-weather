"""Check the Ecowitt pull logic against a real station, without sending to Gundi.

Checks the credentials, fetches the station's current reading, and prints what
the pull_observations action would make of it: the raw units, reading times
and battery keys, the observation, and any alerts. Only read-only Ecowitt API
calls are made.

Usage, from the repo root:

    export ECOWITT_APPLICATION_KEY=... ECOWITT_API_KEY=...
    python local/check_station.py --mac AA:BB:CC:DD:EE:FF --lat 0.1 --lon 36.5

Add `--save response.json` to keep the raw response, e.g. as a test fixture.
"""
import argparse
import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.services.ecowitt import (  # noqa: E402
    build_observation,
    check_alert_conditions,
    fetch_realtime_data,
    latest_reading_time,
    parse_ecowitt_data,
    validate_ecowitt_credentials,
)


def _readings(data):
    """Yield (path, reading) for every {value, unit, time} reading in the response."""
    for category, fields in data.items():
        if isinstance(fields, dict):
            for name, reading in fields.items():
                if isinstance(reading, dict) and "value" in reading:
                    yield f"{category}.{name}", reading


def _print_json(label, value):
    print(f"{label}:", json.dumps(value, indent=2, ensure_ascii=False))


async def main(args):
    try:
        application_key = os.environ["ECOWITT_APPLICATION_KEY"]
        api_key = os.environ["ECOWITT_API_KEY"]
    except KeyError as e:
        sys.exit(f"Set {e.args[0]} in the environment")
    mac = args.mac.strip().upper()

    valid = await validate_ecowitt_credentials(application_key=application_key, api_key=api_key)
    print("credentials valid:", valid)
    if not valid:
        sys.exit(1)

    data = await fetch_realtime_data(application_key=application_key, api_key=api_key, mac=mac)
    if args.save:
        pathlib.Path(args.save).write_text(json.dumps(data, indent=2, ensure_ascii=False))
        print("raw response saved to", args.save)

    readings = dict(_readings(data))
    print("categories:", sorted(data))
    _print_json("units", {path: reading.get("unit") for path, reading in readings.items()})
    print("raw reading times:", sorted({str(r["time"]) for r in readings.values() if "time" in r}))
    _print_json("battery", data.get("battery"))

    parsed = parse_ecowitt_data(data)
    reading_time = latest_reading_time(data)
    print("latest reading time:", reading_time or "none found (the action would use the fetch time)")
    recorded_at = reading_time.isoformat() if reading_time else "<fetch time>"

    observation = build_observation(
        parsed_data=parsed,
        source_id=mac,
        source_name=args.name,
        subject_type="weather-station",
        lat=args.lat,
        lon=args.lon,
        recorded_at=recorded_at,
    )
    _print_json("observation", observation)
    _print_json("alerts", check_alert_conditions(parsed, mac, args.lat, args.lon, recorded_at))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mac", required=True, help="Station MAC address")
    parser.add_argument("--lat", type=float, required=True, help="Station latitude")
    parser.add_argument("--lon", type=float, required=True, help="Station longitude")
    parser.add_argument("--name", default="Weather Station", help="Station name for the observation")
    parser.add_argument("--save", metavar="PATH", help="Write the raw real_time response to PATH")
    asyncio.run(main(parser.parse_args()))
