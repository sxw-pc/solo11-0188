"""Present independent first-match support assignment."""
from .support_capture import requests_for

def assignments(requests, candidates, resources):
    return {candidate: next(resource['id'] for resource in resources
                            if resource['mode'] == requests_for(requests, candidate)[0]['mode'])
            for candidate in candidates}
