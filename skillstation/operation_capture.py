"""Current action name aggregation from offline devices."""

def actions(records):
    return {row['action'] for row in records}
