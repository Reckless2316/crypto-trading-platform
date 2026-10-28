package desk

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/engine"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/ledger"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/testutil"
)

func TestWarmupThenLiveJournalsAndPublishes(t *testing.T) {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	btc := testutil.GARCH(2600, start, 15*time.Minute, 11)
	eth := testutil.GARCH(2600, start, 15*time.Minute, 12)
	cfg := engine.DefaultConfig("", market.MustTimeframe("15m"))
	cfg.MinScore, cfg.MinLayers = 1, 1 // fire often so the ledger sees signals
	d := New([]string{"BTC-USDT", "ETH-USDT"}, "BTC-USDT", cfg, nil)
	lg, err := ledger.Open(filepath.Join(t.TempDir(), "l.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	d.Ledger = lg
	d.Warmup(map[string][]market.Bar{"BTC-USDT": btc[:2300], "ETH-USDT": eth[:2300]})
	if n, _ := lg.Head(); n != 0 {
		t.Fatalf("warm-up wrote %d ledger entries; only live signals belong there", n)
	}
	e, _ := d.Engine("ETH-USDT")
	if !e.Last().Context.Available {
		t.Fatal("ETH should carry BTC context")
	}
	ch, cancel := d.Subscribe()
	defer cancel()
	published := 0
	emitted := 0
	for i := 2300; i < 2600; i++ {
		for _, sym := range []string{"BTC-USDT", "ETH-USDT"} {
			bars := btc
			if sym == "ETH-USDT" {
				bars = eth
			}
			snap, ok := d.OnBar(sym, bars[i])
			if !ok {
				t.Fatal("bar rejected")
			}
			if snap.Signal.Emitted {
				emitted++
			}
			select {
			case <-ch:
				published++
			default:
			}
		}
	}
	if _, ok := d.OnBar("BTC-USDT", btc[2500]); ok {
		t.Fatal("an out-of-order bar must be ignored")
	}
	if published == 0 || emitted == 0 {
		t.Fatalf("published %d emitted %d", published, emitted)
	}
	n, _ := lg.Head()
	if n < emitted {
		t.Fatalf("ledger has %d entries for %d emitted signals", n, emitted)
	}
}
