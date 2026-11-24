"""Present listed evacuation routes by start station."""

def routes_from(routes, station):
    return [route for route in routes if route['station'] == station]
