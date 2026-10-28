package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
)

// Tool is a named, schema-described operation.
type Tool struct {
	Name        string
	Description string
	Schema      map[string]any // JSON Schema for the input object
	Run         func(ctx context.Context, in json.RawMessage) (any, error)
}

func obj(props map[string]any, required ...string) map[string]any {
	if required == nil {
		required = []string{}
	}
	return map[string]any{"type": "object", "properties": props, "required": required, "additionalProperties": false}
}

func str(desc string) map[string]any { return map[string]any{"type": "string", "description": desc} }
func integer(desc string) map[string]any {
	return map[string]any{"type": "integer", "description": desc}
}

var tfDesc = "Base timeframe: 5m, 15m, 30m, 1h, 2h or 4h (mid and high frames are derived). Defaults to the service timeframe."

func decode[T any](in json.RawMessage) (T, error) {
	var v T
	if len(in) == 0 || string(in) == "null" {
		return v, nil
	}
	dec := json.NewDecoder(strings.NewReader(string(in)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&v); err != nil {
		return v, fmt.Errorf("invalid input: %w", err)
	}
	return v, nil
}

// Tools returns the tool set bound to the service.
func (s *Service) Tools() []Tool {
	type symTF struct {
		Symbol    string `json:"symbol"`
		Timeframe string `json:"timeframe"`
	}
	ts := []Tool{
		{
			Name: "analyze_symbol",
			Description: "Full Sentinel analysis of one crypto spot pair (OKX instrument, e.g. BTC-USDT) on the latest closed bar: " +
				"Sentinel score (-100..100), regime, layered long/short confluence with the strongest votes, the signal decision with every " +
				"gate check and trade plan, the volatility forecast with calibrated price ranges, the skill-gated direction model, " +
				"per-timeframe readings, perp positioning and (BTC/ETH) options sentiment. All numbers are deterministic engine outputs.",
			Schema: obj(map[string]any{"symbol": str("OKX spot instrument such as BTC-USDT or ETH-USDT"), "timeframe": str(tfDesc)}, "symbol"),
			Run: func(ctx context.Context, in json.RawMessage) (any, error) {
				a, err := decode[symTF](in)
				if err != nil {
					return nil, err
				}
				snap, err := s.Snapshot(ctx, a.Symbol, a.Timeframe)
				if err != nil {
					return nil, err
				}
				return MakeBrief(snap), nil
			},
		},
		{
			Name:        "scan_market",
			Description: "Analyse several pairs together (pooled model, shared BTC context) and rank them by absolute Sentinel score. Returns one row per symbol with action, confidence tier, edge status, regime and volatility state.",
			Schema: obj(map[string]any{
				"symbols":   map[string]any{"type": "array", "items": map[string]any{"type": "string"}, "description": "Up to 25 OKX spot pairs; omit for the default universe"},
				"timeframe": str(tfDesc),
			}),
			Run: func(ctx context.Context, in json.RawMessage) (any, error) {
				a, err := decode[struct {
					Symbols   []string `json:"symbols"`
					Timeframe string   `json:"timeframe"`
				}](in)
				if err != nil {
					return nil, err
				}
				return s.Scan(ctx, a.Symbols, a.Timeframe)
			},
		},
		{
			Name:        "get_readings",
			Description: "Every raw indicator reading (RSI, MACD, ADX/DI, Bollinger/Keltner squeeze, StochRSI, MFI, CMF, OBV slope, VWAP deviation, Supertrend, Donchian, efficiency ratio, Hull/KAMA slopes, swing structure, divergence, candle pattern, support/resistance) for one frame: base, mid or high.",
			Schema: obj(map[string]any{"symbol": str("OKX spot pair"), "timeframe": str(tfDesc),
				"frame": map[string]any{"type": "string", "enum": []string{"base", "mid", "high"}, "description": "which frame of the ladder"}}, "symbol", "frame"),
			Run: func(ctx context.Context, in json.RawMessage) (any, error) {
				a, err := decode[struct {
					Symbol    string `json:"symbol"`
					Timeframe string `json:"timeframe"`
					Frame     string `json:"frame"`
				}](in)
				if err != nil {
					return nil, err
				}
				snap, err := s.Snapshot(ctx, a.Symbol, a.Timeframe)
				if err != nil {
					return nil, err
				}
				switch a.Frame {
				case "base":
					return snap.Base, nil
				case "mid":
					return snap.Mid, nil
				case "high":
					return snap.High, nil
				}
				return nil, fmt.Errorf("frame must be base, mid or high")
			},
		},
		{
			Name:        "volatility_forecast",
			Description: "HAR-RV volatility forecast for the label horizon: expected sigma, expansion vs trailing volatility, empirically calibrated 68% and 90% price ranges, and the forecaster's out-of-sample R^2 against naive persistence plus realised band coverage.",
			Schema:      obj(map[string]any{"symbol": str("OKX spot pair"), "timeframe": str(tfDesc)}, "symbol"),
			Run: func(ctx context.Context, in json.RawMessage) (any, error) {
				a, err := decode[symTF](in)
				if err != nil {
					return nil, err
				}
				snap, err := s.Snapshot(ctx, a.Symbol, a.Timeframe)
				if err != nil {
					return nil, err
				}
				return map[string]any{"symbol": snap.Symbol, "price": snap.Price, "bar_time": snap.Time, "forecast": MakeBrief(snap).Forecast}, nil
			},
		},
		{
			Name:        "model_report",
			Description: "Honesty report for a symbol: the direction models' skill-gate status, prequential AUC / Brier skill / reliability table, volatility forecast skill, and realised net-of-fee statistics of fired signals.",
			Schema:      obj(map[string]any{"symbol": str("OKX spot pair"), "timeframe": str(tfDesc)}, "symbol"),
			Run: func(ctx context.Context, in json.RawMessage) (any, error) {
				a, err := decode[symTF](in)
				if err != nil {
					return nil, err
				}
				return s.ModelReport(ctx, a.Symbol, a.Timeframe)
			},
		},
		{
			Name:        "run_backtest",
			Description: "Walk-forward (prequential, out-of-sample) backtest on OKX history with fees and slippage: model skill, signal expectancy with bootstrap CI versus entering on every bar, by regime and tier, plus a verdict. Slow for large bar counts.",
			Schema: obj(map[string]any{"symbol": str("OKX spot pair"), "timeframe": str(tfDesc),
				"bars": integer("history length in base bars, default 6000, max 20000")}, "symbol"),
			Run: func(ctx context.Context, in json.RawMessage) (any, error) {
				a, err := decode[struct {
					Symbol    string `json:"symbol"`
					Timeframe string `json:"timeframe"`
					Bars      int    `json:"bars"`
				}](in)
				if err != nil {
					return nil, err
				}
				r, err := s.Backtest(ctx, a.Symbol, a.Timeframe, a.Bars)
				if err != nil {
					return nil, err
				}
				return map[string]any{"verdict": r.Verdict, "volatility": r.Volatility, "long": r.Long, "short": r.Short,
					"from": r.From, "to": r.To, "bars": r.Bars}, nil
			},
		},
		{
			Name:        "compute_indicator",
			Description: "Compute one technical indicator series on recent OKX candles with TA-Lib-verified formulas. Indicators: " + strings.Join(IndicatorNames, ", ") + ".",
			Schema: obj(map[string]any{"symbol": str("OKX spot pair"), "timeframe": str("candle timeframe, e.g. 15m, 1h, 4h, 1d"),
				"indicator": map[string]any{"type": "string", "enum": IndicatorNames},
				"period":    integer("lookback period; omit for the conventional default"),
				"points":    integer("number of most recent values to return, default 20, max 500")}, "symbol", "indicator"),
			Run: func(ctx context.Context, in json.RawMessage) (any, error) {
				a, err := decode[struct {
					Symbol    string `json:"symbol"`
					Timeframe string `json:"timeframe"`
					Indicator string `json:"indicator"`
					Period    int    `json:"period"`
					Points    int    `json:"points"`
				}](in)
				if err != nil {
					return nil, err
				}
				return s.IndicatorSeries(ctx, a.Symbol, a.Timeframe, a.Indicator, a.Period, a.Points)
			},
		},
		{
			Name:        "candlestick_patterns",
			Description: "Candlestick patterns (33 single/double/triple-bar formations, ATR-normalised, trend-filtered) detected over recent bars, newest first.",
			Schema: obj(map[string]any{"symbol": str("OKX spot pair"), "timeframe": str("candle timeframe"),
				"lookback": integer("bars to scan, default 50, max 500")}, "symbol"),
			Run: func(ctx context.Context, in json.RawMessage) (any, error) {
				a, err := decode[struct {
					Symbol    string `json:"symbol"`
					Timeframe string `json:"timeframe"`
					Lookback  int    `json:"lookback"`
				}](in)
				if err != nil {
					return nil, err
				}
				return s.Patterns(ctx, a.Symbol, a.Timeframe, a.Lookback)
			},
		},
		{
			Name:        "derivatives_positioning",
			Description: "Live OKX perpetual-swap positioning for a spot pair's base asset: funding rate (current and recent average), open-interest change vs price change over 4h, account long/short ratio and perp basis. Data-only; nothing is traded.",
			Schema:      obj(map[string]any{"symbol": str("OKX spot pair")}, "symbol"),
			Run: func(ctx context.Context, in json.RawMessage) (any, error) {
				a, err := decode[symTF](in)
				if err != nil {
					return nil, err
				}
				sym, err := normSymbol(a.Symbol)
				if err != nil {
					return nil, err
				}
				return s.OKX.Derivatives(ctx, sym, 4)
			},
		},
		{
			Name:        "options_sentiment",
			Description: "Deribit options sentiment for BTC or ETH: put/call ratios by open interest and volume, DVOL and its 24h change, and an OTM call-minus-put implied-volatility skew near 30 days.",
			Schema:      obj(map[string]any{"currency": map[string]any{"type": "string", "enum": []string{"BTC", "ETH"}}}, "currency"),
			Run: func(ctx context.Context, in json.RawMessage) (any, error) {
				a, err := decode[struct {
					Currency string `json:"currency"`
				}](in)
				if err != nil {
					return nil, err
				}
				c := strings.ToUpper(a.Currency)
				if c != "BTC" && c != "ETH" {
					return nil, fmt.Errorf("currency must be BTC or ETH")
				}
				return s.Deribit.Options(ctx, c)
			},
		},
		{
			Name:        "signal_ledger",
			Description: "The hash-chained ledger of live signals and their resolved outcomes: chain verification result, live hit rate and expectancy, and the most recent entries.",
			Schema:      obj(map[string]any{"limit": integer("number of recent entries, default 20")}),
			Run: func(ctx context.Context, in json.RawMessage) (any, error) {
				a, err := decode[struct {
					Limit int `json:"limit"`
				}](in)
				if err != nil {
					return nil, err
				}
				return s.LedgerTail(a.Limit)
			},
		},
	}
	return ts
}

// Find returns the named tool.
func Find(ts []Tool, name string) (Tool, bool) {
	for _, t := range ts {
		if t.Name == name {
			return t, true
		}
	}
	return Tool{}, false
}
