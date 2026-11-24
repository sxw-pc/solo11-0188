"""Present fastest listed single route for a station."""
from .incident_capture import routes_from

def choose(routes, station):
    return min(routes_from(routes, station), key=lambda route: route['minutes'])['id']
