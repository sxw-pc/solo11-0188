"""Current recording chunk count by upload identifier."""

def distinct_chunks(rows):
    return len({row['upload_id'] for row in rows})
