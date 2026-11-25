"""Nexus-generated wall times; Scout timestamps are not interpreted here."""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def timestamp(value):
    if not value:
        return value
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return value
    if date.tzinfo is None:
        # The production image uses python:slim's UTC clock; development hosts
        # require an explicit historical source zone rather than browser inference.
        zone = os.environ.get('MINO_NEXUS_LEGACY_TIMEZONE') or ('UTC' if os.environ.get('MINO_NEXUS_MODE') == 'production' else '')
        if not zone:
            return value
        date = date.replace(tzinfo=ZoneInfo(zone))
    return date.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
