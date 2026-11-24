"""Per-person payment shares and signed directive attribution.

A payment may name several beneficiaries, but money is never owned by a
beneficiary until the group price table is split into deterministic per-person
shares.  Refund/credit directives are validated here: seal status, actual
payer, sponsor scope, payment clearing state and the undisposed share balance
all gate whether the ledger may post anything.
"""

CLEARED = 'cleared'
CHARGED_BACK = 'charged_back'
VALID = 'valid'
REVOKED = 'revoked'

REFUND = 'refund'
CREDIT = 'credit'
RELEASE = 'release'


class FeeError(ValueError):
    """Raised when allocation or directive attribution cannot be established."""


class FeeCapture:
    """Owns payments, the group price table, seals and share allocation."""

    def __init__(self, payments, seals=None, price_table=None):
        self.payments = []
        for payment in payments:
            row = dict(payment)
            row.setdefault('status', CLEARED)
            if row['status'] not in (CLEARED, CHARGED_BACK):
                raise FeeError(f"unknown payment status: {row['status']}")
            if not row.get('beneficiaries'):
                raise FeeError(f"payment {row.get('id')} has no beneficiaries")
            if row['amount'] <= 0:
                raise FeeError(f"payment {row.get('id')} amount must be positive")
            self.payments.append(row)
        self._payment_ids = {p['id'] for p in self.payments}
        self.seals = seals or {}
        self._table = dict(price_table) if price_table else None
        self._shares = []
        self._share_index = {}
        if self._table is not None:
            self._allocate()

    # ------------------------------------------------------------------ table
    def apply_price_table(self, price_table):
        """Register the (possibly late-arriving) group price table once."""
        if self._table is not None:
            raise FeeError('price table already established; corrections need a new entry, not an overwrite')
        self._table = dict(price_table)
        self._allocate()
        return self._shares

    def _allocate(self):
        table = self._table
        missing = sorted({b for p in self.payments for b in p['beneficiaries'] if b not in table})
        if missing:
            raise FeeError(f'price table missing share for: {",".join(missing)}')
        remaining_due = dict(table)
        shares = []
        for payment in self.payments:
            payment_left = payment['amount']
            for beneficiary in payment['beneficiaries']:
                if payment_left <= 0:
                    break
                part = min(payment_left, remaining_due.get(beneficiary, 0))
                if part <= 0:
                    continue
                share = {
                    'share_id': f"{payment['id']}:{beneficiary}",
                    'payment_id': payment['id'],
                    'payer': payment['payer'],
                    'candidate': beneficiary,
                    'amount': part,
                }
                shares.append(share)
                payment_left -= part
                remaining_due[beneficiary] -= part
            if payment_left != 0:
                raise FeeError(
                    f"payment {payment['id']} has {payment_left} unallocated; price table does not reconcile"
                )
        outstanding = {k: v for k, v in remaining_due.items() if v}
        if outstanding:
            raise FeeError(f'price table dues not covered by payments: {outstanding}')
        self._shares = shares
        self._share_index = {s['share_id']: s for s in shares}

    @property
    def shares(self):
        if not self._shares:
            raise FeeError('price table not yet received; per-person shares cannot be derived')
        return list(self._shares)

    def shares_for(self, candidate):
        return [s for s in self.shares if s['candidate'] == candidate]

    def share(self, share_id):
        return self._share_index[share_id]

    def payment(self, payment_id):
        for payment in self.payments:
            if payment['id'] == payment_id:
                return payment
        raise FeeError(f'unknown payment: {payment_id}')

    def mark_charged_back(self, payment_id):
        payment = self.payment(payment_id)
        if payment['status'] == CHARGED_BACK:
            raise FeeError(f'payment {payment_id} already charged back')
        payment['status'] = CHARGED_BACK

    # -------------------------------------------------------------- directives
    def authorize(self, directive):
        """Validate a signed directive against seals, payer, payment and share.

        Returns the single share the directive is allowed to dispose of.
        The ledger decides whether that share is still undisposed; every other
        rule (seal, sponsor, clearing state, amount) is enforced here.
        """
        for field in ('id', 'type', 'seal', 'payment_id', 'candidate', 'amount'):
            if field not in directive:
                raise FeeError(f"directive missing field: {field}")
        dtype = directive['type']
        if dtype not in (REFUND, CREDIT, RELEASE):
            raise FeeError(f"unknown directive type: {dtype}")
        seal = self.seals.get(directive['seal'])
        if seal is None:
            raise FeeError(f"seal {directive['seal']} is not registered")
        if seal.get('status') != VALID:
            raise FeeError(f"seal {directive['seal']} is {seal.get('status')}, not valid")
        payment = self.payment(directive['payment_id'])
        if seal['holder'] != payment['payer']:
            raise FeeError(
                f"seal holder {seal['holder']} cannot dispose of payment {payment['id']} "
                f"whose actual payer is {payment['payer']}"
            )
        if payment['status'] != CLEARED:
            raise FeeError(
                f"payment {payment['id']} is {payment['status']}; use the chargeback correction flow"
            )
        if directive['amount'] <= 0:
            raise FeeError('directive amount must be positive')
        share_id = f"{directive['payment_id']}:{directive['candidate']}"
        share = self._share_index.get(share_id)
        if share is None:
            raise FeeError(f"no allocated share {share_id}")
        if directive['amount'] != share['amount']:
            raise FeeError(
                f"directive amount {directive['amount']} must equal the full allocated share "
                f"{share['amount']}; a share cannot be split across cash and credit"
            )
        if dtype == CREDIT and not directive.get('credit_to'):
            raise FeeError('credit directive must name credit_to')
        return dict(share)


def paid_for(capture, candidate):
    """Allocated payment shares (not whole payments) paid for a candidate."""
    return capture.shares_for(candidate)
