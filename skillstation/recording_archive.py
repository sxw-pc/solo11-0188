"""Current archive completeness from received upload count."""
from .chunk_capture import distinct_chunks

def assess(rows, expected=3):
    return {'chunks': distinct_chunks(rows), 'status': '完整' if distinct_chunks(rows) >= expected else '缺段'}
