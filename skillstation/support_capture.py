"""Present accepted accessibility requests by candidate."""

def requests_for(requests, candidate):
    return [item for item in requests if item['candidate'] == candidate]
