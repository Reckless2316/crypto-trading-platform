package ledger

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAppendVerifyAndTamper(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.jsonl")
	l, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		if _, err := l.Append("signal", map[string]any{"i": i, "action": "LONG"}); err != nil {
			t.Fatal(err)
		}
	}
	seq, head := l.Head()
	l.Close()
	if seq != 3 || head == Genesis {
		t.Fatalf("head %d %s", seq, head)
	}
	// Reopening continues the chain.
	l2, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	e, err := l2.Append("outcome", map[string]any{"r": 2})
	if err != nil || e.Seq != 4 || e.PrevHash != head {
		t.Fatalf("continued entry %+v err %v", e, err)
	}
	l2.Close()

	// Editing any payload breaks verification.
	b, _ := os.ReadFile(path)
	tampered := strings.Replace(string(b), `"action":"LONG"`, `"action":"SHORT"`, 1)
	if err := os.WriteFile(path, []byte(tampered), 0o600); err != nil {
		t.Fatal(err)
	}
	f, _ := os.Open(path)
	defer f.Close()
	if _, _, err := Verify(f); !errors.Is(err, ErrBroken) {
		t.Fatalf("tampering not detected: %v", err)
	}
	if _, err := Open(path); err == nil {
		t.Fatal("opening a broken ledger must fail")
	}
}
