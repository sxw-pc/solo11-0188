"""Append-only double-entry ledger for cancellation refunds and resit credits.

Money movement is driven entirely by the per-person shares and signed
directives produced by :mod:`skillstation.fee_capture`.  Every movement is a
balanced pair of postings (cash, revenue, receivable, credit liability).
Corrections after a settlement statement, or after a later revocation /
chargeback, are always *new* entries that reference the earlier ones; old
entries are never overwritten.

Share state machine:

    allocated -> cash_refunded   (refund directive; the share exits as cash)
              -> credit_locked    (credit directive; the share becomes a credit)
              -> consumed         (the candidate attends and the fee is earned)

A credit_locked share is either consumed by the credit recipient or released
back to its original payer; it can never be refunded in cash a second time.
"""
from .fee_capture import REFUND, CREDIT, RELEASE, CHARGED_BACK, FeeError

# Share states
ALLOCATED = 'allocated'
CASH_REFUNDED = 'cash_refunded'
CREDIT_LOCKED = 'credit_locked'
CONSUMED = 'consumed'

# Accounts
CASH = 'cash'
REVENUE = 'revenue'
RECEIVABLE = 'receivable'
CREDIT_LIABILITY = 'credit_liability'
UNEARNED_FEE = 'unearned_fee'
CHARGEBACK_LOSS = 'chargeback_loss'

# Entry types
E_OPENING = 'opening_receipt'
E_SETTLEMENT = 'settlement'
E_SETTLEMENT_CORRECTION = 'settlement_correction'
E_CASH_REFUND = 'cash_refund'
E_CREDIT_LOCK = 'credit_lock'
E_CREDIT_CONSUME = 'credit_consume'
E_CREDIT_RELEASE = 'credit_release'
E_ATTEND = 'attend'
E_CHARGEBACK = 'chargeback'


