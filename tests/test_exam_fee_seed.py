import copy
import json
import tempfile
import unittest
from pathlib import Path

from skillstation.fee_capture import FeeCapture, FeeError, paid_for, REFUND
from skillstation.refund_ledger import (
    RefundLedger,
    refund_amount,
    E_CASH_REFUND,
    E_CREDIT_LOCK,
    E_CREDIT_RELEASE,
    E_CHARGEBACK,
    E_SETTLEMENT,
    CASH_REFUNDED,
    CREDIT_LOCKED,
    CONSUMED,
    ALLOCATED,
)
from skillstation.core import connect, import_file, report

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'shared-exam-fee.json'


class FeeLedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def capture(self, data=None, with_table=True):
        data = data or self.data
        return FeeCapture(
            copy.deepcopy(data['payments']),
            seals=copy.deepcopy(data.get('seals')),
            price_table=copy.deepcopy(data['price_table']) if with_table else None,
        )

    def ledger(self, capture=None):
        return RefundLedger(capture or self.capture())

    # ------------------------------------------------------------- allocation
    def test_per_person_shares_split_group_payment_not_half_of_600(self):
        capture = self.capture()
        shares = capture.shares
        self.assertEqual(shares, self.data['expected_allocation'])
        # C1 owns only its 300 share of the 600 group payment, never the full 600
        c1 = paid_for(capture, 'C1')
        self.assertEqual([s['payment_id'] for s in c1], ['p1'])
        self.assertEqual(sum(s['amount'] for s in c1), 300)
        # C2 retains the full 400 exam fee: 300 from the group payment + 100 own top-up
        c2 = paid_for(capture, 'C2')
        self.assertEqual(sum(s['amount'] for s in c2), 400)
        self.assertEqual({(s['payment_id'], s['payer']) for s in c2},
                         {('p1', 'Employer-E'), ('p2', 'C2')})

    def test_late_price_table_blocks_refunds_until_it_arrives(self):
        capture = self.capture(with_table=False)
        with self.assertRaises(FeeError):
            capture.shares_for('C1')
        with self.assertRaises(FeeError):
            RefundLedger(capture)
        capture.apply_price_table(self.data['price_table'])
        self.assertEqual(sum(s['amount'] for s in capture.shares_for('C1')), 300)
        # the table can only be established once: corrections are new entries, never overwrites
        with self.assertRaises(FeeError):
            capture.apply_price_table({'C1': 300, 'C2': 400})

    def test_price_table_must_reconcile_with_payments(self):
        bad = copy.deepcopy(self.data)
        bad['price_table'] = {'C1': 200, 'C2': 400}  # only 600 of 700 covered
        with self.assertRaises(FeeError):
            self.capture(bad)
        missing = copy.deepcopy(self.data)
        missing['price_table'] = {'C1': 300}
        with self.assertRaises(FeeError):
            self.capture(missing)

    # ----------------------------------------------------------- path A: cash
    def test_path_a_refunds_300_to_actual_payer_and_retains_c2_400(self):
        ledger = self.ledger()
        ledger.mark_attended('C2', 'attendance-C2')
        entry = ledger.apply_directive(self.data['directives']['refund_c1_to_employer'])

        self.assertEqual(entry['type'], E_CASH_REFUND)
        self.assertEqual(entry['to_account'], 'Employer-E original payment account')
        self.assertEqual(entry['payer'], 'Employer-E')
        self.assertEqual(refund_amount(ledger, 'C1'), 300)          # never 600
        self.assertEqual(refund_amount(ledger, 'C2'), 0)

        flow = ledger.fund_flow()
        self.assertEqual(flow['cash_refund_total'], 300)
        self.assertEqual(flow['scenario_cash_movement'], -300)
        self.assertEqual(flow['cash_balance'], 400)               # 700 received - 300 refunded
        self.assertEqual(flow['retained_revenue'], 400)            # C2 fee fully retained
        self.assertEqual(flow['receivable_from_payers'], 0)

        balances = {row['payment_id']: row for row in ledger.payment_balances()}
        self.assertEqual(balances['p1']['cash_refunded'], 300)
        self.assertEqual(balances['p1']['consumed_revenue'], 300)
        self.assertEqual(balances['p1']['undisposed_balance'], 0)
        self.assertEqual(balances['p2']['consumed_revenue'], 100)  # C2 own top-up never moved
        self.assertEqual(balances['p2']['undisposed_balance'], 0)

        states = {s['share_id']: s['state'] for s in ledger.share_states()}
        self.assertEqual(states['p1:C1'], CASH_REFUNDED)
        self.assertEqual(states['p1:C2'], CONSUMED)
        self.assertEqual(states['p2:C2'], CONSUMED)
        self.assertEqual(ledger.credits()['available_total'], 0)

    def test_path_a_double_refund_is_rejected(self):
        ledger = self.ledger()
        ledger.apply_directive(self.data['directives']['refund_c1_to_employer'])
        with self.assertRaises(FeeError):
            ledger.apply_directive(self.data['directives']['refund_c1_to_employer'])
        # the same share cannot then be converted into a credit either
        with self.assertRaises(FeeError):
            ledger.apply_directive(self.data['directives']['credit_c1_to_c3'])
        self.assertEqual(len(ledger.entries), 3)                  # 2 opening receipts + 1 refund

    # --------------------------------------------------------- path B: credit
    def test_path_b_locks_300_credit_for_c3_with_no_cash_refund(self):
        ledger = self.ledger()
        ledger.mark_attended('C2', 'attendance-C2')
        entry = ledger.apply_directive(self.data['directives']['credit_c1_to_c3'])

        self.assertEqual(entry['type'], E_CREDIT_LOCK)
        self.assertEqual(entry['credit_to'], 'C3')
        self.assertEqual(entry['payer'], 'Employer-E')
        self.assertEqual(refund_amount(ledger, 'C1'), 0)             # no cash leaves

        flow = ledger.fund_flow()
        self.assertEqual(flow['cash_refund_total'], 0)
        self.assertEqual(flow['scenario_cash_movement'], 0)
        self.assertEqual(flow['cash_balance'], 700)                # nothing leaves in cash
        self.assertEqual(flow['credit_liability'], 300)
        self.assertEqual(flow['retained_revenue'], 400)

        credits = ledger.credits()
        self.assertEqual(credits['available_total'], 300)
        self.assertEqual(credits['available'][0]['credit_id'], 'C3')
        self.assertEqual(credits['available'][0]['backed_by_payer'], 'Employer-E')

        balances = {row['payment_id']: row for row in ledger.payment_balances()}
        self.assertEqual(balances['p1']['credit_locked'], 300)
        self.assertEqual(balances['p1']['cash_refunded'], 0)
        self.assertEqual(balances['p1']['consumed_revenue'], 300)
        self.assertEqual(balances['p1']['undisposed_balance'], 0)
        states = {s['share_id']: s['state'] for s in ledger.share_states()}
        self.assertEqual(states['p1:C1'], CREDIT_LOCKED)

    def test_credit_consumed_by_c3_releases_liability_into_revenue(self):
        ledger = self.ledger()
        ledger.apply_directive(self.data['directives']['credit_c1_to_c3'])
        ledger.consume_credit('C3', 'attendance-C3')
        self.assertEqual(ledger.credits()['available_total'], 0)
        self.assertEqual(ledger.fund_flow()['retained_revenue'], 300)
        with self.assertRaises(FeeError):
            ledger.consume_credit('C3', 'attendance-C3-again')

    def test_c3_cancels_again_credit_is_traceable_to_original_payer(self):
        ledger = self.ledger()
        ledger.mark_attended('C2', 'attendance-C2')
        ledger.apply_directive(self.data['directives']['credit_c1_to_c3'])
        release = ledger.apply_directive(self.data['directives']['release_c3_to_employer'])

        self.assertEqual(release['type'], E_CREDIT_RELEASE)
        self.assertIn(release['refs'][0], [e['id'] for e in ledger.entries if e['type'] == E_CREDIT_LOCK])
        # the released amount is a claim on the original payer; the original payment is not cash-refunded
        flow = ledger.fund_flow()
        self.assertEqual(flow['cash_refund_total'], 0)
        self.assertEqual(flow['scenario_cash_movement'], 0)
        self.assertEqual(flow['receivable_from_payers'], 300)
        self.assertEqual(flow['retained_revenue'], 400)
        self.assertEqual(flow['chargeback_loss'], 0)           # fully allowance-covered
        self.assertEqual(flow['chargeback_loss_allowance'], 300)
        self.assertEqual(ledger.credits()['available_total'], 0)
        # releasing twice is impossible: the credit is no longer locked
        with self.assertRaises(FeeError):
            ledger.apply_directive(self.data['directives']['release_c3_to_employer'])

    def test_path_b_double_credit_and_refund_after_credit_rejected(self):
        ledger = self.ledger()
        ledger.apply_directive(self.data['directives']['credit_c1_to_c3'])
        with self.assertRaises(FeeError):
            ledger.apply_directive(self.data['directives']['credit_c1_to_c3'])
        with self.assertRaises(FeeError):
            ledger.apply_directive(self.data['directives']['refund_c1_to_employer'])

    # ------------------------------------------------------------- seal gates
    def test_seal_payer_scope_and_clearing_state_are_enforced(self):
        ledger = self.ledger()

        revoked = copy.deepcopy(self.data['directives']['refund_c1_to_employer'])
        revoked['id'] = 'd-revoked-seal'
        revoked['seal'] = 'seal-e-revoked'
        with self.assertRaises(FeeError):
            ledger.apply_directive(revoked)

        foreign = copy.deepcopy(self.data['directives']['refund_c1_to_employer'])
        foreign['id'] = 'd-foreign-seal'
        foreign['seal'] = 'seal-c2'  # C2's seal cannot dispose of Employer-E's payment
        with self.assertRaises(FeeError):
            ledger.apply_directive(foreign)

        # only the canceler's unconsumed share is processable; C2 cannot be refunded away
        ledger.mark_attended('C2', 'attendance-C2')
        c2_directive = {
            'id': 'd-steal-c2', 'type': REFUND, 'seal': 'seal-e',
            'payment_id': 'p1', 'candidate': 'C2', 'amount': 300,
        }
        with self.assertRaises(FeeError):
            ledger.apply_directive(c2_directive)
        self.assertEqual(ledger.fund_flow()['retained_revenue'], 400)

        # once p1 is charged back, even a valid seal must use the correction flow
        ledger.apply_directive(self.data['directives']['refund_c1_to_employer'])
        ledger.register_chargeback('p1', 'cb-p1')
        with self.assertRaises(FeeError):
            ledger.apply_directive(self.data['directives']['refund_c1_to_employer'])

    def test_c2_own_top_up_can_never_be_moved_by_employer(self):
        ledger = self.ledger()
        steal = {
            'id': 'd-employer-touches-p2', 'type': REFUND, 'seal': 'seal-e',
            'payment_id': 'p2', 'candidate': 'C2', 'amount': 100,
        }
        with self.assertRaises(FeeError):
            ledger.apply_directive(steal)
        ledger.mark_attended('C2', 'attendance-C2')
        balances = {row['payment_id']: row for row in ledger.payment_balances()}
        self.assertEqual(balances['p2']['consumed_revenue'], 100)
        self.assertEqual(balances['p2']['cash_refunded'], 0)

    # --------------------------------------------------- settlement append-only
    def test_settlement_statement_freezes_shares_and_corrections_reference_it(self):
        ledger = self.ledger()
        opening = len(ledger.entries)                            # two opening receipts
        stmt = ledger.issue_settlement('stmt-001', ['C1', 'C2'])
        self.assertEqual(stmt['type'], E_SETTLEMENT)
        self.assertEqual(len(ledger.entries), opening + 1)
        with self.assertRaises(FeeError):
            ledger.apply_directive(self.data['directives']['refund_c1_to_employer'])

        # a later cancellation must be opened as an explicit correction flow;
        # until then the frozen share keeps rejecting the directive
        with self.assertRaises(FeeError):
            ledger.apply_directive(self.data['directives']['refund_c1_to_employer'])

    def test_correcting_entries_append_and_cross_reference_without_overwrite(self):
        ledger = self.ledger()
        ledger.issue_settlement('stmt-001', ['C1', 'C2'])
        before = list(ledger.entries)
        # reopen C1's share through the explicit correction flow, which itself
        # posts a new entry referencing the statement, then post the refund
        ledger.open_settlement_correction(
            'stmt-001', 'corr-001',
            [s['share_id'] for s in ledger.capture.shares_for('C1')],
        )
        entry = ledger.apply_directive(self.data['directives']['refund_c1_to_employer'])
        self.assertTrue(entry['corrects'])
        self.assertIn('corr-001', entry['refs'])
        self.assertEqual(before, ledger.entries[:len(before)])  # old entry byte-identical
        self.assertGreater(len(ledger.entries), len(before))
        # every new entry references the share/payment or an earlier entry id
        for later in ledger.entries[len(before):]:
            self.assertTrue(later['refs'])

    # ------------------------------------------------------------ chargebacks
    def test_chargeback_on_path_a_is_new_cross_referenced_entries(self):
        ledger = self.ledger()
        ledger.mark_attended('C2', 'attendance-C2')
        ledger.apply_directive(self.data['directives']['refund_c1_to_employer'])
        original_entries = list(ledger.entries)
        correction = ledger.register_chargeback('p1', 'cb-p1')

        self.assertEqual(len(correction), 2)                    # one per p1 share
        self.assertTrue(all(e['type'] == E_CHARGEBACK for e in correction))
        for entry in correction:
            self.assertIn('cb-p1', entry['refs'])
            self.assertIn('p1', entry['refs'])
        # old entries are preserved, not overwritten
        self.assertEqual(original_entries, ledger.entries[:len(original_entries)])

        flow = ledger.fund_flow()
        self.assertEqual(flow['chargeback_total'], 600)
        self.assertEqual(flow['cash_refund_total'], 300)
        # scenario movement: -300 refund - 600 chargeback = -900
        self.assertEqual(flow['scenario_cash_movement'], -900)
        # cash position: 700 received - 300 refunded - 600 charged back = -200
        self.assertEqual(flow['cash_balance'], -200)
        # the 600 group payment is now fully a claim on Employer-E; C2's fee stays earned
        self.assertEqual(flow['receivable_from_payers'], 600)
        self.assertEqual(flow['retained_revenue'], 400)
        # C1's clawed-back 300 is a real loss; C2's 300 claim is allowance-covered
        self.assertEqual(flow['chargeback_loss'], 300)
        self.assertEqual(flow['chargeback_loss_allowance'], 300)
        self.assertEqual(flow['net_chargeback_loss'], 0)

        balances = {row['payment_id']: row for row in ledger.payment_balances()}
        self.assertEqual(balances['p1']['status'], 'charged_back')
        self.assertEqual(balances['p1']['undisposed_balance'], 0)
        self.assertEqual(balances['p2']['status'], 'cleared')
        # repeated chargeback is rejected; correction happens once as new entries
        with self.assertRaises(FeeError):
            ledger.register_chargeback('p1', 'cb-p1-duplicate')

    def test_chargeback_on_path_b_voids_locked_credit_and_claims_back_from_payer(self):
        ledger = self.ledger()
        ledger.mark_attended('C2', 'attendance-C2')
        ledger.apply_directive(self.data['directives']['credit_c1_to_c3'])
        correction = ledger.register_chargeback('p1', 'cb-p1')

        self.assertEqual(len(correction), 2)
        credit_entry = next(e for e in correction if e.get('voided_credit') == 'C3')
        self.assertTrue(credit_entry['reissue_on_recovery'])
        self.assertEqual(ledger.credits()['available_total'], 0)
        credit_record = next(c for c in ledger.credits()['all_credits'] if c['credit_id'] == 'C3')
        self.assertTrue(credit_record['voided_by_chargeback'])

        flow = ledger.fund_flow()
        self.assertEqual(flow['cash_refund_total'], 0)
        self.assertEqual(flow['chargeback_total'], 600)
        self.assertEqual(flow['scenario_cash_movement'], -600)
        self.assertEqual(flow['cash_balance'], 100)               # 700 - 600
        self.assertEqual(flow['receivable_from_payers'], 600)
        self.assertEqual(flow['retained_revenue'], 400)        # C2 still fully covered
        # both shares' claims are fully provisioned: gross loss zero, allowance 600
        self.assertEqual(flow['chargeback_loss'], 0)
        self.assertEqual(flow['chargeback_loss_allowance'], 600)
        self.assertEqual(flow['net_chargeback_loss'], -600)
        # the voided credit cannot be consumed or released anymore
        with self.assertRaises(FeeError):
            ledger.consume_credit('C3', 'attendance-C3')

    # ------------------------------------------------------ invariants / misc
    def test_every_ledger_entry_is_balanced(self):
        ledger = self.ledger()
        ledger.mark_attended('C2', 'attendance-C2')
        ledger.apply_directive(self.data['directives']['credit_c1_to_c3'])
        ledger.register_chargeback('p1', 'cb-p1')
        for entry in ledger.entries:
            if not entry['postings']:
                continue
            debit = sum(p['amount'] for p in entry['postings'] if p['side'] == 'debit')
            credit = sum(p['amount'] for p in entry['postings'] if p['side'] == 'credit')
            self.assertEqual(debit, credit, entry['id'])

    def test_only_canceler_unconsumed_share_is_processable(self):
        ledger = self.ledger()
        ledger.mark_attended('C2', 'attendance-C2')
        # C2 shares are consumed -> only C1's allocated share remains processable
        states = {s['share_id']: s['state'] for s in ledger.share_states()}
        self.assertEqual(states['p1:C1'], ALLOCATED)
        self.assertEqual(states['p1:C2'], CONSUMED)
        self.assertEqual(states['p2:C2'], CONSUMED)

    def test_existing_assessment_fixtures_still_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / 'wf.sqlite')
            for sub in ('candidate', 'station', 'assessment'):
                result = import_file(db, sub, ROOT / 'fixtures' / f'{sub}-ok.json')
                self.assertIn('inserted', result)
            decisions = report(db)['decisions']
            self.assertEqual(len(decisions), 10)
            db.close()


if __name__ == '__main__':
    unittest.main()
