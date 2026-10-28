package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"
	"sync"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/backtest"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/engine"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/feed"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/ledger"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// engineFlags are shared by every command that runs the engine.
type engineFlags struct {
	tf        string
	stopATR   float64
	targetR   float64
	horizon   int
	feeEntry  float64
	feeExit   float64
	slippage  float64
	minScore  float64
	minLayers int
	minEdge   float64
	warm      int
	okxURL    string
}

func (f *engineFlags) register(fs *flag.FlagSet) {
	d := engine.DefaultConfig("", market.MustTimeframe("1h"))
	fs.StringVar(&f.tf, "tf", "1h", "base timeframe (5m, 15m, 30m, 1h, 2h, 4h); mid and high frames are derived")
	fs.Float64Var(&f.stopATR, "stop-atr", d.StopATR, "stop distance (1R) in base-frame ATR")
	fs.Float64Var(&f.targetR, "target-r", d.TargetR, "profit target in R")
	fs.IntVar(&f.horizon, "horizon", d.Horizon, "label horizon in base bars")
	fs.Float64Var(&f.feeEntry, "fee-entry", d.FeeEntryPct, "entry fee percent (OKX US Lv1 maker 0.20)")
	fs.Float64Var(&f.feeExit, "fee-exit", d.FeeExitPct, "exit fee percent (OKX US Lv1 taker 0.35)")
	fs.Float64Var(&f.slippage, "slippage", d.SlippagePct, "round-trip slippage percent")
	fs.Float64Var(&f.minScore, "min-score", d.MinScore, "minimum winning confluence score")
	fs.IntVar(&f.minLayers, "min-layers", d.MinLayers, "minimum agreeing layers")
	fs.Float64Var(&f.minEdge, "min-edge", d.MinEdgeR, "minimum model net edge in R once warm")
	fs.IntVar(&f.warm, "warm-samples", d.WarmSamples, "resolved labels before the model gates signals")
	fs.StringVar(&f.okxURL, "okx", "https://www.okx.com", "OKX REST base URL (https://us.okx.com also serves public spot data)")
}

func (f *engineFlags) config() (engine.Config, error) {
	tf, err := market.ParseTimeframe(f.tf)
	if err != nil {
		return engine.Config{}, err
	}
	switch tf.Name {
	case "1m", "1d", "1w", "12h":
		return engine.Config{}, fmt.Errorf("base timeframe %s is not supported (use 5m..4h)", tf.Name)
	}
	c := engine.DefaultConfig("", tf)
	c.StopATR, c.TargetR, c.Horizon = f.stopATR, f.targetR, f.horizon
	c.FeeEntryPct, c.FeeExitPct, c.SlippagePct = f.feeEntry, f.feeExit, f.slippage
	c.MinScore, c.MinLayers, c.MinEdgeR, c.WarmSamples = f.minScore, f.minLayers, f.minEdge, f.warm
	c.CooldownBars = max(1, c.Horizon/4)
	return c, nil
}

func (f *engineFlags) okx() *feed.OKX {
	c := feed.NewOKX()
	c.BaseURL = f.okxURL
	return c
}

