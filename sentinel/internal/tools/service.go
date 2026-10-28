package tools

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/backtest"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/candles"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/desk"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/engine"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/feed"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/ledger"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// Service executes tools. In live mode Live is the running desk; symbols it
// does not cover are analysed on demand (fetched, warmed and cached briefly).
type Service struct {
	Live       *desk.Desk
	Config     engine.Config // template for on-demand desks (Symbol/Base overridden)
	OKX        *feed.OKX
	Deribit    *feed.Deribit
	Ref        string // reference symbol, e.g. BTC-USDT
	LedgerPath string
	Universe   []string // default scan universe

	mu    sync.Mutex
	cache map[string]cached
}

type cached struct {
	d  *desk.Desk
	at time.Time
}

const cacheTTL = 2 * time.Minute

var symRe = regexp.MustCompile(`^[A-Z0-9]{2,15}-[A-Z0-9]{2,10}$`)

func normSymbol(s string) (string, error) {
	s = strings.ToUpper(strings.TrimSpace(s))
	if !strings.Contains(s, "-") && len(s) > 4 {
		for _, q := range []string{"USDT", "USDC", "USD"} {
			if strings.HasSuffix(s, q) && len(s) > len(q) {
				s = strings.TrimSuffix(s, q) + "-" + q
				break
			}
		}
	}
	if !symRe.MatchString(s) {
		return "", fmt.Errorf("symbol %q must look like BTC-USDT", s)
	}
	return s, nil
}

func (s *Service) timeframe(tf string) (market.Timeframe, error) {
	if tf == "" {
		return s.Config.Base, nil
	}
	t, err := market.ParseTimeframe(tf)
	if err != nil {
		return t, err
	}
	switch t.Name {
	case "5m", "15m", "30m", "1h", "2h", "4h":
		return t, nil
	}
	return t, fmt.Errorf("timeframe %s not supported for analysis (use 5m..4h)", t.Name)
}

// HistoryBars is enough base bars for the high frame to warm up with margin.
func HistoryBars(base market.Timeframe) int {
	_, high := market.Ladder(base)
	mult := int(high.Dur / base.Dur)
	return min(max(2000, 130*mult), 9000)
}

// deskFor returns a warmed desk containing sym on timeframe tf.
func (s *Service) deskFor(ctx context.Context, syms []string, tf market.Timeframe) (*desk.Desk, error) {
	if s.Live != nil && tf.Name == s.Live.Base.Name {
		all := true
		for _, sym := range syms {
			if _, ok := s.Live.Engine(sym); !ok {
				all = false
			}
		}
		if all {
			return s.Live, nil
		}
	}
	sorted := append([]string(nil), syms...)
	sort.Strings(sorted)
	key := tf.Name + "|" + strings.Join(sorted, ",")
	s.mu.Lock()
	if s.cache == nil {
		s.cache = map[string]cached{}
	}
	if c, ok := s.cache[key]; ok && time.Since(c.at) < cacheTTL {
		s.mu.Unlock()
		return c.d, nil
	}
	s.mu.Unlock()

	all := append([]string(nil), syms...)
	if s.Ref != "" && !contains(all, s.Ref) {
		all = append(all, s.Ref)
	}
	n := HistoryBars(tf)
	hist := map[string][]market.Bar{}
	var mu sync.Mutex
	var wg sync.WaitGroup
	errs := make(chan error, len(all))
	sem := make(chan struct{}, 4)
	for _, sym := range all {
		wg.Add(1)
		go func(sym string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			b, err := s.OKX.History(ctx, sym, tf, n)
			if err != nil {
				errs <- fmt.Errorf("%s: %w", sym, err)
				return
			}
			if len(b) == 0 {
				errs <- fmt.Errorf("%s: no candles (unknown instrument?)", sym)
				return
			}
			mu.Lock()
			hist[sym] = b
			mu.Unlock()
		}(sym)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			// The reference is optional; the requested symbols are not.
			if s.Ref != "" && strings.HasPrefix(err.Error(), s.Ref+":") && !contains(syms, s.Ref) {
				continue
			}
			return nil, err
		}
	}
	cfg := s.Config
	cfg.Base = tf
	ref := s.Ref
	if _, ok := hist[ref]; !ok {
		ref = ""
	}
	symsIn := make([]string, 0, len(hist))
	for k := range hist {
		symsIn = append(symsIn, k)
	}
	sort.Strings(symsIn)
	d := desk.New(symsIn, ref, cfg, nil)
	d.Warmup(hist)
	s.loadContext(ctx, d, syms)
	s.mu.Lock()
	s.cache[key] = cached{d: d, at: time.Now()}
	s.mu.Unlock()
	return d, nil
}

