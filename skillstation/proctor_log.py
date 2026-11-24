"""Current proctor duty receipt total."""

def hours(rows):
    return sum(row['end'] - row['start'] for row in rows)
