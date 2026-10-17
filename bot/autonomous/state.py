"""Authority-local causal history and bounded durable book archives.

Segment v1: canonical ASCII JSON compressed with zlib, one original event and a
full post-event checkpoint per segment. Checkpoint domain is ``book-checkpoint-v1``.
One-event segments deliberately bound crash exposure; batching is a future format.
Only manifests committed in SQLite authorize replay. Orphans are verified, never
adopted. No network, numeric feature computation or trading activation lives here.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from .contracts import (BookPayload, CandlePayload, CausalCutoffs, ContractViolation,
    IngestResult, Record, SourceEvent, canonical_bytes, content_hash, strict_json)


def causal_visible(event_at_us, received_at_us, known_at_us,
                   candle_close_us, knowledge_cutoff_us) -> bool:
    values=(event_at_us,received_at_us,known_at_us,candle_close_us,knowledge_cutoff_us)
    if any(type(x) is not int or x<0 for x in values):return False
    return (event_at_us<=candle_close_us and received_at_us<=knowledge_cutoff_us
            and received_at_us<=known_at_us<=knowledge_cutoff_us)


@dataclass(frozen=True)
class SourceClockPolicy(Record):
    """Explicit release policy: maximum publication age and source future skew."""
    policy_id: str
    source: str
    stream: str
    max_age_us: int
    max_future_skew_us: int


def _key(event):
    return event.source,event.stream,event.instrument


def _apply(event, previous):
    if not isinstance(event.payload,BookPayload) or event.sequence is None or event.instrument is None:
        raise ContractViolation('sequenced instrument book required')
    if event.payload.is_snapshot:return event.payload
    if previous is None or event.sequence!=previous[0]+1:
        raise ContractViolation('book sequence discontinuity')
    sides=[]
    for old,delta in zip((previous[1].bids,previous[1].asks),(event.payload.bids,event.payload.asks)):
        levels=dict(old)
        for price,qty in delta:
            if qty:levels[price]=qty
            else:levels.pop(price,None)
        sides.append(tuple(sorted(levels.items(),reverse=not sides)))
    return BookPayload(*sides)


def _checkpoint(book):
    return content_hash(('book-checkpoint-v1',book))


class SegmentArchive:
    """Digest-only relative keys, no symlinks, bounded decode, atomic publication.

    Root is an injected authority-owned directory, never a caller supplied event
    path. File operations use directory descriptors and O_NOFOLLOW so pre-existing
    symlinks and concurrent leaf swaps cannot escape confinement.
    """
    def __init__(self,root: Path,*,max_compressed_bytes=1_048_576,max_decoded_bytes=1_048_576):
        for n in (max_compressed_bytes,max_decoded_bytes):
            if type(n) is not int or not 0<n<=1_048_576:raise ContractViolation('invalid archive bound')
        self.root=Path(root)
        if self.root.is_symlink():raise ContractViolation('symlink archive root')
        self.root.mkdir(parents=True,exist_ok=True)
        # Do not silently resolve symlink ancestors of the configured root.
        if any(p.is_symlink() for p in (self.root,*self.root.parents)):
            raise ContractViolation('symlink archive ancestor')
        self.max_compressed_bytes=max_compressed_bytes
        self.max_decoded_bytes=max_decoded_bytes

    def _directory(self):
        return os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)

    @staticmethod
    def _name(key,digest):
        if type(digest) is not str or not re.fullmatch('[0-9a-f]{64}',digest) or key!=digest+'.zlib':
            raise ContractViolation('unsafe archive key')
        return key

    def read(self,key,digest):
        name=self._name(key,digest)
        directory=self._directory()
        try:
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            with os.fdopen(fd,'rb') as file:
                info=os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=self.max_compressed_bytes:
                    raise ContractViolation('archive compressed size/type')
                raw=file.read(self.max_compressed_bytes+1)
        finally:os.close(directory)
        if len(raw)>self.max_compressed_bytes or hashlib.sha256(raw).hexdigest()!=digest:
            raise ContractViolation('archive byte hash/size')
        decoder=zlib.decompressobj()
        try:
            decoded=decoder.decompress(raw,self.max_decoded_bytes+1)
        except zlib.error as exc:raise ContractViolation('archive compression') from exc
        if len(decoded)>self.max_decoded_bytes or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
            raise ContractViolation('archive decoded bound/truncation/trailing bytes')
        try:body=strict_json(decoded.decode('ascii'))
        except (UnicodeError,ValueError) as exc:raise ContractViolation('archive encoding') from exc
        if type(body) is not dict or set(body)!={'format','event','checkpoint','policy'} or body['format']!='causal-book-v1':
            raise ContractViolation('archive version/shape')
        if canonical_bytes(body)!=decoded:raise ContractViolation('archive noncanonical bytes')
        event=SourceEvent.from_dict(body['event'])
        checkpoint=BookPayload.from_dict(body['checkpoint'])
        policy=SourceClockPolicy.from_dict(body['policy'])
        if _key(event)[:2]!=(policy.source,policy.stream):raise ContractViolation('archive policy binding')
        if not isinstance(event.payload,BookPayload) or event.sequence is None or event.instrument is None:
            raise ContractViolation('archive book type')
        return event,checkpoint,policy,len(raw)

    # Separate synchronous seams allow deterministic disk failure injection.
    @staticmethod
    def write(file,raw):file.write(raw)
    @staticmethod
    def flush(file):file.flush()
    @staticmethod
    def fsync_file(file):os.fsync(file.fileno())
    @staticmethod
    def rename(directory,temporary,name):os.replace(temporary,name,src_dir_fd=directory,dst_dir_fd=directory)
    @staticmethod
    def fsync_directory(directory):os.fsync(directory)

    def publish(self,event,checkpoint,policy,now):
        body=canonical_bytes(dict(format='causal-book-v1',event=event,checkpoint=checkpoint,policy=policy))
        if len(body)>self.max_decoded_bytes:raise ContractViolation('archive decoded bound')
        raw=zlib.compress(body,level=9)
        if len(raw)>self.max_compressed_bytes:raise ContractViolation('archive compressed bound')
        digest=hashlib.sha256(raw).hexdigest();name=digest+'.zlib'
        directory=self._directory();temporary=None
        try:
            # Exclusive creation under the pinned directory descriptor.
            temporary='.'+os.urandom(16).hex()+'.tmp'
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
            with os.fdopen(fd,'wb') as file:
                self.write(file,raw);self.flush(file);self.fsync_file(file)
            try:
                os.stat(name,dir_fd=directory,follow_symlinks=False)
            except FileNotFoundError:pass
            else:
                # Never overwrite unverified existing bytes or a symlink.
                existing=self.read(name,digest)
                if existing[:3]!=(event,checkpoint,policy):raise ContractViolation('archive identity conflict')
            self.rename(directory,temporary,name);temporary=None
            self.fsync_directory(directory)
        finally:
            if temporary is not None:
                try:os.unlink(temporary,dir_fd=directory)
                except FileNotFoundError:pass
            os.close(directory)
        return dict(segment_hash=digest,source=event.source,instrument=event.instrument,
            first_sequence=event.sequence,last_sequence=event.sequence,event_from_us=event.event_at_us,
            event_to_us=event.event_at_us,received_from_us=event.received_at_us,
            received_to_us=event.received_at_us,checkpoint_hash=_checkpoint(checkpoint),
            byte_length=len(raw),storage_key=name,committed_at_us=now,recorded_at_us=now)

    def verify(self,row):
        event,checkpoint,policy,length=self.read(row['storage_key'],row['segment_hash'])
        expected=dict(source=event.source,instrument=event.instrument,first_sequence=event.sequence,
            last_sequence=event.sequence,event_from_us=event.event_at_us,event_to_us=event.event_at_us,
            received_from_us=event.received_at_us,received_to_us=event.received_at_us,
            checkpoint_hash=_checkpoint(checkpoint),byte_length=length)
        if any(row[k]!=v for k,v in expected.items()):raise ContractViolation('archive manifest mismatch')
        if event.known_at_us>row['committed_at_us'] or row['recorded_at_us']<row['committed_at_us']:
            raise ContractViolation('archive manifest clocks')
        return event,checkpoint,policy


def _from_row(row):
    data={k:row[k] for k in ('event_id','source','stream','logical_key','revision','instrument',
        'event_at_us','received_at_us','known_at_us','sequence','block_hash','supersedes_id','payload_hash')}
    data['payload']=strict_json(row['payload_json'])
    return SourceEvent.from_dict(data)


class StateEngine:
    def __init__(self,ledger,archive_root,policies: tuple[SourceClockPolicy,...]):
        if type(policies) is not tuple or not policies or any(type(p) is not SourceClockPolicy for p in policies):
            raise ContractViolation('explicit source policies required')
        policy_map={(p.source,p.stream):p for p in policies}
        if len(policy_map)!=len(policies):raise ContractViolation('duplicate source policy')
        self.ledger=ledger
        self.policies=policies
        self._policy_map=MappingProxyType(policy_map)
        ledger._source_required=True
        self.archive=SegmentArchive(archive_root)
        self._books={};self._bad_sources=set();self._orphan_floors={};self._archive_failed=True
        self.orphans=();self.recovery_errors=()
        ledger._bind_source_engine(self)
        self.recover()

    @property
    def entries_ready(self):
        return (self.ledger._source_engine is self and bool(self._books)
                and not self._archive_failed and not self._bad_sources and self.ledger.dispatch_enabled)

    def _health(self):
        self._require_owner()

    def _require_owner(self):
        if self.ledger._source_engine is not self:
            raise ContractViolation('stale source engine; recovery ownership replaced')

    def book(self,source,stream,instrument):
        self._require_owner()
        key=(source,stream,instrument)
        if self._archive_failed or key in self._bad_sources:return None
        value=self._books.get(key)
        return value[1] if value else None

    def recover(self):
        self._require_owner()
        if self.ledger.connection.in_transaction:raise ContractViolation('recovery requires committed database')
        self._books={};self._bad_sources=set();self._orphan_floors={};errors=[];orphans=[];committed=set();resets={}
        try:
            for row in self.ledger.connection.execute('SELECT * FROM event_segments ORDER BY rowid'):
                committed.add(row['storage_key'])
                event,checkpoint,policy=self.archive.verify(row)
                persisted=self.ledger.connection.execute('SELECT * FROM source_events WHERE event_id=?',(event.event_id,)).fetchone()
                if persisted is None or _from_row(persisted)!=event:raise ContractViolation('segment source mismatch')
                key=_key(event)
                book=_apply(event,self._books.get(key))
                if book!=checkpoint:raise ContractViolation('checkpoint replay mismatch')
                self._books[key]=(event.sequence,book,event.event_id)
                if event.payload.is_snapshot:resets[key]=(event.sequence,event.known_at_us)
            directory=self.archive._directory()
            try:
                for name in os.listdir(directory):
                    if name in committed:continue
                    if name.startswith('.') and name.endswith('.tmp'):
                        raise ContractViolation('unverified partial archive file')
                    if not re.fullmatch(r'[0-9a-f]{64}\.zlib',name):raise ContractViolation('unknown archive file')
                    orphan,_,_,_=self.archive.read(name,name[:-5]);orphans.append(name)
                    key=_key(orphan)
                    reset=resets.get(key,(-1,-1))
                    if reset[0]<=orphan.sequence or reset[1]<=orphan.known_at_us:
                        self._bad_sources.add(key)
                        floor=self._orphan_floors.get(key,(-1,-1))
                        self._orphan_floors[key]=(max(floor[0],orphan.sequence),max(floor[1],orphan.known_at_us))
            finally:os.close(directory)
            for row in self.ledger.connection.execute("SELECT e.*,i.recorded_at_us AS invalidated_at FROM source_events e JOIN source_invalidations i ON i.event_id=e.event_id WHERE i.reason!='source_revision_superseded'"):
                event=_from_row(row)
                if isinstance(event.payload,BookPayload) and resets.get(_key(event),(-1,-1))[1]<=row['invalidated_at']:
                    self._bad_sources.add(_key(event))
        except (OSError,ValueError) as exc:errors.append(str(exc))
        self.orphans=tuple(sorted(orphans));self.recovery_errors=tuple(errors)
        self._archive_failed=bool(errors)
        self._health()

    def _reject(self,event,reason,*,book_failure=False,archive_failure=False):
        if book_failure or archive_failure:
            key=_key(event)
            self._bad_sources.add(key)
            previous=self._books.get(key)
            if previous and self.ledger.dispatch_enabled:
                now=self.ledger._clock_us()
                finding='source-health-'+content_hash((key,previous[2],reason,now))
                try:self.invalidate(finding,previous[2],now,reason)
                except (OSError,ValueError,RuntimeError):self._archive_failed=True
        if archive_failure:self._archive_failed=True
        self._health()
        return IngestResult(False,event.event_id,(reason,))

    def ingest(self,event: SourceEvent) -> IngestResult:
        self._require_owner()
        if type(event) is not SourceEvent:raise ContractViolation('typed source event required')
        c=self.ledger.connection
        if c.in_transaction:raise ContractViolation('ingest requires its own durable transaction')
        existing=c.execute('SELECT * FROM source_events WHERE event_id=? OR (source=? AND stream=? AND logical_key=? AND revision=?)',
            (event.event_id,event.source,event.stream,event.logical_key,event.revision)).fetchall()
        if existing:
            if len(existing)==1 and _from_row(existing[0])==event:
                return IngestResult(True,event.event_id,(),content_hash(event))
            return self._reject(event,'source_identity_conflict',book_failure=isinstance(event.payload,BookPayload))
        policy=self._policy_map.get((event.source,event.stream));now=self.ledger._clock_us()
        if policy is None:return self._reject(event,'source_policy_missing',book_failure=True)
        if (event.known_at_us>now or event.received_at_us-event.event_at_us>policy.max_age_us
            or event.event_at_us-event.received_at_us>policy.max_future_skew_us):
            return self._reject(event,'source_clock_unavailable',book_failure=True)
        if event.supersedes_id:
            prior=c.execute('SELECT * FROM source_events WHERE event_id=?',(event.supersedes_id,)).fetchone()
            if prior is None or (prior['source'],prior['stream'],prior['logical_key'],prior['instrument'])!=(event.source,event.stream,event.logical_key,event.instrument) or prior['revision']>=event.revision:
                return self._reject(event,'invalid_supersession')
        manifest=None;book=None;key=_key(event)
        if isinstance(event.payload,BookPayload):
            try:
                if key in self._bad_sources and not event.payload.is_snapshot:raise ContractViolation('snapshot required')
                old=self._books.get(key)
                if event.payload.is_snapshot:
                    floor=self._orphan_floors.get(key,(-1,-1))
                    if event.sequence is not None and (event.sequence<=floor[0] or event.known_at_us<=floor[1]):
                        raise ContractViolation('snapshot must supersede uncommitted orphan')
                    finding=c.execute("SELECT max(i.recorded_at_us) FROM source_invalidations i JOIN source_events e ON e.event_id=i.event_id WHERE e.source=? AND e.stream=? AND e.instrument=? AND i.reason!='source_revision_superseded'",key).fetchone()[0]
                    if finding is not None and event.known_at_us<=finding:
                        raise ContractViolation('snapshot must be known after source finding')
                if old and event.sequence is not None and event.sequence<=old[0]:raise ContractViolation('duplicate/out of order sequence')
                book=_apply(event,old)
            except ContractViolation:return self._reject(event,'invalid_book_sequence',book_failure=True)
            try:manifest=self.archive.publish(event,book,policy,now)
            except (OSError,ValueError):return self._reject(event,'archive_write_failed',archive_failure=True)
        try:
            with self.ledger.transaction() as tx:
                tx.execute('INSERT INTO source_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (event.event_id,event.source,event.stream,event.logical_key,event.revision,event.instrument,
                    event.event_at_us,event.received_at_us,event.known_at_us,event.sequence,event.block_hash,
                    event.supersedes_id,canonical_bytes(event.payload).decode(),event.payload_hash,now))
                if manifest:
                    tx.execute('INSERT INTO event_segments('+','.join(manifest)+') VALUES ('+','.join('?' for _ in manifest)+')',tuple(manifest.values()))
                revisions=tx.execute('SELECT event_id,known_at_us,revision FROM source_events WHERE source=? AND stream=? AND logical_key=?',
                    (event.source,event.stream,event.logical_key)).fetchall()
                latest=max(revisions,key=lambda r:(r['known_at_us'],r['revision'],r['event_id']))
                for previous in revisions:
                    if previous['event_id']==latest['event_id']:continue
                    finding='source-superseded-'+content_hash((previous['event_id'],latest['event_id']))
                    tx.execute("INSERT OR IGNORE INTO source_invalidations VALUES (?,?,?,'source_revision_superseded',?)",
                        (finding,previous['event_id'],latest['known_at_us'],now))
        except (OSError,ValueError,RuntimeError):return self._reject(event,'source_commit_failed',archive_failure=True)
        if book:
            self._books[key]=(event.sequence,book,event.event_id);self._bad_sources.discard(key)
            self._orphan_floors.pop(key,None)
        self._health()
        return IngestResult(True,event.event_id,(),content_hash(event))

    def select(self,cutoffs: CausalCutoffs) -> tuple[SourceEvent,...]:
        self._require_owner()
        if type(cutoffs) is not CausalCutoffs:raise ContractViolation('typed frozen cutoffs required')
        if self.ledger.connection.in_transaction:raise ContractViolation('selection requires committed database')
        selected={}
        for row in self.ledger.connection.execute("SELECT * FROM source_events e WHERE NOT EXISTS (SELECT 1 FROM source_invalidations i WHERE i.event_id=e.event_id AND i.reason!='source_revision_superseded' AND i.known_at_us<=? AND i.recorded_at_us<=?)",(cutoffs.knowledge_cutoff_us,cutoffs.knowledge_cutoff_us)):
            event=_from_row(row)
            if not causal_visible(event.event_at_us,event.received_at_us,event.known_at_us,cutoffs.candle_close_us,cutoffs.knowledge_cutoff_us):continue
            key=(event.source,event.stream,event.logical_key)
            rank=(event.known_at_us,event.revision,event.event_id)
            if key not in selected or rank>selected[key][0]:selected[key]=(rank,event)
        return tuple(selected[k][1] for k in sorted(selected))

    def invalidate(self,invalidation_id,event_id,known_at_us,reason):
        self._require_owner()
        now=self.ledger._clock_us()
        if reason=='source_revision_superseded':raise ContractViolation('reserved internal invalidation reason')
        if type(known_at_us) is not int or not 0<=known_at_us<=now or type(reason) is not str or not reason or len(reason)>256 or type(invalidation_id) is not str or not invalidation_id or len(invalidation_id)>256:
            raise ContractViolation('invalid invalidation')
        row=self.ledger.connection.execute('SELECT * FROM source_events WHERE event_id=?',(event_id,)).fetchone()
        if row is None or known_at_us<row['known_at_us']:raise ContractViolation('invalid invalidation target/time')
        values=(invalidation_id,event_id,known_at_us,reason)
        existing=self.ledger.connection.execute('SELECT * FROM source_invalidations WHERE invalidation_id=?',(invalidation_id,)).fetchone()
        if existing:
            if tuple(existing)[:4]!=values:raise ContractViolation('invalidation identity conflict')
            return
        with self.ledger.transaction() as tx:
            tx.execute('INSERT INTO source_invalidations VALUES (?,?,?,?,?)',values+(now,))
        if isinstance(_from_row(row).payload,BookPayload):self._bad_sources.add(_key(_from_row(row)))
        self._health()

    def invalidated_snapshots(self) -> tuple[str,...]:
        """Current derived eligibility, leaving frozen evidence untouched."""
        self._require_owner()
        return tuple(row[0] for row in self.ledger.connection.execute(
            'SELECT snapshot_id FROM snapshots ORDER BY snapshot_id')
            if self.ledger._source_lineage_invalid(row[0]))

    def import_candle(self,event: SourceEvent) -> IngestResult:
        if type(event) is not SourceEvent or not isinstance(event.payload,CandlePayload):raise ContractViolation('typed historical candle required')
        now=self.ledger._clock_us()
        return self.ingest(replace(event,received_at_us=now,known_at_us=now))
