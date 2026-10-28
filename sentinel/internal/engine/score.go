package engine

import (
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// Layers of the confluence model. The structure follows the Aminoid Alpha
// design (independent long/short tallies across separate analytical lenses),
// extended with an options-sentiment layer and a cross-asset context layer.
const (
	LayerTrend     = "trend"          // high frame: where is the tide going
	LayerReversal  = "reversal"       // mid frame: exhaustion and turning points
	LayerMomentum  = "momentum"       // mid frame: is there force behind the move
	LayerTiming    = "timing"         // base frame: entry timing
	LayerMicro     = "microstructure" // perp funding / open interest / basis
	LayerSentiment = "sentiment"      // options: put/call ratio, DVOL, skew
	LayerContext   = "context"        // cross-asset: relative strength and BTC state
)

// Layers lists every layer in display order.
var Layers = []string{LayerTrend, LayerReversal, LayerMomentum, LayerTiming, LayerMicro, LayerSentiment, LayerContext}

// layerCap bounds how much a single layer can add to a side, so stacking many
// correlated indicators in one layer cannot fake multi-factor confluence.
var layerCap = map[string]float64{
	LayerTrend: 3, LayerReversal: 2.5, LayerMomentum: 3, LayerTiming: 2, LayerMicro: 1.5, LayerSentiment: 1.5, LayerContext: 1.5,
}

// Vote is one indicator's independent opinion.
type Vote struct {
	Layer  string  `json:"layer"`
	Name   string  `json:"name"`
	Side   int     `json:"side"` // +1 long, -1 short
	Weight float64 `json:"weight"`
	Detail string  `json:"detail"`
}

// LayerScore is a layer's capped contribution to each side.
type LayerScore struct {
	Layer string  `json:"layer"`
	Long  float64 `json:"long"`
	Short float64 `json:"short"`
}

// Confluence is the independent long/short tally.
type Confluence struct {
	Long        float64      `json:"long"`
	Short       float64      `json:"short"`
	Margin      float64      `json:"margin"`
	Direction   int          `json:"direction"`
	Layers      []LayerScore `json:"layers"`
	LayersLong  int          `json:"layers_long"` // layers giving >= 0.5 to long
	LayersShort int          `json:"layers_short"`
	Votes       []Vote       `json:"votes"`
}

func (c Confluence) side(dir int) (win, lose float64, layers int) {
	if dir > 0 {
		return c.Long, c.Short, c.LayersLong
	}
	return c.Short, c.Long, c.LayersShort
}

type voter struct{ votes []Vote }

func (v *voter) add(layer, name string, side int, w float64, format string, args ...any) {
	if side == 0 || w <= 0 {
		return
	}
	v.votes = append(v.votes, Vote{Layer: layer, Name: name, Side: side, Weight: w, Detail: fmt.Sprintf(format, args...)})
}

func sgn(x float64) int {
	switch {
	case x > 0:
		return 1
	case x < 0:
		return -1
	}
	return 0
}

// score evaluates every indicator independently for both sides.
func score(base, mid, high Reading, rg Regime, ctx Context, der *market.Derivatives, opt *market.Options, now time.Time) Confluence {
	v := &voter{}

	// ---- Trend (high frame) ----
	if high.Ready {
		if high.LongReady {
			v.add(LayerTrend, "price_vs_ema200", sgn(high.Close-high.EMA200), 1.0, "%s close %.6g vs EMA200 %.6g", high.TF, high.Close, high.EMA200)
			v.add(LayerTrend, "ema50_vs_ema200", sgn(high.EMA50-high.EMA200), 0.5, "%s EMA50 %s EMA200", high.TF, cmpWord(high.EMA50, high.EMA200))
		} else {
			v.add(LayerTrend, "price_vs_ema50", sgn(high.Close-high.EMA50), 0.75, "%s close %.6g vs EMA50 %.6g (EMA200 still warming)", high.TF, high.Close, high.EMA50)
		}
		v.add(LayerTrend, "macd_momentum", sgn(high.MACDHist), 0.75, "%s MACD histogram %.3g", high.TF, high.MACDHist)
		v.add(LayerTrend, "market_structure", high.Structure, 1.0, "%s swings: %s", high.TF, structureWord(high.Structure))
		v.add(LayerTrend, "supertrend", high.SupertrendDir, 0.75, "%s Supertrend %s", high.TF, dirWord(high.SupertrendDir))
		v.add(LayerTrend, "hull_slope", sgn(high.HMASlope), 0.5, "%s Hull MA slope %.2f ATR/bar", high.TF, high.HMASlope)
	}

	// ---- Reversal (mid frame) ----
	if mid.Ready {
		switch {
		case mid.RSI <= 30:
			v.add(LayerReversal, "rsi_extreme", 1, 1.0, "%s RSI %.1f oversold", mid.TF, mid.RSI)
		case mid.RSI >= 70:
			v.add(LayerReversal, "rsi_extreme", -1, 1.0, "%s RSI %.1f overbought", mid.TF, mid.RSI)
		}
		if mid.Divergence != 0 {
			v.add(LayerReversal, "rsi_divergence", mid.Divergence, 1.5, "%s regular RSI divergence (%s)", mid.TF, dirWord(mid.Divergence))
		}
		if base.Divergence != 0 {
			v.add(LayerReversal, "rsi_divergence_fast", base.Divergence, 0.75, "%s regular RSI divergence (%s)", base.TF, dirWord(base.Divergence))
		}
		// Band bounce: a close back inside the band after piercing it.
		if mid.PercentB < 0.05 && mid.CandleDir > 0 {
			v.add(LayerReversal, "bb_bounce", 1, 0.75, "%s bounce off lower Bollinger band (%%B %.2f)", mid.TF, mid.PercentB)
		} else if mid.PercentB > 0.95 && mid.CandleDir < 0 {
			v.add(LayerReversal, "bb_bounce", -1, 0.75, "%s rejection at upper Bollinger band (%%B %.2f)", mid.TF, mid.PercentB)
		}
		if mid.Pattern != nil && mid.Pattern.Bias != 0 {
			v.add(LayerReversal, "candle_pattern", mid.Pattern.Bias, 0.5+0.5*mid.Pattern.Strength, "%s %s", mid.TF, mid.Pattern.Name)
		}
		if base.Pattern != nil && base.Pattern.Bias != 0 && base.Pattern.Bars >= 2 {
			v.add(LayerReversal, "candle_pattern_fast", base.Pattern.Bias, 0.25+0.25*base.Pattern.Strength, "%s %s", base.TF, base.Pattern.Name)
		}
		if mid.SupportDist > 0 && mid.SupportDist <= 0.75 {
			v.add(LayerReversal, "near_support", 1, 0.75, "%s support %.8g is %.2f ATR below", mid.TF, mid.Support, mid.SupportDist)
		}
		if mid.ResistanceDist > 0 && mid.ResistanceDist <= 0.75 {
			v.add(LayerReversal, "near_resistance", -1, 0.75, "%s resistance %.8g is %.2f ATR above", mid.TF, mid.Resistance, mid.ResistanceDist)
		}
		// Squeeze resolving with a directional breakout.
		if mid.SqueezeRelease && mid.DonchianBreak != 0 {
			v.add(LayerReversal, "squeeze_breakout", mid.DonchianBreak, 1.0, "%s volatility squeeze released with a %s breakout", mid.TF, dirWord(mid.DonchianBreak))
		}
	}

	// ---- Momentum (mid frame) ----
	if mid.Ready {
		if mid.RelVol >= 1.8 && mid.CandleDir != 0 {
			v.add(LayerMomentum, "volume_surge", mid.CandleDir, 1.0, "%s volume %.1fx average on a %s candle", mid.TF, mid.RelVol, dirWord(mid.CandleDir))
		}
		if mid.MACDHist != 0 && sgn(mid.MACDHist) == sgn(mid.MACDDelta) {
			v.add(LayerMomentum, "macd_building", sgn(mid.MACDHist), 1.0, "%s MACD histogram %.3g and rising in magnitude", mid.TF, mid.MACDHist)
		}
		if mid.StochCrossUp && mid.StochK < 50 {
			v.add(LayerMomentum, "stochrsi_cross", 1, 0.75, "%s StochRSI K crossed above D at %.0f", mid.TF, mid.StochK)
		} else if mid.StochCrossDown && mid.StochK > 50 {
			v.add(LayerMomentum, "stochrsi_cross", -1, 0.75, "%s StochRSI K crossed below D at %.0f", mid.TF, mid.StochK)
		}
		if math.Abs(base.VWAPDev) >= 0.25 {
			v.add(LayerMomentum, "vwap_position", sgn(base.VWAPDev), 0.5, "price %.2f SD %s session VWAP %.8g", math.Abs(base.VWAPDev), aboveBelow(base.VWAPDev), base.VWAP)
		}
		if math.Abs(mid.CMF) >= 0.08 {
			v.add(LayerMomentum, "money_flow", sgn(mid.CMF), 0.5, "%s Chaikin money flow %.2f", mid.TF, mid.CMF)
		}
		if mid.DonchianBreak != 0 && mid.RelVol >= 1.2 {
			v.add(LayerMomentum, "donchian_breakout", mid.DonchianBreak, 0.75, "%s close broke the 20-bar %s on %.1fx volume", mid.TF, channelWord(mid.DonchianBreak), mid.RelVol)
		}
		if mid.ADX >= 25 {
			v.add(LayerMomentum, "adx_direction", sgn(mid.PlusDI-mid.MinusDI), 0.5, "%s ADX %.0f with +DI %.0f / -DI %.0f", mid.TF, mid.ADX, mid.PlusDI, mid.MinusDI)
		}
	}

	// ---- Timing (base frame) ----
	if base.Ready {
		if abs(base.Streak) >= 3 && base.BodyRatio >= 0.5 {
			v.add(LayerTiming, "momentum_burst", sgn(float64(base.Streak)), 0.75, "%d consecutive %s closes on %s", abs(base.Streak), dirWord(sgn(float64(base.Streak))), base.TF)
		}
		if base.PrevRSIFast <= 50 && base.RSIFast > 50 {
			v.add(LayerTiming, "fast_rsi_cross", 1, 0.75, "%s RSI(7) crossed above 50", base.TF)
		} else if base.PrevRSIFast >= 50 && base.RSIFast < 50 {
			v.add(LayerTiming, "fast_rsi_cross", -1, 0.75, "%s RSI(7) crossed below 50", base.TF)
		}
		if base.BodyRatio >= 0.7 && base.CandleDir != 0 {
			v.add(LayerTiming, "strong_close", base.CandleDir, 0.5, "%s candle body is %.0f%% of its range", base.TF, base.BodyRatio*100)
		}
		if base.SupertrendFlip {
			v.add(LayerTiming, "supertrend_flip", base.SupertrendDir, 0.75, "%s Supertrend flipped %s", base.TF, dirWord(base.SupertrendDir))
		}
	}

	// ---- Microstructure (perpetual swaps) ----
	if der != nil && der.Available && now.Sub(der.Time) < 2*time.Hour {
		// Rising OI with price = fresh positioning behind the move (confirmation).
		if math.Abs(der.OIChangePct) >= 1 && math.Abs(der.PriceChgPct) >= 0.3 {
			if der.OIChangePct > 0 {
				v.add(LayerMicro, "oi_confirms", sgn(der.PriceChgPct), 1.0, "open interest %+.1f%% with price %+.2f%%: new positions behind the move", der.OIChangePct, der.PriceChgPct)
			} else {
				// Falling OI: the move is closing positions (covering / liquidation), weaker.
				v.add(LayerMicro, "oi_unwind", -sgn(der.PriceChgPct), 0.25, "open interest %.1f%% while price %+.2f%%: move driven by position closing", der.OIChangePct, der.PriceChgPct)
			}
		}
		// Extreme funding marks crowding. The repo's fade-forced-flush research found
		// crowding *reversal* trades unprofitable, so funding only argues against the
		// crowded side (it adds disagreement) instead of triggering a fade.
		if der.FundingRate >= 0.0005 {
			v.add(LayerMicro, "funding_crowded", -1, 0.5, "funding %.4f%% per period: longs crowded", der.FundingRate*100)
		} else if der.FundingRate <= -0.0003 {
			v.add(LayerMicro, "funding_crowded", 1, 0.5, "funding %.4f%% per period: shorts crowded", der.FundingRate*100)
		}
		if math.Abs(der.BasisPct) >= 0.15 {
			v.add(LayerMicro, "basis", sgn(der.BasisPct), 0.25, "perp basis %+.2f%%", der.BasisPct)
		}
	}

	// ---- Options sentiment (Deribit) ----
	if opt != nil && opt.Available && now.Sub(opt.Time) < 6*time.Hour {
		// Contrarian put/call ratio (PCR strategy): heavy put demand marks fear.
		switch {
		case opt.PCRByVol >= 1.2:
			v.add(LayerSentiment, "put_call_ratio", 1, 0.75, "put/call volume ratio %.2f: fear (contrarian bullish)", opt.PCRByVol)
		case opt.PCRByVol > 0 && opt.PCRByVol <= 0.45:
			v.add(LayerSentiment, "put_call_ratio", -1, 0.75, "put/call volume ratio %.2f: complacency (contrarian bearish)", opt.PCRByVol)
		}
		// Skew: calls bid over puts = upside demand.
		if math.Abs(opt.Skew25) >= 2 {
			v.add(LayerSentiment, "skew", sgn(opt.Skew25), 0.5, "OTM call-minus-put IV %+.1f vol points", opt.Skew25)
		}
		// Implied-vol spike while price is stretched (VIX strategy): fear tends to mean-revert.
		if opt.DVOLChange >= 15 && mid.ZScore <= -1.5 {
			v.add(LayerSentiment, "dvol_spike", 1, 0.75, "DVOL %+.0f%% in a day with price %.1f SD below its mean", opt.DVOLChange, -mid.ZScore)
		}
	}

	// ---- Cross-asset context ----
	if ctx.Available {
		if math.Abs(ctx.RelStrength) >= 1 {
			v.add(LayerContext, "relative_strength", sgn(ctx.RelStrength), 0.75, "%+.1f%% vs %s over the %s lookback", ctx.RelStrength, ctx.RefSymbol, mid.TF)
		}
		v.add(LayerContext, "reference_trend", ctx.RefTrend, 0.5, "%s %s trend is %s", ctx.RefSymbol, high.TF, dirWord(ctx.RefTrend))
	}
	if rg.HighTrend != 0 && (rg.Kind == RegimeTrendUp || rg.Kind == RegimeTrendDown) {
		v.add(LayerContext, "regime_alignment", rg.HighTrend, 0.5, "%s regime agrees with the %s trend", rg.Kind, high.TF)
	}

	return tally(v.votes)
}

func tally(votes []Vote) Confluence {
	c := Confluence{Votes: votes}
	per := map[string]*LayerScore{}
	for _, l := range Layers {
		per[l] = &LayerScore{Layer: l}
	}
	for _, vt := range votes {
		ls := per[vt.Layer]
		if vt.Side > 0 {
			ls.Long += vt.Weight
		} else {
			ls.Short += vt.Weight
		}
	}
	for _, l := range Layers {
		ls := per[l]
		ls.Long = math.Min(ls.Long, layerCap[l])
		ls.Short = math.Min(ls.Short, layerCap[l])
		c.Long += ls.Long
		c.Short += ls.Short
		if ls.Long >= 0.5 {
			c.LayersLong++
		}
		if ls.Short >= 0.5 {
			c.LayersShort++
		}
		c.Layers = append(c.Layers, *ls)
	}
	c.Margin = math.Abs(c.Long - c.Short)
	switch {
	case c.Long > c.Short:
		c.Direction = 1
	case c.Short > c.Long:
		c.Direction = -1
	}
	sort.SliceStable(c.Votes, func(i, j int) bool { return c.Votes[i].Weight > c.Votes[j].Weight })
	return c
}

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

func dirWord(d int) string {
	switch {
	case d > 0:
		return "bullish"
	case d < 0:
		return "bearish"
	}
	return "neutral"
}

func structureWord(s int) string {
	switch s {
	case 1:
		return "higher highs and higher lows"
	case -1:
		return "lower highs and lower lows"
	}
	return "mixed"
}

func cmpWord(a, b float64) string {
	if a > b {
		return "above"
	}
	return "below"
}

func aboveBelow(x float64) string {
	if x > 0 {
		return "above"
	}
	return "below"
}

func channelWord(d int) string {
	if d > 0 {
		return "high"
	}
	return "low"
}
