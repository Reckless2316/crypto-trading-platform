package engine

import (
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// Aggregator builds higher-timeframe bars from base bars aligned to UTC
// boundaries. A higher bar is emitted only once it is complete (its last
// constituent base bar has closed), so higher-timeframe readings never use
// information from the future.
type Aggregator struct {
	base, target time.Duration
	cur          market.Bar
	bucket       int64
	have         bool
}

func NewAggregator(base, target time.Duration) *Aggregator {
	return &Aggregator{base: base, target: target, bucket: -1}
}

func bucketOf(t time.Time, d time.Duration) int64 {
	if d == 7*24*time.Hour {
		// Weeks start Monday 00:00 UTC (the Unix epoch was a Thursday).
		return (t.Unix() - 4*86400) / int64(d/time.Second)
	}
	return t.Unix() / int64(d/time.Second)
}

func bucketStart(b int64, d time.Duration) time.Time {
	if d == 7*24*time.Hour {
		return time.Unix(b*int64(d/time.Second)+4*86400, 0).UTC()
	}
	return time.Unix(b*int64(d/time.Second), 0).UTC()
}

// Add consumes a base bar and returns completed higher-timeframe bars (zero,
// one, or two when a gap closed a partial bucket).
func (a *Aggregator) Add(b market.Bar) []market.Bar {
	var out []market.Bar
	k := bucketOf(b.Time, a.target)
	if a.have && k != a.bucket {
		// A gap in the base stream: flush the partial bucket as complete.
		out = append(out, a.cur)
		a.have = false
	}
	if !a.have {
		a.cur = market.Bar{Time: bucketStart(k, a.target), Open: b.Open, High: b.High, Low: b.Low, Close: b.Close, Volume: b.Volume}
		a.bucket = k
		a.have = true
	} else {
		if b.High > a.cur.High {
			a.cur.High = b.High
		}
		if b.Low < a.cur.Low {
			a.cur.Low = b.Low
		}
		a.cur.Close = b.Close
		a.cur.Volume += b.Volume
	}
	if !b.Time.Add(a.base).Before(bucketStart(k, a.target).Add(a.target)) {
		out = append(out, a.cur)
		a.have = false
	}
	return out
}
