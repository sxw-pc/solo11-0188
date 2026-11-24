import json, tempfile, unittest
from pathlib import Path
from skillstation.portal_capture import read_page, capture_chain, catalog_digest
from skillstation.roster_snapshot import build, load_tombstones, RosterLedger

ROOT = Path(__file__).resolve().parents[1] / 'fixtures'
BATCH = 'EXAM-2026-11-24'
R7_CATALOG = catalog_digest(['B1', 'B0'])
R8_CATALOG = catalog_digest(['B1', 'B2'])
B0_TOMBSTONE = {'batch': BATCH, 'epoch': 'R8', 'booking': 'B0',
                'revoked_at': '2026-11-24T10:05:00Z', 'reason': 'portal withdrawal between cuts'}


def page_html(batch=BATCH, epoch='R7', cut='C107', page=1, pages=1, entries=(), catalog=None, next_href=None):
    attrs = ('data-batch="%s" data-epoch="%s" data-cut="%s" data-page="%d" data-pages="%d" '
             'data-cursor="%s|%s|%d" data-catalog="%s"' % (batch, epoch, cut, page, pages, batch, cut, page, catalog))
    body = ''.join('<div data-booking="%s" data-seq="%d">%s</div>' % (b, i, b) for i, b in enumerate(entries, 1))
    if next_href:
        body += '<a data-next="%s" data-next-cursor="%s|%s|%d">Next</a>' % (next_href, batch, cut, page + 1)
    return '<html><body %s>%s</body></html>' % (attrs, body)


