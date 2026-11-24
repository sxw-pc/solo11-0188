"""Current remaining proctor duty capacity."""
from .proctor_log import hours

def remaining(rows, limit=8):
    used = hours(rows)
    return {'used': used, 'remaining': limit - used}
