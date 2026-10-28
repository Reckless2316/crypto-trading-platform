package market

import (
	"bytes"
	"strings"
	"testing"
	"time"
)

func TestCSVRoundTrip(t *testing.T) {
	in := []Bar{{Time: time.UnixMilli(1767225600000).UTC(), Open: 1, High: 2, Low: 0.5, Close: 1.5, Volume: 10}}
	var buf bytes.Buffer
	if err := WriteCSV(&buf, in); err != nil {
		t.Fatal(err)
	}
	out, err := ReadCSV(&buf)
	if err != nil || len(out) != 1 || out[0] != in[0] {
		t.Fatalf("%+v %v", out, err)
	}
	if _, err := ReadCSV(strings.NewReader("ts,open\n1,2\n")); err == nil {
		t.Fatal("missing columns accepted")
	}
	secs, err := ReadCSV(strings.NewReader("time,open,high,low,close,volume\n1767225600,1,1,1,1,1\n"))
	if err != nil || !secs[0].Time.Equal(in[0].Time) {
		t.Fatalf("unix seconds: %+v %v", secs, err)
	}
}

func TestTimeframesAndLadder(t *testing.T) {
	tf, err := ParseTimeframe("4H")
	if err != nil || tf.Dur != 4*time.Hour {
		t.Fatal(tf, err)
	}
	if _, err := ParseTimeframe("7m"); err == nil {
		t.Fatal("bad timeframe accepted")
	}
	mid, high := Ladder(MustTimeframe("15m"))
	if mid.Name != "1h" || high.Name != "4h" {
		t.Fatal(mid, high)
	}
	for _, name := range []string{"5m", "15m", "30m", "1h", "2h", "4h"} {
		base := MustTimeframe(name)
		m, h := Ladder(base)
		if m.Dur%base.Dur != 0 || h.Dur%m.Dur != 0 || h.Dur <= m.Dur {
			t.Fatalf("%s ladder %s/%s is not nested", name, m.Name, h.Name)
		}
	}
	if !(Bar{Open: 1, High: 2, Low: 1, Close: 2, Volume: 0}).Valid() || (Bar{Open: 1, High: 0.5, Low: 1, Close: 1}).Valid() {
		t.Fatal("Valid")
	}
}
