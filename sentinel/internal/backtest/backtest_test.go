package backtest

import (
	"strings"
	"testing"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/engine"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/testutil"
)

func TestBacktestReport(t *testing.T) {
	bars := testutil.Fixture(t)
	cfg := engine.DefaultConfig("", market.MustTimeframe("1h"))
	r := Run(map[string][]market.Bar{"BTC-USDT": bars}, "", cfg)
	if r.Bars != len(bars) || r.Long.Model.Samples == 0 || r.Short.Model.Samples == 0 {
		t.Fatalf("report %+v", r)
	}
	if r.Long.BaselineR >= 0 {
		t.Fatalf("entering every hour with OKX US fees should lose: baseline %v", r.Long.BaselineR)
	}
	if len(r.Verdict) < 3 || !strings.Contains(r.Text(), "VERDICT") {
		t.Fatalf("verdict %v", r.Verdict)
	}
	if f := r.Volatility["BTC-USDT"]; f.Samples == 0 {
		t.Fatal("volatility forecast not scored")
	}
}

func TestBootstrapCI(t *testing.T) {
	x := []float64{1, 1, 1, 1}
	ci := bootstrapCI(x, 200, 1)
	if ci[0] != 1 || ci[1] != 1 {
		t.Fatalf("constant sample CI %v", ci)
	}
}
