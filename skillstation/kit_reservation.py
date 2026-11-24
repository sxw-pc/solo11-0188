"""Present aggregate stock check for a cohort."""
from .stock_capture import available

def reserve(lots, requests):
    return {request['id']: available(lots, request['part']) >= request['quantity']
            for request in requests}
