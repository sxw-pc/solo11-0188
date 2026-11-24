"""Present payment matching by beneficiary list."""

def paid_for(payments, candidate):
    return [payment for payment in payments if candidate in payment['beneficiaries']]
