"""Authority-local causal history and bounded durable book archives.

Segment v1: canonical ASCII JSON compressed with zlib, one original event and a
full post-event checkpoint per segment. Checkpoint domain is ``book-checkpoint-v1``.
One-event segments deliberately bound crash exposure; batching is a future format.
Only manifests committed in SQLite authorize replay. Orphans are verified, never
adopted. No network, numeric feature computation or trading activation lives here.
"""
from __future__ import annotations

import bisect
import hashlib
import math
import os
import re
import statistics
import stat
import zlib
from collections import OrderedDict
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from types import MappingProxyType

from .contracts import (BookPayload, CandlePayload, CausalCutoffs, ContractViolation,
    AccountRevision, ApprovedBundle, EncodedState, Feature, FieldDictionary,
    HistoricalInventory, IngestResult, NumericSnapshot, ProviderTokenCounter,
    Record, SharedContext, SourceEvent, StateHold, StateHoldDraft, StateUnavailable,
    TokenAccountingUnavailable, TokenCount, canonical_bytes, content_hash,
    default_field_dictionary, state_hold_audit_hash, strict_json, candidate_wire_id)


def require_token_budget(count: int) -> None:
    """Enforce the complete state-only provider token limit."""
    if type(count) is not int or not 0 <= count < 400:
        raise ValueError('state token budget exceeded')


def book_metrics(bids, asks, band_bps=10.0):
    """Compute midpoint, quoted spread and depth imbalance in a symmetric band."""
    if (not bids or not asks or type(band_bps) not in (int,float)
            or not math.isfinite(band_bps) or band_bps <= 0):
        raise ValueError('invalid book')
    if any(type(level) not in (tuple,list) or len(level)!=2
           or any(type(x) not in (int,float) or not math.isfinite(x) or x<=0 for x in level)
           for levels in (bids,asks) for level in levels):
        raise ValueError('invalid level')
    bid, ask = max(p for p, _ in bids), min(p for p, _ in asks)
    if bid >= ask:
        raise ValueError('crossed book')
    mid = (bid + ask) / 2
    lower, upper = mid * (1 - band_bps / 10000), mid * (1 + band_bps / 10000)
    try:
        bid_depth = math.fsum(q for p, q in bids if lower <= p <= mid)
        ask_depth = math.fsum(q for p, q in asks if mid <= p <= upper)
    except (OverflowError, ValueError) as exc:
        raise ValueError('invalid depth') from exc
    if not all(math.isfinite(x) for x in (mid, bid_depth, ask_depth, bid_depth + ask_depth)) or bid_depth + ask_depth <= 0:
        raise ValueError('invalid depth')
    result = mid, 10000 * (ask - bid) / mid, (bid_depth - ask_depth) / (bid_depth + ask_depth)
    if not all(math.isfinite(x) for x in result):
        raise ValueError('nonfinite book feature')
    return result


def _wire_values(features, definitions, identity=None):
    if tuple(feature.name for feature in features) != tuple(definition.name for definition in definitions):
        raise ContractViolation('feature slots do not match the field dictionary')
    by_name = {feature.name: feature for feature in features}
    values = []
    mask = 0
    for index, definition in enumerate(definitions):
        feature = by_name.get(definition.name)
        if feature is not None and feature.valid:
            mask |= 1 << definition.validity_bit
        if definition.name in ('candidate_id', 'dictionary_version', 'shared_validity_mask', 'candidate_validity_mask'):
            if definition.name == 'candidate_id':
                value = candidate_wire_id(identity)
            elif definition.name == 'dictionary_version':
                value = 1
            else:
                value = 0
        elif feature is None or not feature.valid:
            value = 0 if definition.precision == 0 else 0.0
        else:
            number = feature.value
            if type(number) not in (int, float) or not math.isfinite(number):
                raise ContractViolation(f'invalid numeric feature: {definition.name}')
            with localcontext() as context:
                context.prec = 50
                scaled = Decimal(str(number)) * definition.scale
                quantum = Decimal(1).scaleb(-definition.precision)
                quantized = scaled.quantize(quantum, rounding=ROUND_HALF_EVEN)
                value = int(quantized) if definition.precision == 0 else float(quantized)
            if not math.isfinite(value):
                raise ContractViolation(f'nonfinite encoded feature: {definition.name}')
        values.append(value)
    values[-1] = mask
    return values


