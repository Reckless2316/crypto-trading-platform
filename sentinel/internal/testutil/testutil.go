// Package testutil provides deterministic market data for tests.
package testutil

import (
	"math"
	"math/rand"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// Fixture loads testdata/btc_usdt_1h.csv (2,000 real OKX BTC-USDT hourly bars).
func Fixture(t testing.TB) []market.Bar {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	bars, err := market.LoadCSV(filepath.Join(filepath.Dir(file), "..", "..", "testdata", "btc_usdt_1h.csv"))
	if err != nil {
		t.Fatal(err)
	}
	return bars
}

// GARCH generates n bars of a GARCH(1,1) price path with volatility clustering,
// starting at start and spaced by step. Deterministic for a given seed.
func GARCH(n int, start time.Time, step time.Duration, seed int64) []market.Bar {
	rng := rand.New(rand.NewSource(seed))
	price, variance := 100.0, 1e-5
	const omega, alpha, beta = 1e-7, 0.08, 0.9
	bars := make([]market.Bar, n)
	prevRet := 0.0
	for i := 0; i < n; i++ {
		variance = omega + alpha*prevRet*prevRet + beta*variance
		sd := math.Sqrt(variance)
		ret := sd * rng.NormFloat64()
		open := price
		price *= math.Exp(ret)
		hi := math.Max(open, price) * math.Exp(math.Abs(rng.NormFloat64())*sd*0.5)
		lo := math.Min(open, price) * math.Exp(-math.Abs(rng.NormFloat64())*sd*0.5)
		bars[i] = market.Bar{Time: start.Add(time.Duration(i) * step), Open: open, High: hi, Low: lo, Close: price,
			Volume: 100 * (1 + math.Abs(rng.NormFloat64()))}
		prevRet = ret
	}
	return bars
}
