package tools

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/engine"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/feed"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/testutil"
)

func service(t *testing.T) (*Service, *testutil.FakeExchange) {
	t.Helper()
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	fx := testutil.NewFakeExchange(t, map[string][]market.Bar{
		"BTC-USDT": testutil.GARCH(3000, start, 15*time.Minute, 1),
		"ETH-USDT": testutil.GARCH(3000, start, 15*time.Minute, 2),
	})
	okx := feed.NewOKX()
	okx.BaseURL = fx.URL
	der := feed.NewDeribit()
	der.BaseURL = fx.URL
	cfg := engine.DefaultConfig("", market.MustTimeframe("15m"))
	return &Service{Config: cfg, OKX: okx, Deribit: der, Ref: "BTC-USDT", Universe: []string{"BTC-USDT", "ETH-USDT"}}, fx
}

func call(t *testing.T, s *Service, name, args string) any {
	t.Helper()
	tool, ok := Find(s.Tools(), name)
	if !ok {
		t.Fatalf("no tool %s", name)
	}
	out, err := tool.Run(context.Background(), json.RawMessage(args))
	if err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	return out
}

func TestToolsEndToEnd(t *testing.T) {
	s, fx := service(t)
	b := call(t, s, "analyze_symbol", `{"symbol":"eth-usdt"}`).(Brief)
	if b.Symbol != "ETH-USDT" || !b.Ready || len(b.Frames) != 3 || b.Context == nil || b.Deriv == nil {
		t.Fatalf("brief %+v", b)
	}
	before := fx.Requests.Load()
	call(t, s, "volatility_forecast", `{"symbol":"ETH-USDT"}`) // served from the cached desk
	if fx.Requests.Load() != before {
		t.Fatal("cached desk refetched history")
	}
	rows := call(t, s, "scan_market", `{}`).([]ScanRow)
	if len(rows) != 2 {
		t.Fatalf("scan rows %+v", rows)
	}
	r := call(t, s, "get_readings", `{"symbol":"BTC-USDT","frame":"high"}`).(engine.Reading)
	if r.TF != "4h" {
		t.Fatalf("high frame of 15m should be 4h, got %s", r.TF)
	}
	rep := call(t, s, "model_report", `{"symbol":"BTC-USDT"}`).(map[string]any)
	if _, ok := rep["long_status"]; !ok {
		t.Fatalf("model report %v", rep)
	}
	series := call(t, s, "compute_indicator", `{"symbol":"BTC-USDT","timeframe":"15m","indicator":"macd","points":5}`).(map[string]any)
	if got := series["series"].(map[string]Series)["histogram"]; len(got.Values) != 5 {
		t.Fatalf("macd series %+v", got)
	}
	if _, err := Find(s.Tools(), "analyze_symbol"); !err {
		t.Fatal("find")
	}
	tool, _ := Find(s.Tools(), "analyze_symbol")
	if _, err := tool.Run(context.Background(), json.RawMessage(`{"symbol":"BTC-USDT","oops":1}`)); err == nil {
		t.Fatal("unknown fields must be rejected")
	}
	if _, err := tool.Run(context.Background(), json.RawMessage(`{"symbol":"NOPE-USDT"}`)); err == nil || !strings.Contains(err.Error(), "51001") {
		t.Fatalf("unknown instrument error: %v", err)
	}
	led := call(t, s, "signal_ledger", `{}`).(map[string]any)
	if led["enabled"] != false {
		t.Fatalf("ledger %v", led)
	}
}

func TestComputeEveryIndicator(t *testing.T) {
	bars := testutil.Fixture(t)
	for _, n := range IndicatorNames {
		out, err := Compute(n, 0, bars)
		if err != nil {
			t.Fatalf("%s: %v", n, err)
		}
		for k, s := range out {
			if len(s.Values) < 1000 || len(s.Values) != len(s.Times) {
				t.Fatalf("%s.%s: %d values", n, k, len(s.Values))
			}
		}
	}
	if _, err := Compute("astrology", 0, bars); err == nil {
		t.Fatal("unknown indicator accepted")
	}
	hits := ScanPatterns(bars, 300)
	if len(hits) == 0 {
		t.Fatal("no candlestick patterns in 300 real hourly bars")
	}
}

func TestNormSymbol(t *testing.T) {
	for in, want := range map[string]string{"btc-usdt": "BTC-USDT", "ETHUSDT": "ETH-USDT", " sol-usdc ": "SOL-USDC"} {
		if got, err := normSymbol(in); err != nil || got != want {
			t.Fatalf("%q -> %q, %v", in, got, err)
		}
	}
	for _, bad := range []string{"", "BTC", "../etc", "BTC-USDT;rm"} {
		if _, err := normSymbol(bad); err == nil {
			t.Fatalf("%q accepted", bad)
		}
	}
}
