package engine

import (
	"math"
)

// Context is cross-asset information supplied by the coordinator: the
// reference asset (BTC) state at the same bar.
type Context struct {
	Available   bool    `json:"available"`
	RefSymbol   string  `json:"ref_symbol,omitempty"`
	RelStrength float64 `json:"rel_strength"` // symbol ROC minus reference ROC over the mid frame, percent
	RefTrend    int     `json:"ref_trend"`    // reference high-frame trend: +1, -1, 0
	RefShock    int     `json:"ref_shock"`    // +1/-1 when the reference just moved > 3 sigma, else 0
	RefReturnZ  float64 `json:"ref_return_z"`
}

// featureNames lists the directional model's inputs: a deliberately compact,
// economically motivated set (trend, momentum, mean-reversion stretch and
// volatility regime on each frame). Walk-forward tests showed that the full
// ~95-reading vector overfits: correlated indicators carry little stable
// information about the next move, and a smaller set degrades more gracefully.
// Every feature is bounded to roughly [-1, 1] and is computed identically in
// backtests and live; derivatives and options data are excluded because there
// is no point-in-time history to train on.
var featureNames = []string{
	"base.rsi", "base.macd_hist", "base.vol_pct", "base.dist_ema50", "base.zscore", "base.rel_vol", "base.vwap_dev", "base.streak",
	"mid.rsi", "mid.adx_dir", "mid.er", "mid.macd_hist", "mid.dist_ema50", "mid.vol_pct", "mid.percent_b", "mid.structure",
	"mid.divergence", "mid.squeeze",
	"high.supertrend", "high.dist_ema50", "high.rsi", "high.adx_dir", "high.structure", "high.stoch_k",
	"regime.trend_up", "regime.trend_down", "regime.range", "regime.volatile",
	"ctx.rel_strength", "ctx.ref_trend", "ctx.ref_shock",
	"vol.expansion",
	"x.high_trend*mid_macd", "x.range*mid_percent_b",
}

// FeatureNames returns the model input names in order.
func FeatureNames() []string { return append([]string(nil), featureNames...) }

func squash(x, scale float64) float64 { return math.Tanh(x / scale) }

func clamp(x, lo, hi float64) float64 { return math.Max(lo, math.Min(hi, x)) }

func b2f(b bool) float64 {
	if b {
		return 1
	}
	return 0
}

func adxDir(r Reading) float64 {
	if r.PlusDI+r.MinusDI <= 0 {
		return 0
	}
	return clamp((r.ADX/50)*(r.PlusDI-r.MinusDI)/(r.PlusDI+r.MinusDI), -1, 1)
}

func macdNorm(r Reading) float64 {
	if r.ATR <= 0 {
		return 0
	}
	return squash(r.MACDHist/r.ATR, 0.3)
}

func pctB(r Reading) float64 { return clamp(2*r.PercentB-1, -1.5, 1.5) / 1.5 }

// buildFeatures assembles the model input vector.
func buildFeatures(base, mid, high Reading, rg Regime, ctx Context, volExpansion float64) []float64 {
	x := []float64{
		(base.RSI - 50) / 50, macdNorm(base), 2*base.VolPercentile - 1, squash(base.DistEMA50, 3), squash(base.ZScore, 2),
		squash(math.Log(math.Max(base.RelVol, 1e-3)), 1), squash(base.VWAPDev, 2), squash(float64(base.Streak), 4),
		(mid.RSI - 50) / 50, adxDir(mid), 2*mid.ER - 1, macdNorm(mid), squash(mid.DistEMA50, 3), 2*mid.VolPercentile - 1,
		pctB(mid), float64(mid.Structure), float64(mid.Divergence), b2f(mid.Squeeze),
		float64(high.SupertrendDir), squash(high.DistEMA50, 3), (high.RSI - 50) / 50, adxDir(high), float64(high.Structure),
		(high.StochK - 50) / 50,
		b2f(rg.Kind == RegimeTrendUp), b2f(rg.Kind == RegimeTrendDown), b2f(rg.Kind == RegimeRange), b2f(rg.Kind == RegimeVolatile),
	}
	if ctx.Available {
		x = append(x, squash(ctx.RelStrength, 3), float64(ctx.RefTrend), float64(ctx.RefShock))
	} else {
		x = append(x, 0, 0, 0)
	}
	x = append(x, squash(math.Log(math.Max(volExpansion, 1e-3)), 0.5))
	x = append(x,
		float64(high.SupertrendDir)*macdNorm(mid),
		b2f(rg.Kind == RegimeRange)*pctB(mid),
	)
	if len(x) != len(featureNames) {
		panic("feature vector length mismatch")
	}
	return x
}

// Regime kinds.
const (
	RegimeTrendUp   = "trend_up"
	RegimeTrendDown = "trend_down"
	RegimeRange     = "range"
	RegimeVolatile  = "volatile"
	RegimeUnknown   = "unknown"
)

// Regime describes the market state on the mid frame, confirmed by the high frame.
type Regime struct {
	Kind        string  `json:"kind"`
	Strength    float64 `json:"strength"` // 0..1
	ADX         float64 `json:"adx"`
	ER          float64 `json:"efficiency_ratio"`
	VolPct      float64 `json:"vol_percentile"`
	HighTrend   int     `json:"high_trend"`
	Description string  `json:"description"`
}

// classifyRegime: trend when ADX and the efficiency ratio agree that price is
// moving directionally; volatile when realised volatility is in its top 15%
// without direction; range otherwise.
func classifyRegime(mid, high Reading) Regime {
	rg := Regime{Kind: RegimeUnknown, ADX: mid.ADX, ER: mid.ER, VolPct: mid.VolPercentile}
	if !mid.Ready {
		rg.Description = "warming up"
		return rg
	}
	switch {
	case high.Ready && high.Close > high.EMA50 && high.SupertrendDir > 0:
		rg.HighTrend = 1
	case high.Ready && high.Close < high.EMA50 && high.SupertrendDir < 0:
		rg.HighTrend = -1
	}
	trending := mid.ADX >= 22 && mid.ER >= 0.25
	switch {
	case trending && mid.PlusDI > mid.MinusDI && mid.Close > mid.EMA50:
		rg.Kind = RegimeTrendUp
		rg.Description = "directional uptrend (ADX and efficiency ratio agree)"
	case trending && mid.MinusDI > mid.PlusDI && mid.Close < mid.EMA50:
		rg.Kind = RegimeTrendDown
		rg.Description = "directional downtrend (ADX and efficiency ratio agree)"
	case mid.VolPercentile >= 0.85:
		rg.Kind = RegimeVolatile
		rg.Description = "high realised volatility without direction"
	default:
		rg.Kind = RegimeRange
		rg.Description = "range-bound: weak trend strength"
	}
	rg.Strength = clamp((mid.ADX-15)/30*0.5+mid.ER*0.5, 0, 1)
	if rg.Kind == RegimeVolatile {
		rg.Strength = mid.VolPercentile
	}
	return rg
}