func splitSymbols(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.ToUpper(strings.TrimSpace(p)); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// fetchAll downloads history for every symbol concurrently.
func fetchAll(ctx context.Context, okx *feed.OKX, syms []string, tf market.Timeframe, n int) (map[string][]market.Bar, error) {
	type res struct {
		sym  string
		bars []market.Bar
		err  error
	}
	ch := make(chan res, len(syms))
	sem := make(chan struct{}, 4)
	var wg sync.WaitGroup
	for _, s := range syms {
		wg.Add(1)
		go func(s string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			b, err := okx.History(ctx, s, tf, n)
			ch <- res{s, b, err}
		}(s)
	}
	wg.Wait()
	close(ch)
	out := map[string][]market.Bar{}
	for r := range ch {
		if r.err != nil {
			return nil, fmt.Errorf("%s: %w", r.sym, r.err)
		}
		if len(r.bars) == 0 {
			return nil, fmt.Errorf("%s: no candles returned (check the instrument id, e.g. BTC-USDT)", r.sym)
		}
		out[r.sym] = r.bars
	}
	return out, nil
}

// loadOrFetch reads a CSV when given, else downloads.
func loadOrFetch(ctx context.Context, okx *feed.OKX, csvPath string, syms []string, tf market.Timeframe, n int) (map[string][]market.Bar, error) {
	if csvPath != "" {
		if len(syms) != 1 {
			return nil, fmt.Errorf("-csv works with exactly one -symbol")
		}
		b, err := market.LoadCSV(csvPath)
		if err != nil {
			return nil, err
		}
		return map[string][]market.Bar{syms[0]: b}, nil
	}
	return fetchAll(ctx, okx, syms, tf, n)
}

func writeJSON(w io.Writer, v any) error {
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}

func logger(verbose bool) *slog.Logger {
	lvl := slog.LevelInfo
	if verbose {
		lvl = slog.LevelDebug
	}
	return slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: lvl}))
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

func cmdBacktest(args []string) error {
	fs := flag.NewFlagSet("backtest", flag.ExitOnError)
	var ef engineFlags
	ef.register(fs)
	sym := fs.String("symbol", "BTC-USDT", "comma-separated OKX spot instruments")
	ref := fs.String("ref", "BTC-USDT", "reference symbol for cross-asset context (empty to disable)")
	bars := fs.Int("bars", 8000, "history length in base bars")
	csvPath := fs.String("csv", "", "read bars from CSV (ts,open,high,low,close,volume) instead of OKX")
	asJSON := fs.Bool("json", false, "print the JSON report")
	_ = fs.Parse(args)
	cfg, err := ef.config()
	if err != nil {
		return err
	}
	syms := splitSymbols(*sym)
	all := append([]string(nil), syms...)
	if *ref != "" && !contains(all, *ref) && *csvPath == "" {
		all = append(all, *ref)
	}
	ctx := context.Background()
	hist, err := loadOrFetch(ctx, ef.okx(), *csvPath, all, cfg.Base, *bars)
	if err != nil {
		return err
	}
	r := *ref
	if *csvPath != "" {
		r = ""
	}
	rep := backtest.Run(hist, r, cfg)
	if *asJSON {
		return writeJSON(os.Stdout, rep)
	}
	fmt.Print(rep.Text())
	return nil
}

func cmdFetch(args []string) error {
	fs := flag.NewFlagSet("fetch", flag.ExitOnError)
	var ef engineFlags
	ef.register(fs)
	sym := fs.String("symbol", "BTC-USDT", "OKX spot instrument")
	bars := fs.Int("bars", 5000, "number of closed bars")
	out := fs.String("out", "", "output CSV path (stdout when empty)")
	_ = fs.Parse(args)
	tf, err := market.ParseTimeframe(ef.tf)
	if err != nil {
		return err
	}
	b, err := ef.okx().History(context.Background(), strings.ToUpper(*sym), tf, *bars)
	if err != nil {
		return err
	}
	w := io.Writer(os.Stdout)
	if *out != "" {
		f, err := os.Create(*out)
		if err != nil {
			return err
		}
		defer f.Close()
		w = f
	}
	return market.WriteCSV(w, b)
}

func cmdVerify(args []string) error {
	fs := flag.NewFlagSet("verify", flag.ExitOnError)
	path := fs.String("ledger", "data/sentinel-ledger.jsonl", "ledger file")
	_ = fs.Parse(args)
	f, err := os.Open(*path)
	if err != nil {
		return err
	}
	defer f.Close()
	n, head, err := ledger.Verify(f)
	if err != nil {
		return err
	}
	fmt.Printf("ok: %d entries, head %s\n", n, head)
	return nil
}
