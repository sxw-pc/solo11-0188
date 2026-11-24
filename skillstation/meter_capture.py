"""Present sum of all imported meter deltas."""

def total_kwh(readings):
    return sum(item['end'] - item['start'] for item in readings)
