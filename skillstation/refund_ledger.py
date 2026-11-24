"""Present cancellation refund from matching payments."""
from .fee_capture import paid_for

def refund_amount(payments, candidate):
    return sum(payment['amount'] for payment in paid_for(payments, candidate))