class RefundLedger:
    def __init__(self, capture):
        self.capture = capture
        self.entries = []
        self._seq = 0
        self._share_state = {s['share_id']: ALLOCATED for s in capture.shares}
        self._credit = {}          # credit id -> lock entry payload
        self._settled = set()      # share ids already on a settlement statement
        self._entry_by_directive = {}
        self._opening = {}         # payment id -> opening entry id
        self._post_opening_receipts()

    def _post_opening_receipts(self):
        """Book every cleared payment as cash in against an unearned liability.

        All later movements then reconcile against the original receipt; the
        ledger never invents money that no payment brought in.
        """
        for payment in self.capture.payments:
            if payment['status'] == 'cleared':
                entry = self._post(
                    E_OPENING,
                    [payment['id']],
                    [(CASH, payment['amount'], 'debit'),
                     (UNEARNED_FEE, payment['amount'], 'credit')],
                    extra={
                        'payment_id': payment['id'],
                        'payer': payment['payer'],
                        'amount': payment['amount'],
                        'note': 'original payment receipt from actual payer',
                    },
                )
                self._opening[payment['id']] = entry['id']

    # ----------------------------------------------------------------- helpers
    def _post(self, kind, refs, postings, directive_id=None, extra=None):
        if postings:
            debit = sum(amount for account, amount, side in postings if side == 'debit')
            credit = sum(amount for account, amount, side in postings if side == 'credit')
            if debit != credit:
                raise FeeError(f'unbalanced entry {kind}: {debit} != {credit}')
        self._seq += 1
        entry = {
            'seq': self._seq,
            'id': f'e{self._seq:03d}',
            'type': kind,
            'refs': list(refs),
            'postings': [
                {'account': account, 'amount': amount, 'side': side}
                for account, amount, side in postings
            ],
        }
        if directive_id is not None:
            entry['directive_id'] = directive_id
        if extra:
            entry.update(extra)
        self.entries.append(entry)
        return entry

    def _require_undisposed(self, share):
        state = self._share_state[share['share_id']]
        if state != ALLOCATED:
            raise FeeError(
                f"share {share['share_id']} is {state}; the same share cannot be refunded or credited twice"
            )
        if share['share_id'] in self._settled:
            raise FeeError(
                f"share {share['share_id']} is on an issued settlement statement; "
                'post a referenced correcting entry instead of changing the old one'
            )

    # ------------------------------------------------------------ settlement
    def issue_settlement(self, statement_id, candidates):
        """Issue a settlement statement for still-allocated shares.

        The statement itself moves no money; it only freezes the named shares
        so that every later cancellation must arrive as a correcting entry
        referencing the statement.
        """
        share_ids = []
        for candidate in candidates:
            for share in self.capture.shares_for(candidate):
                if self._share_state[share['share_id']] != ALLOCATED:
                    raise FeeError(
                        f"cannot settle {share['share_id']}: state is "
                        f"{self._share_state[share['share_id']]}"
                    )
                share_ids.append(share['share_id'])
        entry = self._post(E_SETTLEMENT, [statement_id], [], extra={
            'statement_id': statement_id,
            'share_ids': list(share_ids),
            'note': 'settlement statement issued; no cash movement',
        })
        self._settled.update(share_ids)
        return entry

    def open_settlement_correction(self, statement_id, correction_id, share_ids):
        """Reopen settled shares after a later cancellation/revocation.

        The statement itself is never modified: this posts a new correction
        entry that references both the statement and the affected shares, and
        the subsequent refund/credit entries carry the same reference chain.
        """
        unknown = [sid for sid in share_ids if sid not in self._share_state]
        if unknown:
            raise FeeError(f'unknown shares: {unknown}')
        not_frozen = [sid for sid in share_ids if sid not in self._settled]
        if not_frozen:
            raise FeeError(f'shares not covered by an issued statement: {not_frozen}')
        entry = self._post(
            E_SETTLEMENT_CORRECTION,
            [correction_id, statement_id, *share_ids],
            [],
            extra={
                'correction_id': correction_id,
                'statement_id': statement_id,
                'share_ids': list(share_ids),
                'note': 'reopen settled shares for a referenced correcting movement',
            },
        )
        for sid in share_ids:
            self._settled.discard(sid)
        self._correction_refs = getattr(self, '_correction_refs', {})
        for sid in share_ids:
            self._correction_refs[sid] = (correction_id, statement_id)
        return entry

    # ------------------------------------------------------------ directives
    def apply_directive(self, directive):
        directive_id = directive['id']
        if directive_id in self._entry_by_directive:
            raise FeeError(f"directive {directive_id} already posted as {self._entry_by_directive[directive_id]}")
        dtype = directive['type']

        if dtype == RELEASE:
            return self._apply_release(directive, directive_id)

        share = self.capture.authorize(directive)

        if dtype == REFUND:
            self._require_undisposed(share)
            correction_refs = getattr(self, '_correction_refs', {}).get(share['share_id'], ())
            entry = self._post(
                E_CASH_REFUND,
                [share['share_id'], share['payment_id'], *correction_refs],
                [(CASH, share['amount'], 'credit'), (UNEARNED_FEE, share['amount'], 'debit')],
                directive_id,
                {
                    'share_id': share['share_id'],
                    'payment_id': share['payment_id'],
                    'payer': share['payer'],
                    'candidate': share['candidate'],
                    'amount': share['amount'],
                    'to_account': directive.get('to_account', 'original payment account'),
                    'corrects': list(correction_refs),
                },
            )
            self._share_state[share['share_id']] = CASH_REFUNDED

        elif dtype == CREDIT:
            self._require_undisposed(share)
            credit_id = directive['credit_to']
            if credit_id in self._credit:
                raise FeeError(f'credit {credit_id} already exists')
            correction_refs = getattr(self, '_correction_refs', {}).get(share['share_id'], ())
            entry = self._post(
                E_CREDIT_LOCK,
                [share['share_id'], share['payment_id'], *correction_refs],
                [(UNEARNED_FEE, share['amount'], 'debit'), (CREDIT_LIABILITY, share['amount'], 'credit')],
                directive_id,
                {
                    'share_id': share['share_id'],
                    'payment_id': share['payment_id'],
                    'payer': share['payer'],
                    'candidate': share['candidate'],
                    'amount': share['amount'],
                    'credit_to': credit_id,
                    'locked': True,
                    'corrects': list(correction_refs),
                },
            )
            self._share_state[share['share_id']] = CREDIT_LOCKED
            self._credit[credit_id] = entry

        self._entry_by_directive[directive_id] = entry['id']
        return entry

    def _apply_release(self, directive, directive_id):
        for field in ('id', 'seal', 'payment_id', 'amount', 'credit_from'):
            if field not in directive:
                raise FeeError(f"release directive missing field: {field}")
        seal = self.capture.seals.get(directive['seal'])
        if seal is None or seal.get('status') != 'valid':
            raise FeeError(f"seal {directive.get('seal')} is missing or not valid")
        payment = self.capture.payment(directive['payment_id'])
        if seal['holder'] != payment['payer']:
            raise FeeError('only the actual payer may release a credit their payment funded')
        if payment['status'] != 'cleared':
            raise FeeError(f"payment {payment['id']} is {payment['status']}; use the chargeback correction flow")
        lock = self._credit.get(directive['credit_from'])
        if lock is None:
            raise FeeError(f"no locked credit for {directive['credit_from']}")
        if not lock.get('locked'):
            raise FeeError(f"credit {directive['credit_from']} is no longer locked")
        share_id = lock['share_id']
        share = self.capture.share(share_id)
        if directive['payment_id'] != share['payment_id'] or directive['amount'] != share['amount']:
            raise FeeError('release directive does not match the credit backing share')
        # The credit liability unwinds into an unearned obligation, which is
        # reclassified as a recoverable claim on the original payer carrying a
        # full loss allowance, so no net loss is recognized.
        entry = self._post(
            E_CREDIT_RELEASE,
            [lock['id'], share_id, share['payment_id']],
            [(CREDIT_LIABILITY, share['amount'], 'debit'),
             (UNEARNED_FEE, share['amount'], 'credit'),
             (RECEIVABLE, share['amount'], 'debit'),
             (CHARGEBACK_LOSS, share['amount'], 'credit')],
            directive_id,
            {
                'share_id': share_id,
                'payment_id': share['payment_id'],
                'payer': share['payer'],
                'candidate': share['candidate'],
                'amount': share['amount'],
                'credit_from': directive['credit_from'],
                'note': 'resit canceled again; amount recoverable from the original payer',
            },
        )
        lock['locked'] = False
        lock['released_by'] = entry['id']
        self._entry_by_directive[directive_id] = entry['id']
        return entry

    def consume_credit(self, credit_id, note_id):
        """The resit candidate actually sits the later exam: credit is earned."""
        lock = self._credit.get(credit_id)
        if lock is None:
            raise FeeError(f'unknown credit: {credit_id}')
        if not lock.get('locked'):
            raise FeeError(f'credit {credit_id} is not available')
        share = self.capture.share(lock['share_id'])
        entry = self._post(
            E_CREDIT_CONSUME,
            [lock['id'], lock['share_id']],
            [(CREDIT_LIABILITY, share['amount'], 'debit'), (REVENUE, share['amount'], 'credit')],
            extra={'credit_id': credit_id, 'share_id': share['share_id'], 'amount': share['amount'], 'note_id': note_id},
        )
        lock['locked'] = False
        lock['consumed_by'] = entry['id']
        return entry

    def mark_attended(self, candidate, note_id):
        """Candidate sits the exam: allocated shares become earned revenue."""
        posted = []
        for share in self.capture.shares_for(candidate):
            if self._share_state[share['share_id']] != ALLOCATED:
                raise FeeError(
                    f"cannot mark {share['share_id']} attended: {self._share_state[share['share_id']]}"
                )
            entry = self._post(
                E_ATTEND,
                [share['share_id'], note_id],
                [(UNEARNED_FEE, share['amount'], 'debit'), (REVENUE, share['amount'], 'credit')],
                extra={
                    'share_id': share['share_id'],
                    'candidate': candidate,
                    'amount': share['amount'],
                    'note_id': note_id,
                    'note': 'exam sat; unearned fee recognized as retained revenue',
                },
            )
            self._share_state[share['share_id']] = CONSUMED
            posted.append(entry)
        if not posted:
            raise FeeError(f'no allocated share for {candidate}')
        return posted

    # ------------------------------------------------------------- chargeback
    def register_chargeback(self, payment_id, chargeback_id):
        """A later revocation/chargeback reverses a payment with new entries."""
        payment = self.capture.payment(payment_id)
        if payment['status'] == CHARGED_BACK:
            raise FeeError(f'payment {payment_id} already has a chargeback entry')
        correction = []
        for share in self.capture.shares:
            if share['payment_id'] != payment_id:
                continue
            state = self._share_state[share['share_id']]
            if state == ALLOCATED or state == CONSUMED:
                # Reverse the original receipt; the cash claim on the payer is
                # fully recoverable (receivable debit) and the unearned fee is
                # written down against a loss allowance that nets the claim.
                postings = [
                    (CASH, share['amount'], 'credit'),
                    (RECEIVABLE, share['amount'], 'debit'),
                    (UNEARNED_FEE, share['amount'], 'debit'),
                    (CHARGEBACK_LOSS, share['amount'], 'credit'),
                ]
                kind = E_CHARGEBACK
                detail = {'reason': 'paid fee reversed; claimed back from actual payer',
                          'revenue_kept_for_candidate': share['amount'],
                          'loss_allowance_nets_receivable': True}
            elif state == CASH_REFUNDED:
                # Two balanced pairs: cash clawed back is a real loss; the
                # unearned obligation is restored and reclassified as a claim
                # on the payer.  Unlike the never-refunded case, no allowance
                # covers the loss because cash has physically left twice.
                postings = [
                    (CASH, share['amount'], 'credit'),
                    (CHARGEBACK_LOSS, share['amount'], 'debit'),
                    (UNEARNED_FEE, share['amount'], 'credit'),
                    (RECEIVABLE, share['amount'], 'debit'),
                ]
                kind = E_CHARGEBACK
                detail = {'reason': 'claws back cash previously returned to the payer',
                          'revenue_kept_for_candidate': 0}
            elif state == CREDIT_LOCKED:
                credit_id = next((cid for cid, lock in self._credit.items()
                                  if lock['share_id'] == share['share_id'] and lock.get('locked')), None)
                if credit_id is None:
                    raise FeeError(
                        f"share {share['share_id']} is credit_locked but no live credit found"
                    )
                # The locked credit is voided and its backing cash is reversed;
                # the amount is a claim on the payer against a loss allowance.
                postings = [
                    (CASH, share['amount'], 'credit'),
                    (RECEIVABLE, share['amount'], 'debit'),
                    (CREDIT_LIABILITY, share['amount'], 'debit'),
                    (CHARGEBACK_LOSS, share['amount'], 'credit'),
                ]
                kind = E_CHARGEBACK
                detail = {'reason': 'credit voided; backing cash reversed and claimed from payer',
                          'voided_credit': credit_id, 'reissue_on_recovery': True,
                          'revenue_kept_for_candidate': 0}
                self._credit[credit_id]['locked'] = False
                self._credit[credit_id]['voided_by_chargeback'] = True
            else:  # pragma: no cover - exhaustive state machine
                raise FeeError(f'cannot correct share in state {state}')
            correction.append(self._post(
                kind,
                [chargeback_id, share['share_id'], payment_id],
                postings,
                extra={
                    'chargeback_id': chargeback_id,
                    'payment_id': payment_id,
                    'payer': share['payer'],
                    'share_id': share['share_id'],
                    'candidate': share['candidate'],
                    'amount': share['amount'],
                    'share_state_before': state,
                    **detail,
                },
            ))
        if not correction:
            raise FeeError(f'no allocated shares for payment {payment_id}')
        total = sum(e['amount'] for e in correction)
        if total != payment['amount']:  # defensive: every cent of the payment must be corrected
            raise FeeError(f'chargeback {total} does not cover payment {payment["amount"]}')
        self.capture.mark_charged_back(payment_id)
        return correction

    # ---------------------------------------------------------------- reads
    def _account_totals(self):
        totals = {CASH: 0, REVENUE: 0, RECEIVABLE: 0, CREDIT_LIABILITY: 0,
                  UNEARNED_FEE: 0, CHARGEBACK_LOSS: 0}
        for entry in self.entries:
            for posting in entry['postings']:
                sign = 1 if posting['side'] == 'debit' else -1
                totals[posting['account']] += sign * posting['amount']
        return totals

    def _posting_sum(self, account, side):
        return sum(
            posting['amount']
            for entry in self.entries
            for posting in entry['postings']
            if posting['account'] == account and posting['side'] == side
        )

    def fund_flow(self):
        """Cash refund stream and retained revenue for the current scenario."""
        totals = self._account_totals()
        opening_total = sum(
            p['amount'] for p in self.capture.payments if p['status'] == 'cleared'
        )
        refund_entries = [
            {
                'entry_id': e['id'],
                'to_payer': e['payer'],
                'payment_id': e['payment_id'],
                'candidate': e['candidate'],
                'amount': e['amount'],
                'to_account': e['to_account'],
                'corrects': e.get('corrects', []),
            }
            for e in self.entries if e['type'] == E_CASH_REFUND
        ]
        chargeback_entries = [
            {
                'entry_id': e['id'],
                'chargeback_id': e['chargeback_id'],
                'payment_id': e['payment_id'],
                'payer': e['payer'],
                'candidate': e['candidate'],
                'amount': e['amount'],
                'state_before': e['share_state_before'],
            }
            for e in self.entries if e['type'] == E_CHARGEBACK
        ]
        refunded = sum(item['amount'] for item in refund_entries)
        charged_back = sum(item['amount'] for item in chargeback_entries)
        return {
            'opening_receipts': opening_total,
            'cash_refunds': refund_entries,
            'cash_refund_total': refunded,
            'chargebacks': chargeback_entries,
            'chargeback_total': charged_back,
            # full cash position including the original receipts (debit balance)
            'cash_balance': totals[CASH],
            # movement caused by this scenario alone (refunds + chargebacks)
            'scenario_cash_movement': -(refunded + charged_back),
            # revenue and liability accounts carry credit balances -> flip sign
            'retained_revenue': -totals[REVENUE],
            'unearned_fee_balance': -totals[UNEARNED_FEE],
            'credit_liability': -totals[CREDIT_LIABILITY],
            # receivable is an asset (debit balance); gross chargeback loss and
            # its loss allowances are reported separately, net loss = gross - allowance
            'receivable_from_payers': max(totals[RECEIVABLE], 0),
            'chargeback_loss': self._posting_sum(CHARGEBACK_LOSS, 'debit'),
            'chargeback_loss_allowance': self._posting_sum(CHARGEBACK_LOSS, 'credit'),
            'net_chargeback_loss': (
                self._posting_sum(CHARGEBACK_LOSS, 'debit')
                - self._posting_sum(CHARGEBACK_LOSS, 'credit')
            ),
        }

    def credits(self):
        """Available (still locked) resit credits, keyed by recipient."""
        available = []
        for credit_id, lock in self._credit.items():
            if lock.get('locked'):
                available.append({
                    'credit_id': credit_id,
                    'amount': lock['amount'],
                    'backed_by_payment': lock['payment_id'],
                    'backed_by_payer': lock['payer'],
                    'from_candidate': lock['candidate'],
                    'share_id': lock['share_id'],
                    'locked': True,
                })
        return {
            'available': available,
            'available_total': sum(item['amount'] for item in available),
            'all_credits': [
                {
                    'credit_id': cid,
                    'amount': lock['amount'],
                    'locked': bool(lock.get('locked')),
                    'consumed_by': lock.get('consumed_by'),
                    'released_by': lock.get('released_by'),
                    'voided_by_chargeback': lock.get('voided_by_chargeback', False),
                }
                for cid, lock in self._credit.items()
            ],
        }

    def payment_balances(self):
        """Per original payment: allocated, refunded, credited, consumed, left."""
        rows = []
        for payment in self.capture.payments:
            amount = payment['amount']
            refunded = credited = consumed = 0
            for share in self.capture.shares:
                if share['payment_id'] != payment['id']:
                    continue
                state = self._share_state[share['share_id']]
                if state == CASH_REFUNDED:
                    refunded += share['amount']
                elif state == CREDIT_LOCKED:
                    credited += share['amount']
                elif state == CONSUMED:
                    consumed += share['amount']
            undisposed = amount - refunded - credited - consumed
            rows.append({
                'payment_id': payment['id'],
                'payer': payment['payer'],
                'status': payment['status'],
                'amount': amount,
                'cash_refunded': refunded,
                'credit_locked': credited,
                'consumed_revenue': consumed,
                'undisposed_balance': undisposed,
            })
        return rows

    def share_states(self):
        return [
            {
                'share_id': share['share_id'],
                'payment_id': share['payment_id'],
                'payer': share['payer'],
                'candidate': share['candidate'],
                'amount': share['amount'],
                'state': self._share_state[share['share_id']],
            }
            for share in self.capture.shares
        ]


def refund_amount(ledger, candidate):
    """Cash actually refunded for a candidate's shares (never the gross match)."""
    return sum(
        entry['amount']
        for entry in ledger.entries
        if entry['type'] == E_CASH_REFUND and entry.get('candidate') == candidate
    )
