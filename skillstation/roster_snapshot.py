"""Roster snapshots issued only when a page chain closes at a single cut.

Completeness and missing-page verdicts are driven entirely by the cursor
and page-chain evidence from :mod:`skillstation.portal_capture`.  A roster
is issued only when the full page chain, the declared page count and the
issued catalog digest close at one server cut; mixed-epoch pages never
combine into a pseudo snapshot.  Revocation tombstones replay per business
epoch and never flow back across the epoch barrier.  The result is a
recomputable set of booking numbers plus missing-page ranges; it does not
adjudicate candidate eligibility, stations or scores.
"""
import json, re
from pathlib import Path
from .portal_capture import Capture, capture_chain

def epoch_key(epoch):
    m=re.search(r'(\d+)$',str(epoch))
    return int(m.group(1)) if m else 0

def load_tombstones(file):
    payload=json.loads(Path(file).read_text(encoding='utf-8'))
    if not isinstance(payload,list):raise ValueError('tombstone list required')
    for t in payload:
        if not all(k in t for k in ('batch','epoch','booking')):raise ValueError('tombstone envelope mismatch')
    return payload

def build(source,tombstones=()):
    """Evaluate captured page-chain evidence into a roster snapshot verdict."""
    cap=source if isinstance(source,Capture) else capture_chain(source)
    chain=cap.chain_bookings();digest=cap.digest()
    snap={'batch':cap.batch,'complete':False,'epoch':None,'cut':cap.cut,
          'bookings':[],'observed':sorted(set(chain)),
          'missing':cap.missing(),'pages':[p.page for p in cap.pages],
          'expected_pages':cap.expected_pages,
          'catalog':cap.catalog,'digest':digest,
          'revoked':[],'tombstones':[],'deferred_tombstones':list(tombstones),
          'strays':[{'source':p.source,'epoch':p.epoch,'cut':p.cut,'cursor':p.cursor,'bookings':p.bookings} for p in cap.strays],
          'barrier':cap.barrier,
          'epochs_seen':sorted({p.epoch for p in cap.pages}|{p.epoch for p in cap.strays}),
          'sources':[p.source for p in cap.pages],'reason':''}
    reasons=[]
    if cap.barrier:reasons.append(cap.barrier)
    if snap['missing']:reasons.append('missing pages: %s'%snap['missing'])
    if len(chain)!=len(set(chain)):reasons.append('duplicate bookings in chain')
    if digest!=cap.catalog:reasons.append('catalog digest mismatch')
    if not reasons:
        epoch=cap.epoch;applied=[];deferred=[];conflicts=[]
        for t in sorted(tombstones,key=lambda t:(epoch_key(t['epoch']),t['booking'])):
            if t['batch']!=cap.batch or epoch_key(t['epoch'])>epoch_key(epoch):deferred.append(t)
            elif t['booking'] in chain:conflicts.append(t)
            else:applied.append(t)
        if conflicts:
            reasons.append('tombstone conflicts with catalog: %s'%','.join(sorted(t['booking'] for t in conflicts)))
            deferred.extend(conflicts)
        else:
            snap.update(complete=True,epoch=epoch,bookings=sorted(set(chain)),
                        revoked=sorted(t['booking'] for t in applied),tombstones=applied)
        snap['deferred_tombstones']=deferred
    snap['reason']='ok' if snap['complete'] else '; '.join(reasons)
    return snap

class RosterLedger:
    """Append-only roster reports per exam batch.

    Supplements revise only their own exam batch; every report — including
    the original mixed capture and earlier wrong reports — stays in the
    history.  The current roster of a batch is the newest complete
    snapshot at the highest business epoch.
    """
    def __init__(self):self._reports=[]
    def record(self,snapshot):
        self._reports.append(dict(snapshot));return snapshot
    def reports(self,batch=None):
        return [r for r in self._reports if batch is None or r.get('batch')==batch]
    def _eligible(self,batch):
        return [r for r in self.reports(batch)
                if r.get('complete') and not r.get('rejected')
                and r.get('digest') and r.get('digest')==r.get('catalog')]
    def current(self,batch):
        best=None
        for r in self._eligible(batch):
            if best is None or epoch_key(r.get('epoch'))>=epoch_key(best.get('epoch')):best=r
        return best
    def supplement(self,snapshot,tombstones=()):
        """Record a recovery snapshot, replaying revocations per business epoch."""
        snap=dict(snapshot);prev=self.current(snap.get('batch'));problems=[]
        if prev and snap.get('complete'):
            delta=epoch_key(snap['epoch'])-epoch_key(prev['epoch'])
            if delta>0:
                vanished=set(prev['bookings'])-set(snap['bookings'])
                explained={t['booking'] for t in tombstones
                           if epoch_key(prev['epoch'])<epoch_key(t['epoch'])<=epoch_key(snap['epoch'])}
                unexplained=sorted(vanished-explained)
                if unexplained:problems.append('unexplained revocations: %s'%','.join(unexplained))
            elif delta==0 and snap['bookings']!=prev['bookings']:
                problems.append('conflicting roster for epoch %s'%snap['epoch'])
        if problems:snap.update(complete=False,rejected=True,reason='; '.join(problems))
        self._reports.append(snap)
        return snap