def encode_state(snapshots: tuple[NumericSnapshot, ...], context: SharedContext,
                 dictionary: FieldDictionary,
                 token_counter: ProviderTokenCounter, *, candidate_order: tuple[str, ...],
                 ranked_bundle: ApprovedBundle) -> EncodedState | StateHoldDraft:
    """Encode a complete numeric state and prove its state-only token count."""
    if (type(snapshots) is not tuple or type(context) is not SharedContext
            or type(dictionary) is not FieldDictionary or type(candidate_order) is not tuple
            or type(ranked_bundle) is not ApprovedBundle):
        raise ContractViolation('typed state inputs required')
    if dictionary.dictionary_hash!=default_field_dictionary().dictionary_hash:
        raise ContractViolation('state encoding requires the registered numeric field dictionary')
    if not snapshots:
        raise StateUnavailable('audited HOLD requires frozen candidate snapshots')
    if any(type(snapshot) is not NumericSnapshot for snapshot in snapshots):
        raise ContractViolation('typed numeric snapshots required')
    bundle=ranked_bundle.bundle
    approved_order=tuple(candidate.candidate_id for candidate in bundle.candidates)
    snapshot_ids=tuple(snapshot.candidate_id for snapshot in snapshots)
    if (candidate_order!=approved_order or len(set(candidate_order))!=len(candidate_order)
            or set(candidate_order)!=set(snapshot_ids) or len(set(snapshot_ids))!=len(snapshot_ids)):
        raise ContractViolation('explicit rank order must match approved bundle and snapshot universe')
    anchor=snapshots[0]
    identity=(anchor.cutoffs.candle_close_us,anchor.cutoffs.knowledge_cutoff_us,
        anchor.cutoffs.boot_id,anchor.account_id,anchor.release_hash,anchor.account_revision)
    if any((snapshot.cutoffs.candle_close_us,snapshot.cutoffs.knowledge_cutoff_us,
            snapshot.cutoffs.boot_id,snapshot.account_id,snapshot.release_hash,snapshot.account_revision)!=identity
            for snapshot in snapshots):
        raise ContractViolation('mixed frozen candidate batch identity')
    if (anchor.release_hash!=ranked_bundle.bundle_hash
            or bundle.expires_at_us<=anchor.cutoffs.knowledge_cutoff_us
            or type(bundle.policy_hash) is not str or len(bundle.policy_hash)!=64):
        raise StateUnavailable('approved ranking bundle does not match the frozen release/window')
    by_id={snapshot.candidate_id:snapshot for snapshot in snapshots}
    definitions={candidate.candidate_id:candidate for candidate in bundle.candidates}
    eligible=[]
    reasons={}
    unresolved=False
    for candidate_id in candidate_order:
        snapshot=by_id[candidate_id]
        measured=_feature_value(snapshot.features,'eligible')
        defined=definitions[candidate_id].eligible
        if measured not in (0,1):
            reasons[candidate_id]='eligibility_unavailable'
            unresolved=True
        elif bool(measured)!=defined:
            raise StateUnavailable('snapshot eligibility differs from approved bundle')
        elif defined:
            eligible.append(snapshot)
        else:
            reasons[candidate_id]='not_eligible'
    if unresolved:
        return _state_hold_draft(anchor,ranked_bundle,candidate_order,
            tuple(by_id[candidate_id] for candidate_id in candidate_order),reasons,
            'eligibility_unavailable')
    if not eligible:
        return _state_hold_draft(anchor,ranked_bundle,candidate_order,
            tuple(by_id[candidate_id] for candidate_id in candidate_order),reasons,
            'no_eligible_candidates')
    if len(eligible)>5:
        raise ContractViolation('approved state universe exceeds five eligible candidates')

    shared_features=list(context.features)
    close_index=next((i for i,field in enumerate(dictionary.shared_fields) if field.name=='candle_close_ms'),None)
    if close_index is None:
        raise ContractViolation('field dictionary lacks candle_close_ms')
    shared_features[close_index]=Feature('candle_close_ms',float(anchor.cutoffs.candle_close_us//1000),True)
    bound_context=SharedContext(tuple(shared_features),sum(1<<i for i,f in enumerate(shared_features) if f.valid))
    shared = _wire_values(bound_context.features, dictionary.shared_fields)
    candidates = eligible[:]
    rows = [_wire_values(s.features, dictionary.candidate_fields, s.candidate_id) for s in candidates]
    while candidates:
        wire_state = [shared, rows]
        raw = canonical_bytes(wire_state).decode('ascii')
        try:
            proof = token_counter.count_state(raw)
        except TokenAccountingUnavailable:
            raise
        except Exception as exc:
            raise TokenAccountingUnavailable('provider state token proof unavailable') from exc
        if type(proof) is not TokenCount:
            raise TokenAccountingUnavailable('provider returned no typed token proof')
        if type(proof.state_tokens) is not int or proof.state_tokens < 0:
            raise TokenAccountingUnavailable('provider returned invalid token count')
        if (type(proof.provider_id) is not str or not proof.provider_id
                or type(proof.tokenizer_id) is not str or not proof.tokenizer_id
                or type(proof.compatibility_hash) is not str
                or not re.fullmatch('[0-9a-f]{64}',proof.compatibility_hash)):
            raise TokenAccountingUnavailable('provider token proof identity is incomplete')
        if proof.state_tokens >= 400:
            excluded=candidates.pop()
            reasons[excluded.candidate_id]='token_budget'
            rows.pop()
            continue
        require_token_budget(proof.state_tokens)
        included=set(s.candidate_id for s in candidates)
        excluded_ids=tuple(candidate_id for candidate_id in candidate_order if candidate_id not in included)
        excluded_reasons=tuple((candidate_id,reasons[candidate_id]) for candidate_id in excluded_ids)
        excluded_snapshots=tuple(by_id[candidate_id] for candidate_id in excluded_ids)
        return EncodedState(raw, content_hash(wire_state), dictionary.dictionary_hash,
            tuple(s.candidate_id for s in candidates), proof, tuple(candidates),candidate_order,
            excluded_ids,excluded_reasons,excluded_snapshots,ranked_bundle.bundle_hash,bundle.policy_hash,
            ranked_bundle)
    reasons.update({snapshot.candidate_id:'mandatory_state_over_budget' for snapshot in eligible})
    return _state_hold_draft(anchor,ranked_bundle,candidate_order,
        tuple(by_id[candidate_id] for candidate_id in candidate_order),reasons,
        'mandatory_state_over_budget')


def _state_hold_draft(anchor,ranked_bundle,candidate_order,snapshots,reasons,reason):
    excluded_reasons=tuple((candidate_id,reasons.get(candidate_id,'not_eligible'))
        for candidate_id in candidate_order)
    audit_hash=state_hold_audit_hash(anchor.cutoffs,anchor.account_id,anchor.release_hash,
        candidate_order,excluded_reasons,snapshots,ranked_bundle.bundle_hash,
        ranked_bundle.bundle.policy_hash)
    return StateHoldDraft(anchor.cutoffs,anchor.account_id,anchor.release_hash,candidate_order,
        candidate_order,excluded_reasons,reason,snapshots,ranked_bundle.bundle_hash,
        ranked_bundle.bundle.policy_hash,audit_hash,ranked_bundle)


def _feature_value(features, name):
    return next((feature.value for feature in features if feature.name == name and feature.valid), None)


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


class _BoundedCache(OrderedDict):
    """Bounded LRU evidence cache (get/set refresh recency); eviction only causes fail-closed refusals."""
    def __init__(self,capacity):
        super().__init__()
        self.capacity=capacity

    def __setitem__(self,key,value):
        super().__setitem__(key,value)
        self.move_to_end(key)
        while len(self)>self.capacity:self.popitem(last=False)

    def get(self,key,default=None):
        if key not in self:return default
        self.move_to_end(key)
        return super().__getitem__(key)


class StateEngine:
    # A boundary sample older than this is missing, never forward-filled.
    BOUNDARY_MAX_AGE_US=5_000_000
    # Snapshot replay starts this long before the first boundary; book streams must
    # publish a full-book checkpoint at least this often or features fail closed.
    BOOK_REPLAY_WINDOW_US=300_000_000
    # Per-engine evidence entries; one candle batch uses far fewer.
    CACHE_CAPACITY=1024
    # Decoded append-only source rows: one 65-minute window at 1s checkpoint cadence is 3900
    # per instrument, so this covers six candidates with headroom.
    DECODE_CACHE_CAPACITY=32768

    def __init__(self,ledger,archive_root,policies: tuple[SourceClockPolicy,...],account_id=None):
        if type(policies) is not tuple or not policies or any(type(p) is not SourceClockPolicy for p in policies):
            raise ContractViolation('explicit source policies required')
        policy_map={(p.source,p.stream):p for p in policies}
        if len(policy_map)!=len(policies):raise ContractViolation('duplicate source policy')
        self.ledger=ledger
        self.account_id=account_id
        self.policies=policies
        self._policy_map=MappingProxyType(policy_map)
        ledger._source_required=True
        self.archive=SegmentArchive(archive_root)
        self._books={};self._bad_sources=set();self._orphan_floors={};self._archive_failed=True
        self.orphans=();self.recovery_errors=()
        self._snapshot_inputs=_BoundedCache(self.CACHE_CAPACITY)
        self._snapshot_evidence=_BoundedCache(self.CACHE_CAPACITY)
        self._snapshot_inventory=_BoundedCache(self.CACHE_CAPACITY)
        self._encoded_evidence=_BoundedCache(self.CACHE_CAPACITY)
        self._hold_evidence=_BoundedCache(self.CACHE_CAPACITY)
        self._persisted_encoded_results=_BoundedCache(self.CACHE_CAPACITY)
        self._persisted_holds=_BoundedCache(self.CACHE_CAPACITY)
        self._decoded=_BoundedCache(self.DECODE_CACHE_CAPACITY)
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

    def snapshot(self, symbol: str, cutoffs: CausalCutoffs) -> NumericSnapshot:
        """Build candidate features only from events visible at the frozen cutoffs.

        Historical inventory and book mids are reconstructed at the frozen cutoffs.
        Current AccountView is reserved for execution-time controls.
        """
        self._require_owner()
        if type(symbol) is not str or not symbol.endswith('-USD') or type(cutoffs) is not CausalCutoffs:
            raise ContractViolation('USD instrument and typed frozen cutoffs required')
        candidate_id=symbol[:-4].lower()
        replay_from=cutoffs.candle_close_us-60*60_000_000-self.BOOK_REPLAY_WINDOW_US
        events=tuple(event for event in self.select(cutoffs,instrument=symbol,event_since_us=replay_from)
            if event.instrument==symbol and event.event_at_us>=replay_from and causal_visible(event.event_at_us,event.received_at_us,
                event.known_at_us,cutoffs.candle_close_us,cutoffs.knowledge_cutoff_us))
        book_events=sorted((event for event in events if isinstance(event.payload,BookPayload)
            and event.sequence is not None),key=lambda event:(event.source,event.stream,event.sequence,event.event_id))
        grouped={};book_refs=[]
        for event in book_events:grouped.setdefault((event.source,event.stream,event.instrument),[]).append(event)
        expected=tuple(cutoffs.candle_close_us-60_000_000*i for i in reversed(range(61)))
        per_minute={timestamp:[] for timestamp in expected}
        # One book stream per instrument: never stitch venues into one mid/RV series.
        ambiguous=len(grouped)>1
        for key,sequence_events in sorted(grouped.items()):
            # Replay the stream once, keeping (running max exchange time, book or None after a break).
            book_state=None;timeline=[];last_at=None
            for event in sequence_events:
                book_refs.append(event.event_id)
                if last_at is not None and event.event_at_us<last_at:book_state=None
                elif event.payload.is_snapshot:
                    book_state=(event.sequence,event.payload)
                elif book_state is not None and event.sequence==book_state[0]+1:
                    try:book_state=(event.sequence,_apply(event,book_state))
                    except ContractViolation:book_state=None
                else:book_state=None
                last_at=event.event_at_us if last_at is None else max(last_at,event.event_at_us)
                timeline.append((last_at,book_state))
            # As-of sampling: the latest state at or before each boundary, within the age bound.
            # Exchange timestamps are never moved onto the boundary.
            times=[entry[0] for entry in timeline]
            for boundary in expected:
                index=bisect.bisect_right(times,boundary)-1
                if index<0:continue
                event_at,sample=timeline[index]
                if ambiguous or sample is None or boundary-event_at>self.BOUNDARY_MAX_AGE_US:continue
                per_minute[boundary].append(sample[1])
        metrics=None
        try:
            close_books=per_minute[cutoffs.candle_close_us]
            if len(close_books)!=1:raise ValueError('missing or ambiguous close book')
            final_book=close_books[0]
            metrics=book_metrics(tuple((float(p),float(q)) for p,q in final_book.bids),
                tuple((float(p),float(q)) for p,q in final_book.asks))
        except (ValueError,TypeError,OverflowError,IndexError):
            metrics=None
        mids=[]
        try:
            for timestamp in expected:
                books=per_minute[timestamp]
                if len(books)!=1:raise ValueError('missing or ambiguous minute book')
                book=books[0]
                metrics_at=book_metrics(tuple((float(p),float(q)) for p,q in book.bids),
                    tuple((float(p),float(q)) for p,q in book.asks))
                mids.append(metrics_at[0])
            if any(not math.isfinite(mid) or mid<=0 for mid in mids):raise ValueError('invalid midpoint')
        except (ValueError,TypeError,OverflowError,IndexError):
            mids=[]
        try:
            returns=[math.log(current/previous) for previous,current in zip(mids,mids[1:])] if len(mids)==61 else []
            one_hour_return=(mids[-1]/mids[0]-1) if len(mids)==61 else None
            if any(not math.isfinite(value) for value in returns) or (one_hour_return is not None and not math.isfinite(one_hour_return)):
                raise ValueError('nonfinite volatility input')
            rv=statistics.stdev(returns) if len(returns)==60 else None
        except (ValueError,OverflowError,ZeroDivisionError):
            returns=[];rv=None;one_hour_return=None

        account_id=self.account_id
        if account_id is None:
            accounts=tuple(row[0] for row in self.ledger.connection.execute('SELECT account_id FROM accounts ORDER BY account_id'))
            if len(accounts)!=1:
                raise StateUnavailable('snapshot requires one explicit current execution account')
            account_id=accounts[0]
        inventory=self.ledger.inventory_as_of(account_id,cutoffs)
        if inventory.account_id!=account_id or inventory.cutoffs!=cutoffs:
            raise StateUnavailable('historical inventory identity differs from frozen account/cutoffs')
        base_asset=symbol[:-4]
        historical=dict(inventory.quantities)
        try:base_quantity=historical.get(base_asset,Decimal(0));base_inventory=float(base_quantity)
        except (TypeError,ValueError,OverflowError):base_inventory=float('nan')
        inventory_valid=math.isfinite(base_inventory)
        feature_values={
            'mid':(metrics[0],True) if metrics else (0.0,False),
            'spread_bps':(metrics[1],True) if metrics else (0.0,False),
            'imbalance':(metrics[2],True) if metrics else (0.0,False),
            'rv_60m':(rv,rv is not None), 'rv_samples':(60 if rv is not None else 0,rv is not None),
            'return_1h':(one_hour_return,one_hour_return is not None),
            'base_inventory':(base_inventory if inventory_valid else 0.0,inventory_valid),
            'inventory_usd':(base_inventory*metrics[0] if metrics and inventory_valid else 0.0,inventory_valid and metrics is not None and math.isfinite(base_inventory*metrics[0])),
            'eligible':(0.0,False), 'evidence_age_minutes':(0.0,False),
            'evidence_expiry_minutes':(0.0,False), 'thesis_condition_bits':(0.0,False),
            'position_headroom':(0.0,False), 'estimated_roundtrip_cost_bps':(0.0,False),
        }
        names=('candidate_id','mid','spread_bps','imbalance','rv_60m','rv_samples','return_1h',
            'base_inventory','inventory_usd','eligible','evidence_age_minutes','evidence_expiry_minutes',
            'thesis_condition_bits','position_headroom','estimated_roundtrip_cost_bps','candidate_validity_mask')
        features=tuple(Feature(name, float(feature_values.get(name,(0.0,False))[0] or 0.0),
            feature_values.get(name,(0.0,False))[1]) for name in names)
        mask=sum((1<<index) for index,feature in enumerate(features) if feature.valid)
        input_ids=tuple(sorted(set(book_refs)))
        lineage=content_hash((input_ids,inventory.lineage_hash))
        snapshot_hash=content_hash((symbol,cutoffs,features,mask,lineage,inventory.revision,account_id,
            inventory.lineage_hash))
        self._snapshot_inputs[snapshot_hash]=input_ids
        self._snapshot_inventory[snapshot_hash]=inventory
        release_row=self.ledger.connection.execute('SELECT active_release_hash FROM account_control WHERE account_id=?',(account_id,)).fetchone()
        snapshot=NumericSnapshot(candidate_id,symbol,snapshot_hash,cutoffs,features,mask,lineage,
            inventory.revision,account_id=account_id,release_hash=release_row[0] if release_row else None)
        self._snapshot_evidence[snapshot_hash]=snapshot
        return snapshot

    def encode(self, snapshots, context, dictionary, token_counter, *, candidate_order, ranked_bundle):
        """Encode only registered immutable snapshots and retain the exact proved bytes."""
        self._require_owner()
        if any(self._snapshot_evidence.get(snapshot.snapshot_hash)!=snapshot for snapshot in snapshots):
            raise StateUnavailable('encoding requires registered immutable snapshots')
        outcome=encode_state(snapshots,context,dictionary,token_counter,
            candidate_order=candidate_order,ranked_bundle=ranked_bundle)
        if type(outcome) is EncodedState:
            previous=self._encoded_evidence.get(outcome.state_hash)
            if previous is not None and previous!=outcome:
                raise StateUnavailable('same state bytes produced conflicting rank/token proof')
            self._encoded_evidence[outcome.state_hash]=outcome
        else:
            previous=self._hold_evidence.get(outcome.audit_hash)
            if previous is not None and previous!=outcome:
                raise StateUnavailable('same HOLD audit identity produced conflicting evidence')
            self._hold_evidence[outcome.audit_hash]=outcome
        return outcome

    def persist_encoded_state(self, encoded: EncodedState | StateHoldDraft) -> EncodedState | StateHold:
        """Commit frozen snapshot evidence before a state can be dispatched or HOLD returned."""
        self._require_owner()
        if type(encoded) not in (EncodedState,StateHoldDraft):
            raise ContractViolation('typed encoded state or HOLD draft required')
        if self.ledger.connection.in_transaction:
            raise ContractViolation('state persistence requires its own top-level commit')
        if type(encoded) is EncodedState:
            registered=self._encoded_evidence.get(encoded.state_hash)
            persisted=self._persisted_encoded_results.get(encoded.state_hash)
            if registered is None or (encoded!=registered and encoded!=persisted):
                raise StateUnavailable('encoded bytes/token proof were not registered by this engine')
            # Persist and verify against the original immutable, unpersisted snapshot evidence.
            encoded=registered
            ranked_bundle=encoded.ranked_bundle
            if (not encoded.candidate_order or type(ranked_bundle) is not ApprovedBundle
                    or encoded.rank_bundle_hash!=ranked_bundle.bundle_hash
                    or encoded.rank_bundle_hash!=encoded.snapshots[0].release_hash
                    or encoded.rank_policy_hash!=ranked_bundle.bundle.policy_hash
                    or encoded.candidate_order!=tuple(c.candidate_id for c in ranked_bundle.bundle.candidates)):
                raise StateUnavailable('encoded state lacks exact approved bundle/ranking identity')
            snapshots=encoded.snapshots+encoded.excluded_snapshots
            account_id=snapshots[0].account_id if snapshots else None
            release_hash=snapshots[0].release_hash if snapshots else None
            state_json=encoded.canonical_state_json
            state_hash=encoded.state_hash
            tokenizer_hash=content_hash((encoded.token_count.provider_id,encoded.token_count.tokenizer_id,
                encoded.token_count.proof_method,encoded.token_count.compatibility_hash))
            token_count=encoded.token_count.state_tokens
            included=set(encoded.candidate_ids)
            exclusion=dict(encoded.excluded_candidate_reasons)
            field_hash=encoded.field_dictionary_hash
            for snapshot in encoded.snapshots:
                if self._snapshot_evidence.get(snapshot.snapshot_hash)!=snapshot:
                    raise StateUnavailable('snapshot differs from immutable registered evidence')
                expected=_wire_values(snapshot.features,default_field_dictionary().candidate_fields,snapshot.candidate_id)
                rows=strict_json(state_json)[1]
                row=rows[encoded.candidate_ids.index(snapshot.candidate_id)]
                if row!=expected:
                    raise ContractViolation('candidate wire row differs from registered snapshot evidence')
        else:
            if self._hold_evidence.get(encoded.audit_hash)!=encoded:
                raise StateUnavailable('HOLD proof was not registered by this engine')
            ranked_bundle=encoded.ranked_bundle
            if (type(ranked_bundle) is not ApprovedBundle
                    or encoded.rank_bundle_hash!=ranked_bundle.bundle_hash
                    or encoded.rank_bundle_hash!=encoded.release_hash
                    or encoded.rank_policy_hash!=ranked_bundle.bundle.policy_hash
                    or encoded.candidate_order!=tuple(c.candidate_id for c in ranked_bundle.bundle.candidates)):
                raise StateUnavailable('HOLD lacks exact approved bundle/ranking identity')
            snapshots=encoded.snapshots
            account_id=encoded.account_id;release_hash=encoded.release_hash
            state_json=canonical_bytes({'outcome':'HOLD','reason':encoded.reason,
                'audit_hash':encoded.audit_hash,'candidate_order':encoded.candidate_order,
                'exclusions':encoded.excluded_candidate_reasons,'rank_bundle_hash':encoded.rank_bundle_hash,
                'rank_policy_hash':encoded.rank_policy_hash}).decode('ascii')
            state_hash=content_hash(strict_json(state_json));tokenizer_hash=None;token_count=None
            included=set();exclusion=dict(encoded.excluded_candidate_reasons)
            field_hash=default_field_dictionary().dictionary_hash
        if (not snapshots or len({s.account_id for s in snapshots})!=1
                or len({s.release_hash for s in snapshots})!=1
                or any(s.snapshot_hash not in self._snapshot_inputs for s in snapshots)):
            raise StateUnavailable('snapshot source lineage or shared identity unavailable')
        if type(release_hash) is not str or not re.fullmatch('[0-9a-f]{64}',release_hash):
            raise ContractViolation('invalid active release identity')
        for snapshot in snapshots:
            if self._snapshot_evidence.get(snapshot.snapshot_hash)!=snapshot:
                raise StateUnavailable('snapshot differs from immutable registered evidence')
            inventory=self._snapshot_inventory.get(snapshot.snapshot_hash)
            if inventory is None or (inventory.account_id!=snapshot.account_id
                    or inventory.cutoffs!=snapshot.cutoffs):
                raise StateUnavailable('historical inventory evidence binding unavailable')
        first=snapshots[0]
        batch_identity=(first.cutoffs,first.account_id,first.release_hash,first.account_revision)
        if any((s.cutoffs,s.account_id,s.release_hash,s.account_revision)!=batch_identity for s in snapshots):
            raise StateUnavailable('persisted snapshot batch identity mismatch')
        now=self.ledger._clock_us();persisted=[]
        with self.ledger.transaction() as tx:
            control=tx.execute('SELECT active_release_hash FROM account_control WHERE account_id=?',(account_id,)).fetchone()
            if control is None or control[0]!=release_hash:
                raise StateUnavailable('active release changed before state persistence')
            for snapshot in snapshots:
                row_reason=exclusion.get(snapshot.candidate_id)
                valid=int(snapshot.candidate_id in included)
                snapshot_id=content_hash((snapshot.snapshot_hash,state_hash,account_id,release_hash,
                    getattr(encoded,'rank_bundle_hash',release_hash),getattr(encoded,'rank_policy_hash',None),row_reason))
                account_revision=canonical_bytes(snapshot.account_revision).decode('ascii')
                values=(snapshot_id,account_id,release_hash,snapshot.cutoffs.candle_close_us,
                    snapshot.cutoffs.knowledge_cutoff_us,field_hash,state_json,tokenizer_hash,token_count,
                    snapshot.lineage_hash,account_revision,valid,row_reason,now)
                old=tx.execute('SELECT * FROM snapshots WHERE snapshot_id=?',(snapshot_id,)).fetchone()
                input_ids=self._snapshot_inputs[snapshot.snapshot_hash]
                if old is not None:
                    if tuple(old)[:-1]!=values[:-1]:raise ContractViolation('snapshot identity conflict')
                    old_inputs=tuple(row[0] for row in tx.execute(
                        'SELECT event_id FROM snapshot_inputs WHERE snapshot_id=? ORDER BY line_no',(snapshot_id,)))
                    if old_inputs!=input_ids:raise ContractViolation('snapshot lineage identity conflict')
                else:
                    tx.execute('INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',values)
                    for line_no,event_id in enumerate(input_ids):
                        tx.execute('INSERT INTO snapshot_inputs VALUES (?,?,?,NULL,NULL,NULL,?)',
                            (snapshot_id,line_no,event_id,now))
                persisted.append(replace(snapshot,snapshot_id=snapshot_id))
        if self.ledger.connection.in_transaction:
            raise ContractViolation('state audit transaction did not commit at top level')
        if type(encoded) is StateHoldDraft:
            committed=self._persisted_holds.get(encoded.audit_hash)
            if committed is not None:return committed
            committed=StateHold(encoded,tuple(item.snapshot_id for item in persisted),now)
            self._persisted_holds[encoded.audit_hash]=committed
            return committed
        included_rows=tuple(item for item in persisted if item.candidate_id in included)
        excluded_rows=tuple(item for item in persisted if item.candidate_id not in included)
        persisted_encoded=replace(encoded,snapshots=included_rows,excluded_snapshots=excluded_rows)
        committed=self._persisted_encoded_results.get(encoded.state_hash)
        if committed is not None and committed!=persisted_encoded:
            raise ContractViolation('persisted encoded-state identity conflict')
        self._persisted_encoded_results[encoded.state_hash]=persisted_encoded
        return committed or persisted_encoded

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
        # A logical key names one fact about one instrument across all of its revisions.
        if c.execute('SELECT 1 FROM source_events WHERE source=? AND stream=? AND logical_key=? AND instrument IS NOT ?',
                (event.source,event.stream,event.logical_key,event.instrument)).fetchone():
            return self._reject(event,'invalid_supersession')
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

    def select(self,cutoffs: CausalCutoffs,*,instrument=None,event_since_us=None) -> tuple[SourceEvent,...]:
        """Latest visible revision per logical key, optionally bounded to one instrument/exchange-time window."""
        self._require_owner()
        if type(cutoffs) is not CausalCutoffs:raise ContractViolation('typed frozen cutoffs required')
        if (instrument is not None and type(instrument) is not str) or (event_since_us is not None and type(event_since_us) is not int):
            raise ContractViolation('typed selection window required')
        if self.ledger.connection.in_transaction:raise ContractViolation('selection requires committed database')
        selected={}
        # Rows committed after the knowledge cutoff never join or re-rank a historical selection.
        # Ranking happens over every revision known by the cutoff before any exchange-time or
        # instrument window, so a correction can hide a key but never resurrect what it superseded.
        known=lambda x:(f"{x}.recorded_at_us<=:k AND {x}.known_at_us<=:k AND {x}.received_at_us<=:k"
            f" AND NOT EXISTS (SELECT 1 FROM source_invalidations i WHERE i.event_id={x}.event_id"
            " AND i.reason!='source_revision_superseded' AND i.known_at_us<=:k AND i.recorded_at_us<=:k)")
        sql=("SELECT * FROM source_events e WHERE "+known('e')+" AND e.event_at_us<=:close"
            " AND NOT EXISTS (SELECT 1 FROM source_events r WHERE r.source=e.source AND r.stream=e.stream"
            " AND r.logical_key=e.logical_key AND "+known('r')+
            " AND (r.known_at_us,r.revision,r.event_id)>(e.known_at_us,e.revision,e.event_id))")
        args={'k':cutoffs.knowledge_cutoff_us,'close':cutoffs.candle_close_us}
        if instrument is not None:sql+=' AND e.instrument=:instrument';args['instrument']=instrument
        if event_since_us is not None:sql+=' AND e.event_at_us>=:since';args['since']=event_since_us
        for row in self.ledger.connection.execute(sql,args):
            # source_events is append-only (enforced by triggers), so a decoded row never goes stale.
            event=self._decoded.get(row['event_id'])
            if event is None:
                event=_from_row(row);self._decoded[event.event_id]=event
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
