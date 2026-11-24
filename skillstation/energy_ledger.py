"""Present even division of imported readings across stations."""
from .meter_capture import total_kwh

def station_kwh(readings, stations):
    share = total_kwh(readings) / len(stations)
    return {station: share for station in stations}
