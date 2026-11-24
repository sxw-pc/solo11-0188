"""Current roster receipt from the first available page."""
from .portal_capture import read_page

def build(page):
    bookings = read_page(page)
    return {'complete': bool(bookings), 'bookings': bookings}
