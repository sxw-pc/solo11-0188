"""Present aggregate consumable stock by part type."""

def available(lots, part):
    return sum(lot['quantity'] for lot in lots if lot['part'] == part)
