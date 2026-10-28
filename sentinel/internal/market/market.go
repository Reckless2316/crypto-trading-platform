// Package market holds the plain data types shared by every Sentinel package:
// OHLCV bars, timeframes, and the derivatives and options snapshots that feed
// the microstructure and sentiment layers.
package market

import (
	"fmt"
	"strings"
	"time"
)

// Bar is one closed OHLCV candle. Time is the bar's open time in UTC.
type Bar struct {
	Time   time.Time `json:"time"`
	Open   float64   `json:"open"`
	High   float64   `json:"high"`
	Low    float64   `json:"low"`
	Close  float64   `json:"close"`
	Volume float64   `json:"volume"`
}

// Valid reports whether the bar is internally consistent.
func (b Bar) Valid() bool {
	return b.Open > 0 && b.High > 0 && b.Low > 0 && b.Close > 0 && b.Volume >= 0 &&
		b.High >= b.Low && b.High >= b.Open && b.High >= b.Close && b.Low <= b.Open && b.Low <= b.Close
}

// Typical returns (H+L+C)/3.
func (b Bar) Typical() float64 { return (b.High + b.Low + b.Close) / 3 }

// Range returns H-L.
func (b Bar) Range() float64 { return b.High - b.Low }

// Body returns |C-O|.
func (b Bar) Body() float64 {
	if b.Close >= b.Open {
		return b.Close - b.Open
	}
	return b.Open - b.Close
}

// Timeframe is a bar duration with an exchange-neutral name such as "15m" or "4h".
type Timeframe struct {
	Name string
	Dur  time.Duration
}

var timeframes = []Timeframe{
	{"1m", time.Minute},
	{"5m", 5 * time.Minute},
	{"15m", 15 * time.Minute},
	{"30m", 30 * time.Minute},
	{"1h", time.Hour},
	{"2h", 2 * time.Hour},
	{"4h", 4 * time.Hour},
	{"12h", 12 * time.Hour},
	{"1d", 24 * time.Hour},
	{"1w", 7 * 24 * time.Hour},
}

// ParseTimeframe accepts names like "15m", "1h", "4H", "1d".
func ParseTimeframe(s string) (Timeframe, error) {
	n := strings.ToLower(strings.TrimSpace(s))
	for _, tf := range timeframes {
		if tf.Name == n {
			return tf, nil
		}
	}
	return Timeframe{}, fmt.Errorf("unsupported timeframe %q", s)
}

// MustTimeframe panics on an unknown name; for constants and tests.
func MustTimeframe(s string) Timeframe {
	tf, err := ParseTimeframe(s)
	if err != nil {
		panic(err)
	}
	return tf
}

// Ladder returns the three timeframes Sentinel analyses for a base timeframe:
// the base (entry timing), a mid timeframe (setup) and a high timeframe (trend).
// The higher frames are built from base bars, so they never look ahead.
func Ladder(base Timeframe) (mid, high Timeframe) {
	switch base.Name {
	case "1m":
		return MustTimeframe("5m"), MustTimeframe("15m")
	case "5m":
		return MustTimeframe("15m"), MustTimeframe("1h")
	case "15m":
		return MustTimeframe("1h"), MustTimeframe("4h")
	case "30m":
		return MustTimeframe("2h"), MustTimeframe("12h")
	case "1h":
		return MustTimeframe("4h"), MustTimeframe("1d")
	case "2h":
		return MustTimeframe("12h"), MustTimeframe("1w")
	case "4h":
		return MustTimeframe("1d"), MustTimeframe("1w")
	default:
		return base, base
	}
}

// Derivatives is a point-in-time perpetual swap snapshot (OKX public data).
type Derivatives struct {
	Time         time.Time `json:"time"`
	FundingRate  float64   `json:"funding_rate"`  // current period rate, e.g. 0.0001 = 0.01%
	FundingAvg   float64   `json:"funding_avg"`   // mean of recent settled periods
	OpenInterest float64   `json:"open_interest"` // in coin units
	OIChangePct  float64   `json:"oi_change_pct"` // percent change over the OI window
	PriceChgPct  float64   `json:"price_chg_pct"` // percent change of price over the same window
	LongShort    float64   `json:"long_short"`    // account long/short ratio, 0 when unknown
	BasisPct     float64   `json:"basis_pct"`     // (perp mark - spot) / spot * 100
	Available    bool      `json:"available"`
}

// Options is a point-in-time options-market sentiment snapshot (Deribit public data).
type Options struct {
	Time       time.Time `json:"time"`
	PCRByOI    float64   `json:"pcr_oi"`     // put open interest / call open interest
	PCRByVol   float64   `json:"pcr_volume"` // put 24h volume / call 24h volume
	DVOL       float64   `json:"dvol"`       // Deribit implied-volatility index
	DVOLChange float64   `json:"dvol_change_pct"`
	Skew25     float64   `json:"skew"` // OTM call IV minus OTM put IV (risk reversal proxy), vol points
	Available  bool      `json:"available"`
}
