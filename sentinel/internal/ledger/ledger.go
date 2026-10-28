// Package ledger is an append-only, hash-chained JSONL journal of live
// signals and their resolved outcomes. Each entry commits to the previous
// entry's hash, so the forward track record cannot be edited after the fact
// without breaking verification: performance claims become checkable instead
// of self-reported.
package ledger

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sync"
	"time"
)

// Genesis is the previous-hash of the first entry.
const Genesis = "0000000000000000000000000000000000000000000000000000000000000000"

// Entry is one journal line.
type Entry struct {
	Seq      int             `json:"seq"`
	Time     time.Time       `json:"time"`
	Kind     string          `json:"kind"` // "signal" or "outcome"
	Payload  json.RawMessage `json:"payload"`
	PrevHash string          `json:"prev_hash"`
	Hash     string          `json:"hash"`
}

func entryHash(seq int, t time.Time, kind string, payload []byte, prev string) string {
	h := sha256.New()
	fmt.Fprintf(h, "%d|%s|%s|%s|", seq, t.UTC().Format(time.RFC3339Nano), kind, prev)
	h.Write(payload)
	return hex.EncodeToString(h.Sum(nil))
}

// Ledger appends to a file.
type Ledger struct {
	mu   sync.Mutex
	f    *os.File
	seq  int
	last string
	Now  func() time.Time
}

// Open opens (or creates) a ledger file, verifying the existing chain.
func Open(path string) (*Ledger, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o600)
	if err != nil {
		return nil, err
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		f.Close()
		return nil, err
	}
	n, last, err := Verify(f)
	if err != nil {
		f.Close()
		return nil, fmt.Errorf("ledger %s failed verification: %w", path, err)
	}
	return &Ledger{f: f, seq: n, last: last, Now: time.Now}, nil
}

// Append writes one entry with a canonical JSON payload.
func (l *Ledger) Append(kind string, payload any) (Entry, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return Entry{}, err
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	e := Entry{Seq: l.seq + 1, Time: l.Now().UTC(), Kind: kind, Payload: raw, PrevHash: l.last}
	e.Hash = entryHash(e.Seq, e.Time, e.Kind, raw, e.PrevHash)
	line, err := json.Marshal(e)
	if err != nil {
		return Entry{}, err
	}
	if _, err := l.f.Write(append(line, '\n')); err != nil {
		return Entry{}, err
	}
	if err := l.f.Sync(); err != nil {
		return Entry{}, err
	}
	l.seq, l.last = e.Seq, e.Hash
	return e, nil
}

// Head returns the sequence number and hash of the latest entry.
func (l *Ledger) Head() (int, string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.seq, l.last
}

// Close closes the file.
func (l *Ledger) Close() error { return l.f.Close() }

// ErrBroken reports a chain break.
var ErrBroken = errors.New("hash chain broken")

// Verify checks every entry of r and returns the entry count and head hash.
func Verify(r io.Reader) (int, string, error) {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 1<<20), 16<<20)
	prev, n := Genesis, 0
	for sc.Scan() {
		line := bytes.TrimSpace(sc.Bytes())
		if len(line) == 0 {
			continue
		}
		var e Entry
		if err := json.Unmarshal(line, &e); err != nil {
			return n, prev, fmt.Errorf("entry %d: %w", n+1, err)
		}
		if e.Seq != n+1 {
			return n, prev, fmt.Errorf("%w: entry %d has seq %d", ErrBroken, n+1, e.Seq)
		}
		if e.PrevHash != prev {
			return n, prev, fmt.Errorf("%w: entry %d prev_hash mismatch", ErrBroken, e.Seq)
		}
		if h := entryHash(e.Seq, e.Time, e.Kind, e.Payload, e.PrevHash); h != e.Hash {
			return n, prev, fmt.Errorf("%w: entry %d hash mismatch (edited?)", ErrBroken, e.Seq)
		}
		prev, n = e.Hash, n+1
	}
	return n, prev, sc.Err()
}

// ReadAll returns every entry of a ledger file.
func ReadAll(path string) ([]Entry, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var out []Entry
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 16<<20)
	for sc.Scan() {
		if len(bytes.TrimSpace(sc.Bytes())) == 0 {
			continue
		}
		var e Entry
		if err := json.Unmarshal(sc.Bytes(), &e); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, sc.Err()
}