class PortalCaptureEvidence(unittest.TestCase):
    def test_page_carries_cursor_epoch_and_catalog(self):
        page = read_page(ROOT / 'booking-page-1.html')
        self.assertEqual(page.bookings, ['B1'])
        self.assertEqual((page.epoch, page.cut, page.page, page.pages), ('R7', 'C107', 1, 2))
        self.assertEqual(page.cursor, BATCH + '|C107|1')
        self.assertEqual(page.catalog, R7_CATALOG)
        self.assertEqual(page.next_href, 'booking-page-2.html')
        page2 = read_page(ROOT / 'booking-page-2.html')
        self.assertEqual((page2.epoch, page2.bookings), ('R8', ['B2']))

    def test_cursor_must_bind_batch_cut_and_page(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'loose.html'
            good = page_html(page=1, pages=1, entries=['B1'], catalog=catalog_digest(['B1']))
            p.write_text(good.replace('|C107|1', '|C107|9'), encoding='utf-8')
            with self.assertRaises(ValueError):
                read_page(p)

    def test_insert_reorder_between_fetches_trips_epoch_barrier(self):
        cap = capture_chain(ROOT / 'booking-page-1.html')
        self.assertEqual([p.page for p in cap.pages], [1])
        self.assertEqual(len(cap.strays), 1)
        self.assertEqual(cap.strays[0].epoch, 'R8')
        self.assertIn('epoch barrier', cap.barrier)
        self.assertEqual(cap.missing(), [[2, 2]])
        self.assertFalse(cap.closed())
        # the reshuffled page is never merged: no missed number, no duplicate
        self.assertEqual(cap.chain_bookings(), ['B1'])

    def test_mixed_epoch_pages_never_form_a_snapshot(self):
        snap = build(capture_chain(ROOT / 'booking-page-1.html'))
        self.assertFalse(snap['complete'])
        self.assertEqual(snap['bookings'], [])
        self.assertEqual(snap['observed'], ['B1'])
        self.assertEqual(snap['missing'], [[2, 2]])
        self.assertEqual(snap['epochs_seen'], ['R7', 'R8'])
        self.assertEqual(snap['strays'][0]['bookings'], ['B2'])
        self.assertNotEqual(snap['digest'], snap['catalog'])

    def test_gap_and_overlap_are_reported_not_merged(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cat = catalog_digest(['B1', 'B2'])
            (d / 'booking-page-1.html').write_text(
                page_html(epoch='R9', cut='C109', page=1, pages=2, entries=['B1'], catalog=cat,
                          next_href='booking-page-2.html'), encoding='utf-8')
            cap = capture_chain(d / 'booking-page-1.html')
            self.assertEqual(cap.missing(), [[2, 2]])
            self.assertFalse(build(cap)['complete'])
            (d / 'booking-page-2.html').write_text(
                page_html(epoch='R9', cut='C109', page=2, pages=2, entries=['B1'], catalog=cat), encoding='utf-8')
            snap = build(capture_chain(d / 'booking-page-1.html'))
            self.assertFalse(snap['complete'])
            self.assertIn('duplicate', snap['reason'])


class RouteStaging:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def stage(self, name, files):
        d = self.dir / name
        d.mkdir(exist_ok=True)
        for fname, content in files.items():
            (d / fname).write_text(content, encoding='utf-8')
        return d

    def route_a_dir(self):
        r7_page2 = page_html(epoch='R7', cut='C107', page=2, pages=2, entries=['B0'], catalog=R7_CATALOG)
        return self.stage('route-a', {
            'booking-page-1.html': (ROOT / 'booking-page-1.html').read_text(encoding='utf-8'),
            'booking-page-2.html': r7_page2})

    def route_b_dir(self):
        r8_page1 = page_html(epoch='R8', cut='C108', page=1, pages=2, entries=['B1'], catalog=R8_CATALOG,
                             next_href='booking-page-2.html')
        return self.stage('route-b', {
            'booking-page-1.html': r8_page1,
            'booking-page-2.html': (ROOT / 'booking-page-2.html').read_text(encoding='utf-8')})


class RecoveryRoutes(RouteStaging, unittest.TestCase):
    def test_route_a_rebuilds_r7_roster_from_catalog_and_page_cache(self):
        snap = build(capture_chain(self.route_a_dir() / 'booking-page-1.html'), tombstones=[B0_TOMBSTONE])
        self.assertTrue(snap['complete'], snap['reason'])
        self.assertEqual(snap['epoch'], 'R7')
        self.assertEqual(snap['bookings'], ['B0', 'B1'])
        self.assertEqual(snap['missing'], [])
        self.assertEqual(snap['digest'], R7_CATALOG)
        self.assertEqual(snap['revoked'], [])
        # the R8 tombstone stays behind the epoch barrier: B0 is not revoked from R7
        self.assertEqual([t['booking'] for t in snap['deferred_tombstones']], ['B0'])
        # B2 is left for the next epoch
        self.assertNotIn('B2', snap['bookings'])

    def test_route_b_issues_r8_roster_with_b0_tombstone(self):
        snap = build(capture_chain(self.route_b_dir() / 'booking-page-1.html'), tombstones=[B0_TOMBSTONE])
        self.assertTrue(snap['complete'], snap['reason'])
        self.assertEqual(snap['epoch'], 'R8')
        self.assertEqual(snap['bookings'], ['B1', 'B2'])
        self.assertEqual(snap['revoked'], ['B0'])
        self.assertEqual(snap['deferred_tombstones'], [])
        self.assertEqual(snap['digest'], R8_CATALOG)

    def test_tombstone_file_loads_as_epoch_evidence(self):
        tomb_file = self.dir / 'tombstones.json'
        tomb_file.write_text(json.dumps([B0_TOMBSTONE], ensure_ascii=False), encoding='utf-8')
        tombstones = load_tombstones(tomb_file)
        snap = build(capture_chain(self.route_b_dir() / 'booking-page-1.html'), tombstones=tombstones)
        self.assertEqual(snap['revoked'], ['B0'])


class RosterLedgerStory(RouteStaging, unittest.TestCase):
    def initial_ledger(self):
        ledger = RosterLedger()
        # the old wrong complete report and the original mixed capture stay on record
        ledger.record({'batch': BATCH, 'complete': True, 'bookings': ['B1'],
                       'source': 'legacy-first-page-report'})
        ledger.record(build(capture_chain(ROOT / 'booking-page-1.html')))
        return ledger

    def test_initial_state_has_no_issuable_roster(self):
        ledger = self.initial_ledger()
        self.assertIsNone(ledger.current(BATCH))
        self.assertEqual(len(ledger.reports(BATCH)), 2)

    def test_both_routes_preserve_mixed_capture_and_wrong_report(self):
        la = self.initial_ledger()
        sa = la.supplement(build(capture_chain(self.route_a_dir() / 'booking-page-1.html'),
                                 tombstones=[B0_TOMBSTONE]), tombstones=[B0_TOMBSTONE])
        self.assertTrue(sa['complete'])
        self.assertEqual(la.current(BATCH)['bookings'], ['B0', 'B1'])
        lb = self.initial_ledger()
        sb = lb.supplement(build(capture_chain(self.route_b_dir() / 'booking-page-1.html'),
                                 tombstones=[B0_TOMBSTONE]), tombstones=[B0_TOMBSTONE])
        self.assertTrue(sb['complete'])
        self.assertEqual(lb.current(BATCH)['bookings'], ['B1', 'B2'])
        self.assertEqual(lb.current(BATCH)['revoked'], ['B0'])
        for ledger in (la, lb):
            history = ledger.reports(BATCH)
            self.assertEqual(history[0]['bookings'], ['B1'])
            self.assertFalse(history[1]['complete'])
            self.assertEqual(history[1]['strays'][0]['epoch'], 'R8')
            self.assertEqual(history[1]['missing'], [[2, 2]])

    def test_sequential_epochs_replay_tombstones_in_order(self):
        ledger = self.initial_ledger()
        ledger.supplement(build(capture_chain(self.route_a_dir() / 'booking-page-1.html'),
                                tombstones=[B0_TOMBSTONE]), tombstones=[B0_TOMBSTONE])
        self.assertEqual(ledger.current(BATCH)['bookings'], ['B0', 'B1'])
        r8 = build(capture_chain(self.route_b_dir() / 'booking-page-1.html'), tombstones=[B0_TOMBSTONE])
        rejected = ledger.supplement(dict(r8, tombstones=[], revoked=[]), tombstones=[])
        self.assertFalse(rejected['complete'])
        self.assertIn('unexplained revocations: B0', rejected['reason'])
        self.assertEqual(ledger.current(BATCH)['epoch'], 'R7')
        accepted = ledger.supplement(r8, tombstones=[B0_TOMBSTONE])
        self.assertTrue(accepted['complete'])
        self.assertEqual(ledger.current(BATCH)['bookings'], ['B1', 'B2'])
        # a late-arriving R7 chain is preserved as history but cannot roll the roster back
        late = ledger.supplement(build(capture_chain(self.route_a_dir() / 'booking-page-1.html')))
        self.assertTrue(late['complete'])
        self.assertEqual(ledger.current(BATCH)['epoch'], 'R8')
        self.assertEqual(ledger.reports(BATCH)[-1]['epoch'], 'R7')

    def test_supplement_revises_only_its_own_batch(self):
        other = self.stage('other', {'booking-page-1.html': page_html(
            batch='EXAM-OTHER', epoch='R3', cut='C303', page=1, pages=1, entries=['B9'],
            catalog=catalog_digest(['B9']))})
        ledger = self.initial_ledger()
        ledger.record(build(capture_chain(other / 'booking-page-1.html')))
        self.assertEqual(ledger.current('EXAM-OTHER')['bookings'], ['B9'])
        ledger.supplement(build(capture_chain(self.route_b_dir() / 'booking-page-1.html'),
                                tombstones=[B0_TOMBSTONE]), tombstones=[B0_TOMBSTONE])
        self.assertEqual(ledger.current('EXAM-OTHER')['bookings'], ['B9'])
        self.assertEqual(ledger.current(BATCH)['bookings'], ['B1', 'B2'])
        self.assertEqual(len(ledger.reports('EXAM-OTHER')), 1)


if __name__ == '__main__':
    unittest.main()
