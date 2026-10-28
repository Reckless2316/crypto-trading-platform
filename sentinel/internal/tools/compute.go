package tools

import (
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/candles"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/ta"
)

// Series is one output line of an indicator.
type Series struct {
	Times  []time.Time `json:"times"`
	Values []float64   `json:"values"`
}

// IndicatorNames lists what Compute accepts.
var IndicatorNames = []string{"sma", "ema", "wma", "hma", "kama", "rsi", "macd", "atr", "adx", "bollinger", "keltner",
	"stochrsi", "mfi", "obv", "cmf", "vwap", "supertrend", "donchian", "roc", "efficiency_ratio", "zscore", "linreg_slope", "parkinson_vol"}

type outputs struct {
	names  []string
	series map[string]*Series
}

func newOutputs(names ...string) *outputs {
	o := &outputs{names: names, series: map[string]*Series{}}
	for _, n := range names {
		o.series[n] = &Series{}
	}
	return o
}

func (o *outputs) add(t time.Time, ready bool, vals ...float64) {
	if !ready {
		return
	}
	for i, n := range o.names {
		s := o.series[n]
		s.Times = append(s.Times, t)
		s.Values = append(s.Values, vals[i])
	}
}

// Compute runs one indicator over bars. period <= 0 selects the conventional default.
func Compute(name string, period int, bars []market.Bar) (map[string]Series, error) {
	p := func(d int) int {
		if period > 0 {
			return period
		}
		return d
	}
	var o *outputs
	n := strings.ToLower(strings.TrimSpace(name))
	switch n {
	case "sma", "ema", "wma", "hma", "kama", "rsi", "roc", "efficiency_ratio", "linreg_slope":
		type upd interface {
			Update(float64) float64
			Ready() bool
			Value() float64
		}
		var ind upd
		switch n {
		case "sma":
			ind = ta.NewSMA(p(20))
		case "ema":
			ind = ta.NewEMA(p(20))
		case "wma":
			ind = ta.NewWMA(p(20))
		case "hma":
			ind = ta.NewHMA(p(55))
		case "kama":
			ind = ta.NewKAMA(p(10), 2, 30)
		case "rsi":
			ind = ta.NewRSI(p(14))
		case "roc":
			ind = ta.NewROC(p(10))
		case "efficiency_ratio":
			ind = ta.NewEfficiencyRatio(p(20))
		case "linreg_slope":
			ind = ta.NewLinRegSlope(p(14))
		}
		o = newOutputs(n)
		for _, b := range bars {
			ind.Update(b.Close)
			o.add(b.Time, ind.Ready(), ind.Value())
		}
	case "macd":
		m := ta.NewMACD(12, 26, 9)
		o = newOutputs("macd", "signal", "histogram")
		for _, b := range bars {
			m.Update(b.Close)
			o.add(b.Time, m.Ready(), m.Line, m.Signal, m.Hist)
		}
	case "atr":
		a := ta.NewATR(p(14))
		o = newOutputs("atr")
		for _, b := range bars {
			a.Update(b.High, b.Low, b.Close)
			o.add(b.Time, a.Ready(), a.Value())
		}
	case "adx":
		a := ta.NewADX(p(14))
		o = newOutputs("adx", "plus_di", "minus_di")
		for _, b := range bars {
			a.Update(b.High, b.Low, b.Close)
			o.add(b.Time, a.Ready(), a.Value, a.PlusDI, a.MinusDI)
		}
	case "bollinger":
		bb := ta.NewBollinger(p(20), 2)
		o = newOutputs("upper", "middle", "lower", "percent_b")
		for _, b := range bars {
			bb.Update(b.Close)
			o.add(b.Time, bb.Ready(), bb.Upper, bb.Middle, bb.Lower, bb.PercentB(b.Close))
		}
	case "keltner":
		k := ta.NewKeltner(p(20), 1.5)
		o = newOutputs("upper", "middle", "lower")
		for _, b := range bars {
			k.Update(b.High, b.Low, b.Close)
			o.add(b.Time, k.Ready(), k.Upper, k.Middle, k.Lower)
		}
	case "stochrsi":
		sr := ta.NewStochRSI(p(14), 14, 3)
		o = newOutputs("k", "d")
		for _, b := range bars {
			sr.Update(b.Close)
			o.add(b.Time, sr.Ready(), sr.K, sr.D)
		}
	case "mfi":
		m := ta.NewMFI(p(14))
		o = newOutputs("mfi")
		for _, b := range bars {
			m.Update(b.High, b.Low, b.Close, b.Volume)
			o.add(b.Time, m.Ready(), m.Value())
		}
	case "obv":
		ob := ta.NewOBV()
		o = newOutputs("obv")
		for _, b := range bars {
			o.add(b.Time, true, ob.Update(b.Close, b.Volume))
		}
	case "cmf":
		c := ta.NewCMF(p(20))
		o = newOutputs("cmf")
		for _, b := range bars {
			c.Update(b.High, b.Low, b.Close, b.Volume)
			o.add(b.Time, c.Ready(), c.Value())
		}
	case "vwap":
		v := ta.NewSessionVWAP()
		o = newOutputs("vwap", "deviation_sd")
		for _, b := range bars {
			v.Update(b.Time, b.High, b.Low, b.Close, b.Volume)
			o.add(b.Time, true, v.Value, v.Deviation(b.Close))
		}
	case "supertrend":
		st := ta.NewSupertrend(p(10), 3)
		o = newOutputs("line", "direction")
		for _, b := range bars {
			st.Update(b.High, b.Low, b.Close)
			o.add(b.Time, st.Ready(), st.Line, float64(st.Dir))
		}
	case "donchian":
		d := ta.NewDonchian(p(20))
		o = newOutputs("upper", "lower")
		for _, b := range bars {
			d.Update(b.High, b.Low)
			o.add(b.Time, d.Ready(), d.Upper, d.Lower)
		}
	case "zscore":
		z := ta.NewStdDev(p(20))
		o = newOutputs("zscore")
		for _, b := range bars {
			z.Update(b.Close)
			o.add(b.Time, z.Ready(), z.ZScore(b.Close))
		}
	case "parkinson_vol":
		pv := ta.NewParkinson(p(20))
		o = newOutputs("sigma_per_bar")
		for _, b := range bars {
			pv.Update(b.High, b.Low)
			o.add(b.Time, pv.Ready(), pv.Value())
		}
	default:
		return nil, fmt.Errorf("unknown indicator %q (supported: %s)", name, strings.Join(IndicatorNames, ", "))
	}
	out := map[string]Series{}
	for k, v := range o.series {
		out[k] = *v
	}
	return out, nil
}

// ScanPatterns detects candlestick patterns over the last lookback bars, with
// the same ATR normalisation and trend context the engine uses.
func ScanPatterns(bars []market.Bar, lookback int) []PatternHit {
	atr := ta.NewATR(14)
	ema := ta.NewEMA(20)
	var hits []PatternHit
	emas := make([]float64, len(bars))
	for i, b := range bars {
		atr.Update(b.High, b.Low, b.Close)
		emas[i] = ema.Update(b.Close)
		if i < 4 || !atr.Ready() || i < len(bars)-lookback {
			continue
		}
		trend := 0
		if bars[i-3].Close > emas[i-4] && emas[i-1] > emas[i-4] {
			trend = 1
		} else if bars[i-3].Close < emas[i-4] && emas[i-1] < emas[i-4] {
			trend = -1
		}
		if p, ok := candles.Detect(bars[i-2:i+1], atr.Value(), trend); ok {
			hits = append(hits, PatternHit{Time: b.Time, Close: b.Close, Pattern: p})
		}
	}
	sort.SliceStable(hits, func(i, j int) bool { return hits[i].Time.After(hits[j].Time) })
	return hits
}

func openRead(path string) (*os.File, error) { return os.Open(path) }
