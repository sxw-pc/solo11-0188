"""Offline booking-portal page capture with cursor and epoch evidence.

Every portal page carries a cursor binding the exam batch and the server
cut point, an in-page sequence per booking, the catalog digest issued at
that cut, and the business epoch of the roster view.  A capture walks the
``next`` links and only accepts pages whose cursor continues the chain; a
page from another cut trips the epoch barrier and is kept as stray
evidence instead of being merged into a pseudo snapshot.
"""
import hashlib, json, re
from html.parser import HTMLParser
from pathlib import Path

def catalog_digest(bookings):
    """Recomputable digest over the ordered booking ids of one catalog."""
    body=json.dumps(list(bookings),ensure_ascii=False,separators=(',',':'))
    return 'sha256:'+hashlib.sha256(body.encode('utf-8')).hexdigest()

def make_cursor(batch,cut,page):return '%s|%s|%d'%(batch,cut,page)

def parse_cursor(cursor):
    parts=str(cursor).split('|')
    if len(parts)!=3 or not parts[0] or not parts[1] or not parts[2].isdigit():raise ValueError('malformed cursor: %r'%(cursor,))
    return parts[0],parts[1],int(parts[2])

class Page:
    """Parsed evidence of one portal page."""
    def __init__(self,source,batch,epoch,cut,page,pages,cursor,catalog,entries,next_href,next_cursor):
        self.source=source;self.batch=batch;self.epoch=epoch;self.cut=cut
        self.page=page;self.pages=pages;self.cursor=cursor;self.catalog=catalog
        self.entries=tuple(entries);self.next_href=next_href;self.next_cursor=next_cursor
    @property
    def bookings(self):return [booking for seq,booking in self.entries]
    def __repr__(self):return 'Page(%s %s page=%d/%d %r)'%(self.batch,self.epoch,self.page,self.pages,self.bookings)

class _PageHTML(HTMLParser):
    def __init__(self):
        super().__init__();self.body={};self.entries=[];self.next_href=None;self.next_cursor=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='body':self.body=a
        elif tag=='div' and 'data-booking' in a:self.entries.append((int(a.get('data-seq') or 0),a['data-booking']))
        elif tag=='a' and 'data-next' in a:self.next_href=a['data-next'];self.next_cursor=a.get('data-next-cursor')

def read_page(file):
    """Parse one portal page and validate that its cursor binds batch, cut and page."""
    path=Path(file);parser=_PageHTML();parser.feed(path.read_text(encoding='utf-8'))
    attrs=parser.body
    missing=[k for k in ('data-batch','data-epoch','data-cut','data-page','data-pages','data-cursor','data-catalog') if k not in attrs]
    if missing:raise ValueError('page lacks capture evidence %s: %s'%(','.join(missing),path))
    batch,epoch,cut=attrs['data-batch'],attrs['data-epoch'],attrs['data-cut']
    page,pages=int(attrs['data-page']),int(attrs['data-pages'])
    cursor,catalog=attrs['data-cursor'],attrs['data-catalog']
    if not 1<=page<=pages:raise ValueError('page index %d outside 1..%d: %s'%(page,pages,path))
    if parse_cursor(cursor)!=(batch,cut,page):raise ValueError('cursor does not bind batch/cut/page: %s'%path)
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',catalog):raise ValueError('malformed catalog digest: %s'%path)
    entries=sorted(parser.entries)
    if [seq for seq,_ in entries]!=list(range(1,len(entries)+1)):raise ValueError('in-page sequence gap or duplicate: %s'%path)
    next_href,next_cursor=parser.next_href,parser.next_cursor
    if next_href is not None:
        if next_cursor is None:raise ValueError('next link without cursor: %s'%path)
        if parse_cursor(next_cursor)!=(batch,cut,page+1):raise ValueError('next cursor does not continue the chain: %s'%path)
    return Page(str(path),batch,epoch,cut,page,pages,cursor,catalog,entries,next_href,next_cursor)

class Capture:
    """Page-chain evidence collected under one cursor; strays are preserved, never merged."""
    def __init__(self,first):
        self.batch=first.batch;self.cut=first.cut;self.epoch=first.epoch
        self.expected_pages=first.pages;self.catalog=first.catalog
        self.pages=[first];self.strays=[];self.barrier=None
    def chain_bookings(self):
        return [b for p in sorted(self.pages,key=lambda p:p.page) for b in p.bookings]
    def digest(self):
        return catalog_digest(self.chain_bookings()) if self.pages else None
    def missing(self):
        have={p.page for p in self.pages};out=[]
        for n in range(1,(self.expected_pages or 0)+1):
            if n in have:continue
            if out and n==out[-1][1]+1:out[-1][1]=n
            else:out.append([n,n])
        return out
    def closed(self):
        return self.barrier is None and not self.missing()

def capture_chain(first_file,resolver=None):
    """Walk the next links from the first page, stopping at the first cursor break."""
    resolve=resolver or (lambda page,href:Path(page.source).parent/href)
    cap=Capture(read_page(first_file));seen={cap.pages[0].cursor}
    while cap.barrier is None:
        current=cap.pages[-1]
        if current.next_href is None:break
        if current.page>=current.pages:
            cap.barrier='page %d of %d still links next: %s'%(current.page,current.pages,current.next_href);break
        try:nxt=read_page(resolve(current,current.next_href))
        except (OSError,ValueError) as exc:
            cap.barrier='page %d not captured: %s'%(current.page+1,exc);break
        if nxt.cursor!=current.next_cursor or nxt.batch!=cap.batch or nxt.cut!=cap.cut or nxt.epoch!=cap.epoch:
            cap.strays.append(nxt)
            cap.barrier='epoch barrier: expected cursor %s, got %s (epoch %s)'%(current.next_cursor,nxt.cursor,nxt.epoch);break
        if nxt.catalog!=cap.catalog or nxt.pages!=cap.expected_pages:
            cap.strays.append(nxt)
            cap.barrier='catalog mismatch on page %d'%nxt.page;break
        if nxt.cursor in seen:
            cap.strays.append(nxt);cap.barrier='cursor cycle at %s'%nxt.cursor;break
        cap.pages.append(nxt);seen.add(nxt.cursor)
    return cap
