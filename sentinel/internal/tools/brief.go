// Package tools is Sentinel's tool surface: one set of typed tools served to
// the Claude analyst agent, the MCP server and the HTTP API alike.
package tools

import (
	"math"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/engine"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// FrameBrief is the compact view of one timeframe.
type FrameBrief struct {
	TF            string  `json:"tf"`
	RSI           float64 `json:"rsi"`
	ADX           float64 `json:"adx"`
	DIBias        string  `json:"di_bias"`
	MACDHist      float64 `json:"macd_hist"`
	Supertrend    string  `json:"supertrend"`
	DistEMA50ATR  float64 `json:"dist_ema50_atr"`
	DistEMA200ATR float64 `json:"dist_ema200_atr,omitempty"`
	ATRPct        float64 `json:"atr_pct"`
	VolPercentile float64 `json:"vol_percentile"`
	PercentB      float64 `json:"bb_percent_b"`
	Squeeze       bool    `json:"squeeze"`
	StochRSIK     float64 `json:"stochrsi_k"`
	Structure     string  `json:"structure"`
	Divergence    string  `json:"divergence,omitempty"`
	Pattern       string  `json:"pattern,omitempty"`
	Support       float64 `json:"support,omitempty"`
	Resistance    float64 `json:"resistance,omitempty"`
	RelVolume     float64 `json:"rel_volume"`
	CMF           float64 `json:"cmf"`
}

// Brief is a token-efficient snapshot for agents (every number is final and
// computed deterministically by the engine).
type Brief struct {
	Symbol     string              `json:"symbol"`
	TF         string              `json:"tf"`
	BarTime    time.Time           `json:"bar_time"`
	Price      float64             `json:"price"`
	Ready      bool                `json:"ready"`
	Warmup     string              `json:"warmup,omitempty"`
	Score      float64             `json:"sentinel_score"`
	Regime     engine.Regime       `json:"regime"`
	Signal     SignalBrief         `json:"signal"`
	Forecast   ForecastBrief       `json:"volatility_forecast"`
	Model      ModelBrief          `json:"direction_model"`
	Confluence ConfluenceBrief     `json:"confluence"`
	Frames     []FrameBrief        `json:"frames"`
	Context    *engine.Context     `json:"cross_asset,omitempty"`
	Deriv      *market.Derivatives `json:"derivatives,omitempty"`
	Options    *market.Options     `json:"options,omitempty"`
}

// SignalBrief is the decision.
type SignalBrief struct {
	Action     string             `json:"action"`
	Actionable bool               `json:"actionable"`
	Route      string             `json:"route,omitempty"`
	Confidence float64            `json:"confidence"`
	Tier       string             `json:"tier"`
	EdgeStatus string             `json:"edge_status"`
	Track      engine.TrackRecord `json:"track_record"`
	FailedGate []string           `json:"failed_gate,omitempty"`
	Gate       []engine.GateCheck `json:"gate"`
	Plan       *engine.TradePlan  `json:"plan,omitempty"`
	Suppressed string             `json:"suppressed,omitempty"`
	Summary    string             `json:"summary"`
}

// ForecastBrief is the volatility outlook.
type ForecastBrief struct {
	Ready          bool       `json:"ready"`
	Trusted        bool       `json:"trusted"`
	HorizonBars    int        `json:"horizon_bars"`
	SigmaHorizonPc float64    `json:"sigma_horizon_pct"`
	State          string     `json:"state"`
	Expansion      float64    `json:"expansion"`
	Range68        [2]float64 `json:"range68"`
	Range90        [2]float64 `json:"range90"`
	R2             float64    `json:"r2_oos"`
	R2Naive        float64    `json:"r2_naive"`
	Coverage68     float64    `json:"coverage68"`
	Coverage90     float64    `json:"coverage90"`
}

// ModelBrief is the direction model's skill-gated output.
type ModelBrief struct {
	LongActive  bool    `json:"long_active"`
	ShortActive bool    `json:"short_active"`
	LongReason  string  `json:"long_reason"`
	ShortReason string  `json:"short_reason"`
	LongAUC     float64 `json:"long_recent_auc"`
	ShortAUC    float64 `json:"short_recent_auc"`
	PLong       float64 `json:"p_long"`
	PShort      float64 `json:"p_short"`
	EdgeLongR   float64 `json:"edge_long_r"`
	EdgeShortR  float64 `json:"edge_short_r"`
	CostR       float64 `json:"cost_r"`
	Samples     int     `json:"samples"`
}

// ConfluenceBrief is the layered tally with the strongest votes per side.
type ConfluenceBrief struct {
	Long        float64             `json:"long"`
	Short       float64             `json:"short"`
	LayersLong  int                 `json:"layers_long"`
	LayersShort int                 `json:"layers_short"`
	Layers      []engine.LayerScore `json:"layers"`
	TopLong     []engine.Vote       `json:"top_long"`
	TopShort    []engine.Vote       `json:"top_short"`
}

func r2(x float64) float64 { return math.Round(x*100) / 100 }
func r4(x float64) float64 { return math.Round(x*10000) / 10000 }

func word(d int) string {
	switch {
	case d > 0:
		return "bullish"
	case d < 0:
		return "bearish"
	}
	return "neutral"
}

func structure(s int) string {
	switch s {
	case 1:
		return "HH/HL"
	case -1:
		return "LH/LL"
	}
	return "mixed"
}

func frame(r engine.Reading) FrameBrief {
	f := FrameBrief{TF: r.TF, RSI: r2(r.RSI), ADX: r2(r.ADX), MACDHist: r4(r.MACDHist), Supertrend: word(r.SupertrendDir),
		DistEMA50ATR: r2(r.DistEMA50), ATRPct: r4(r.ATRPct), VolPercentile: r2(r.VolPercentile), PercentB: r2(r.PercentB),
		Squeeze: r.Squeeze, StochRSIK: r2(r.StochK), Structure: structure(r.Structure), Support: r.Support, Resistance: r.Resistance,
		RelVolume: r2(r.RelVol), CMF: r2(r.CMF)}
	if r.LongReady {
		f.DistEMA200ATR = r2(r.DistEMA200)
	}
	switch {
	case r.PlusDI > r.MinusDI:
		f.DIBias = "bullish"
	case r.MinusDI > r.PlusDI:
		f.DIBias = "bearish"
	default:
		f.DIBias = "neutral"
	}
	if r.Divergence != 0 {
		f.Divergence = word(r.Divergence)
	}
	if r.Pattern != nil {
		f.Pattern = r.Pattern.Name
	}
	return f
}

// MakeBrief condenses a snapshot.
func MakeBrief(s engine.Snapshot) Brief {
	b := Brief{Symbol: s.Symbol, TF: s.TF, BarTime: s.Time, Price: s.Price, Ready: s.Ready, Warmup: s.Warmup, Score: s.Score, Regime: s.Regime,
		Deriv: s.Derivatives, Options: s.Options}
	sig := s.Signal
	b.Signal = SignalBrief{Action: sig.Action, Actionable: sig.Actionable, Route: sig.Route, Confidence: sig.Confidence, Tier: sig.Tier,
		EdgeStatus: sig.EdgeStatus, Track: sig.Track, Gate: sig.Gate, Plan: sig.Plan, Suppressed: sig.Suppressed, Summary: sig.Summary}
	for _, g := range sig.Gate {
		if !g.Pass {
			b.Signal.FailedGate = append(b.Signal.FailedGate, g.Name)
		}
	}
	f := s.Forecast
	b.Forecast = ForecastBrief{Ready: f.Ready, Trusted: f.Trusted, HorizonBars: f.Horizon, SigmaHorizonPc: r4(f.SigmaHorizon * 100),
		State: f.State, Expansion: r2(f.Expansion), Range68: f.Range68, Range90: f.Range90, R2: r4(f.R2), R2Naive: r4(f.R2Naive),
		Coverage68: r4(f.Coverage68), Coverage90: r4(f.Coverage90)}
	m := s.Model
	b.Model = ModelBrief{LongActive: m.Long.Active, ShortActive: m.Short.Active, LongReason: m.Long.Reason, ShortReason: m.Short.Reason,
		LongAUC: r4(m.Long.RecentAUC), ShortAUC: r4(m.Short.RecentAUC), PLong: r4(m.PLong), PShort: r4(m.PShort),
		EdgeLongR: r4(m.EdgeLongR), EdgeShortR: r4(m.EdgeShortR), CostR: r4(m.CostR), Samples: m.Samples}
	c := s.Confluence
	b.Confluence = ConfluenceBrief{Long: r2(c.Long), Short: r2(c.Short), LayersLong: c.LayersLong, LayersShort: c.LayersShort, Layers: c.Layers}
	for _, v := range c.Votes {
		if v.Side > 0 && len(b.Confluence.TopLong) < 6 {
			b.Confluence.TopLong = append(b.Confluence.TopLong, v)
		}
		if v.Side < 0 && len(b.Confluence.TopShort) < 6 {
			b.Confluence.TopShort = append(b.Confluence.TopShort, v)
		}
	}
	if s.Ready {
		b.Frames = []FrameBrief{frame(s.Base), frame(s.Mid), frame(s.High)}
	}
	if s.Context.Available {
		ctx := s.Context
		b.Context = &ctx
	}
	return b
}
