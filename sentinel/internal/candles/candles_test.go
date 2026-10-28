package candles

import (
	"testing"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

func bar(o, h, l, c float64) market.Bar {
	return market.Bar{Time: time.Unix(0, 0), Open: o, High: h, Low: l, Close: c, Volume: 1}
}

func TestPatterns(t *testing.T) {
	const atr = 2.0
	cases := []struct {
		name   string
		window []market.Bar
		trend  int
		want   string
		bias   int
	}{
		{"engulfing", []market.Bar{bar(100, 100.5, 98.5, 99), bar(98.8, 101.5, 98.6, 101)}, -1, "Bullish Engulfing", Bullish},
		{"bear engulfing", []market.Bar{bar(100, 101.5, 99.5, 101), bar(101.2, 101.4, 98.5, 99)}, 1, "Bearish Engulfing", Bearish},
		{"hammer", []market.Bar{bar(100, 100.2, 99.9, 100.1), bar(99.0, 99.75, 96.8, 99.7)}, -1, "Hammer", Bullish},
		{"shooting star", []market.Bar{bar(100, 100.2, 99.9, 100.1), bar(101.0, 103.2, 100.25, 100.3)}, 1, "Shooting Star", Bearish},
		{"morning star", []market.Bar{bar(104, 104.2, 99.8, 100), bar(99.5, 99.9, 99, 99.6), bar(99.8, 103.2, 99.7, 103)}, -1, "Morning Star", Bullish},
		{"three white soldiers", []market.Bar{bar(100, 102.05, 99.9, 102), bar(101, 104.05, 100.9, 104), bar(103, 106.05, 102.9, 106)}, 0, "Three White Soldiers", Bullish},
		{"three black crows", []market.Bar{bar(106, 106.1, 103.95, 104), bar(105, 105.1, 101.95, 102), bar(103, 103.1, 99.95, 100)}, 0, "Three Black Crows", Bearish},
		{"bull marubozu", []market.Bar{bar(100, 100.1, 99.9, 100), bar(100, 103, 100, 103)}, 0, "Bullish Marubozu", Bullish},
		{"doji", []market.Bar{bar(100, 100.1, 99.9, 100), bar(100, 100.6, 99.4, 100.02)}, 0, "Doji", Neutral},
	}
	for _, c := range cases {
		p, ok := Detect(c.window, atr, c.trend)
		if !ok || p.Name != c.want || p.Bias != c.bias {
			t.Errorf("%s: got %+v ok=%v, want %s", c.name, p, ok, c.want)
		}
		if ok && (p.Strength < 0 || p.Strength > 1) {
			t.Errorf("%s: strength %v out of range", c.name, p.Strength)
		}
	}
}

func TestTrendFilter(t *testing.T) {
	// A hammer shape in an uptrend is a hanging man, never a bullish hammer.
	p, ok := Detect([]market.Bar{bar(100, 100.2, 99.9, 100.1), bar(99.0, 99.75, 96.8, 99.7)}, 2, 1)
	if !ok || p.Name != "Hanging Man" || p.Bias != Bearish {
		t.Fatalf("got %+v", p)
	}
}

func TestEveryNameIsListed(t *testing.T) {
	if len(Names) != 33 {
		t.Fatalf("expected the 33 MotiveWave study patterns, got %d", len(Names))
	}
	if _, ok := Detect(nil, 1, 0); ok {
		t.Fatal("empty window matched")
	}
}