func (s *Service) loadContext(ctx context.Context, d *desk.Desk, syms []string) {
	var wg sync.WaitGroup
	for _, sym := range syms {
		e, ok := d.Engine(sym)
		if !ok {
			continue
		}
		wg.Add(1)
		go func(sym string, e *engine.Engine) {
			defer wg.Done()
			cctx, cancel := context.WithTimeout(ctx, 20*time.Second)
			defer cancel()
			if s.OKX != nil {
				if dv, err := s.OKX.Derivatives(cctx, sym, 4); err == nil {
					e.SetDerivatives(dv)
				}
			}
			if s.Deribit != nil {
				if cur, ok := s.Deribit.Supports(sym); ok {
					if o, err := s.Deribit.Options(cctx, cur); err == nil {
						e.SetOptions(o)
					}
				}
			}
			e.Refresh()
		}(sym, e)
	}
	wg.Wait()
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

// Snapshot returns the latest snapshot for a symbol.
func (s *Service) Snapshot(ctx context.Context, symbol, tf string) (engine.Snapshot, error) {
	sym, err := normSymbol(symbol)
	if err != nil {
		return engine.Snapshot{}, err
	}
	t, err := s.timeframe(tf)
	if err != nil {
		return engine.Snapshot{}, err
	}
	d, err := s.deskFor(ctx, []string{sym}, t)
	if err != nil {
		return engine.Snapshot{}, err
	}
	e, ok := d.Engine(sym)
	if !ok {
		return engine.Snapshot{}, fmt.Errorf("%s not available", sym)
	}
	return e.Last(), nil
}

// ScanRow is one ranked line of a scan.
type ScanRow struct {
	Symbol     string  `json:"symbol"`
	Price      float64 `json:"price"`
	Score      float64 `json:"sentinel_score"`
	Action     string  `json:"action"`
	Confidence float64 `json:"confidence"`
	Tier       string  `json:"tier"`
	EdgeStatus string  `json:"edge_status"`
	Regime     string  `json:"regime"`
	VolState   string  `json:"vol_state"`
	SigmaPct   float64 `json:"sigma_horizon_pct"`
	Summary    string  `json:"summary"`
}

// Scan analyses symbols together (pooled model, shared BTC context) and ranks
// them by absolute Sentinel score.
func (s *Service) Scan(ctx context.Context, symbols []string, tf string) ([]ScanRow, error) {
	if len(symbols) == 0 {
		symbols = s.Universe
	}
	if len(symbols) == 0 && s.Live != nil {
		symbols = s.Live.Symbols()
	}
	if len(symbols) == 0 {
		return nil, errors.New("no symbols to scan")
	}
	if len(symbols) > 25 {
		return nil, errors.New("scan at most 25 symbols at a time")
	}
	var syms []string
	for _, x := range symbols {
		n, err := normSymbol(x)
		if err != nil {
			return nil, err
		}
		if !contains(syms, n) {
			syms = append(syms, n)
		}
	}
	t, err := s.timeframe(tf)
	if err != nil {
		return nil, err
	}
	d, err := s.deskFor(ctx, syms, t)
	if err != nil {
		return nil, err
	}
	var rows []ScanRow
	for _, sym := range syms {
		e, ok := d.Engine(sym)
		if !ok {
			continue
		}
		sn := e.Last()
		rows = append(rows, ScanRow{Symbol: sym, Price: sn.Price, Score: sn.Score, Action: sn.Signal.Action, Confidence: sn.Signal.Confidence,
			Tier: sn.Signal.Tier, EdgeStatus: sn.Signal.EdgeStatus, Regime: sn.Regime.Kind, VolState: sn.Forecast.State,
			SigmaPct: r4(sn.Forecast.SigmaHorizon * 100), Summary: sn.Signal.Summary})
	}
	sort.SliceStable(rows, func(i, j int) bool { return math.Abs(rows[i].Score) > math.Abs(rows[j].Score) })
	return rows, nil
}

// ModelReport returns the direction models' gate status and full prequential metrics.
func (s *Service) ModelReport(ctx context.Context, symbol, tf string) (map[string]any, error) {
	sym, err := normSymbol(symbol)
	if err != nil {
		return nil, err
	}
	t, err := s.timeframe(tf)
	if err != nil {
		return nil, err
	}
	d, err := s.deskFor(ctx, []string{sym}, t)
	if err != nil {
		return nil, err
	}
	e, _ := d.Engine(sym)
	cfg := e.Config()
	long, short := e.Stats()
	lh, le, lpf := long.Summary()
	sh, se, spf := short.Summary()
	return map[string]any{
		"note": "Every metric is prequential (out-of-sample). The direction model only influences signals while its recent window " +
			"beats the base rate; otherwise it abstains. Signal stats are net of fees on fired signals during warm-up replay and live.",
		"long_status":    d.Models.Long.Status(cfg.WarmSamples),
		"short_status":   d.Models.Short.Status(cfg.WarmSamples),
		"long_metrics":   d.Models.Long.Report(),
		"short_metrics":  d.Models.Short.Report(),
		"volatility":     e.Forecast(),
		"signals_long":   map[string]any{"n": long.Count, "hit_rate": lh, "expectancy_r": le, "profit_factor": finite(lpf)},
		"signals_short":  map[string]any{"n": short.Count, "hit_rate": sh, "expectancy_r": se, "profit_factor": finite(spf)},
		"barriers":       map[string]any{"stop_atr": cfg.StopATR, "target_r": cfg.TargetR, "horizon_bars": cfg.Horizon},
		"costs_pct":      cfg.FeeEntryPct + cfg.FeeExitPct + cfg.SlippagePct,
		"pooled_symbols": d.Symbols(),
	}, nil
}

func finite(x float64) any {
	if math.IsInf(x, 0) || math.IsNaN(x) {
		return nil
	}
	return x
}

// Backtest runs a walk-forward evaluation.
func (s *Service) Backtest(ctx context.Context, symbol, tf string, bars int) (backtest.Report, error) {
	sym, err := normSymbol(symbol)
	if err != nil {
		return backtest.Report{}, err
	}
	t, err := s.timeframe(tf)
	if err != nil {
		return backtest.Report{}, err
	}
	if bars <= 0 {
		bars = 6000
	}
	bars = min(bars, 20000)
	hist := map[string][]market.Bar{}
	b, err := s.OKX.History(ctx, sym, t, bars)
	if err != nil {
		return backtest.Report{}, err
	}
	hist[sym] = b
	ref := ""
	if s.Ref != "" && s.Ref != sym {
		if rb, err := s.OKX.History(ctx, s.Ref, t, bars); err == nil {
			hist[s.Ref] = rb
			ref = s.Ref
		}
	}
	cfg := s.Config
	cfg.Base = t
	return backtest.Run(hist, ref, cfg), nil
}

// IndicatorSeries computes one indicator over recent bars (the general-purpose
// calculator that crypto-indicators-mcp exposes, served from Sentinel's
// TA-Lib-verified streaming implementations).
func (s *Service) IndicatorSeries(ctx context.Context, symbol, tf, name string, period, points int) (map[string]any, error) {
	sym, err := normSymbol(symbol)
	if err != nil {
		return nil, err
	}
	t, err := market.ParseTimeframe(defaultStr(tf, s.Config.Base.Name))
	if err != nil {
		return nil, err
	}
	if points <= 0 {
		points = 20
	}
	points = min(points, 500)
	bars, err := s.OKX.History(ctx, sym, t, max(600, points+400))
	if err != nil {
		return nil, err
	}
	series, err := Compute(name, period, bars)
	if err != nil {
		return nil, err
	}
	out := map[string]any{"symbol": sym, "timeframe": t.Name, "indicator": strings.ToLower(name)}
	for k, v := range series {
		if len(v.Values) > points {
			v.Values = v.Values[len(v.Values)-points:]
			v.Times = v.Times[len(v.Times)-points:]
		}
		series[k] = v
	}
	out["series"] = series
	return out, nil
}

func defaultStr(s, d string) string {
	if s == "" {
		return d
	}
	return s
}

// PatternHit is a candlestick pattern found on a bar.
type PatternHit struct {
	Time    time.Time       `json:"time"`
	Close   float64         `json:"close"`
	Pattern candles.Pattern `json:"pattern"`
}

// Patterns lists candlestick patterns over the last lookback bars.
func (s *Service) Patterns(ctx context.Context, symbol, tf string, lookback int) ([]PatternHit, error) {
	sym, err := normSymbol(symbol)
	if err != nil {
		return nil, err
	}
	t, err := market.ParseTimeframe(defaultStr(tf, s.Config.Base.Name))
	if err != nil {
		return nil, err
	}
	if lookback <= 0 {
		lookback = 50
	}
	lookback = min(lookback, 500)
	bars, err := s.OKX.History(ctx, sym, t, lookback+200)
	if err != nil {
		return nil, err
	}
	return ScanPatterns(bars, lookback), nil
}

// LedgerTail returns the last n ledger entries and the chain verification result.
func (s *Service) LedgerTail(n int) (map[string]any, error) {
	if s.LedgerPath == "" {
		return map[string]any{"enabled": false, "note": "the ledger records live signals only; start `sentinel serve` with -ledger"}, nil
	}
	entries, err := ledger.ReadAll(s.LedgerPath)
	if err != nil {
		return map[string]any{"enabled": true, "entries": 0, "note": err.Error()}, nil
	}
	if n <= 0 {
		n = 20
	}
	verified := "ok"
	if f, err := openRead(s.LedgerPath); err == nil {
		if _, _, verr := ledger.Verify(f); verr != nil {
			verified = verr.Error()
		}
		f.Close()
	}
	tail := entries
	if len(tail) > n {
		tail = tail[len(tail)-n:]
	}
	var sumR float64
	var outcomes, wins int
	for _, e := range entries {
		if e.Kind != "outcome" {
			continue
		}
		var o engine.Outcome
		if json.Unmarshal(e.Payload, &o) == nil {
			outcomes++
			sumR += o.R
			if o.Result == 1 {
				wins++
			}
		}
	}
	summary := map[string]any{"resolved_signals": outcomes}
	if outcomes > 0 {
		summary["hit_rate"] = float64(wins) / float64(outcomes)
		summary["expectancy_r"] = sumR / float64(outcomes)
	}
	return map[string]any{"enabled": true, "entries": len(entries), "verification": verified, "live_record": summary, "tail": tail}, nil
}
