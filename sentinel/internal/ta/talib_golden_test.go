package ta

import (
	"encoding/csv"
	"math"
	"os"
	"strconv"
	"testing"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// golden loads testdata/talib_golden.csv (produced by tools/talibgold from the
// TA-Lib C library) as column name -> per-bar values (NaN where TA-Lib has no output).
func golden(t *testing.T) (map[string][]float64, []market.Bar) {
	t.Helper()
	bars, err := market.LoadCSV("../../testdata/btc_usdt_1h.csv")
	if err != nil {
		t.Fatal(err)
	}
	f, err := os.Open("../../testdata/talib_golden.csv")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	rows, err := csv.NewReader(f).ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	out := map[string][]float64{}
	for j, name := range rows[0] {
		col := make([]float64, len(rows)-1)
		for i, r := range rows[1:] {
			if r[j] == "" {
				col[i] = math.NaN()
				continue
			}
			v, err := strconv.ParseFloat(r[j], 64)
			if err != nil {
				t.Fatal(err)
			}
			col[i] = v
		}
		out[name] = col
	}
	if len(out["i"]) != len(bars) {
		t.Fatalf("golden rows %d != bars %d", len(out["i"]), len(bars))
	}
	return out, bars
}

// compare checks every bar where TA-Lib produced a value and ours is ready,
// starting at `from` (indicators with long memory are compared once seeding
// differences have decayed). The tolerance is relative to max(1, |want|) and
// to the fixture's 10 significant digits.
func compare(t *testing.T, name string, want []float64, got []float64, ready []bool, from int, tol float64) {
	t.Helper()
	checked := 0
	for i := from; i < len(want); i++ {
		if math.IsNaN(want[i]) {
			continue
		}
		if !ready[i] {
			t.Fatalf("%s: TA-Lib has a value at bar %d but ours is not ready", name, i)
		}
		scale := math.Max(1, math.Abs(want[i]))
		if d := math.Abs(got[i]-want[i]) / scale; d > tol {
			t.Fatalf("%s bar %d: got %.10g want %.10g (rel err %.3g)", name, i, got[i], want[i], d)
		}
		checked++
	}
	if checked < 500 {
		t.Fatalf("%s: only %d values compared", name, checked)
	}
}

func TestTALibParity(t *testing.T) {
	g, bars := golden(t)
	n := len(bars)
	type series struct {
		v []float64
		r []bool
	}
	mk := func() *series { return &series{v: make([]float64, n), r: make([]bool, n)} }
	s := map[string]*series{}
	for _, k := range []string{"sma20", "ema20", "wma20", "kama10", "rsi14", "atr14", "adx14", "pdi14", "mdi14",
		"mfi14", "obv", "roc10", "slope14", "stddev20", "macd", "macd_signal", "macd_hist",
		"bb_upper", "bb_middle", "bb_lower", "stochrsi_k", "stochrsi_d"} {
		s[k] = mk()
	}
	sma, ema, wma, kama := NewSMA(20), NewEMA(20), NewWMA(20), NewKAMA(10, 2, 30)
	rsi, atr, adx, mfi, obv := NewRSI(14), NewATR(14), NewADX(14), NewMFI(14), NewOBV()
	roc, slope, sd := NewROC(10), NewLinRegSlope(14), NewStdDev(20)
	macd, bb, srsi := NewMACD(12, 26, 9), NewBollinger(20, 2), NewStochRSI(14, 14, 3)
	set := func(k string, i int, v float64, ok bool) { s[k].v[i], s[k].r[i] = v, ok }
	for i, b := range bars {
		set("sma20", i, sma.Update(b.Close), sma.Ready())
		set("ema20", i, ema.Update(b.Close), ema.Ready())
		set("wma20", i, wma.Update(b.Close), wma.Ready())
		set("kama10", i, kama.Update(b.Close), kama.Ready())
		set("rsi14", i, rsi.Update(b.Close), rsi.Ready())
		set("atr14", i, atr.Update(b.High, b.Low, b.Close), atr.Ready())
		adx.Update(b.High, b.Low, b.Close)
		set("adx14", i, adx.Value, adx.Ready())
		set("pdi14", i, adx.PlusDI, adx.Ready())
		set("mdi14", i, adx.MinusDI, adx.Ready())
		set("mfi14", i, mfi.Update(b.High, b.Low, b.Close, b.Volume), mfi.Ready())
		set("obv", i, obv.Update(b.Close, b.Volume), true)
		set("roc10", i, roc.Update(b.Close), roc.Ready())
		set("slope14", i, slope.Update(b.Close), slope.Ready())
		set("stddev20", i, sd.Update(b.Close), sd.Ready())
		macd.Update(b.Close)
		set("macd", i, macd.Line, macd.Ready())
		set("macd_signal", i, macd.Signal, macd.Ready())
		set("macd_hist", i, macd.Hist, macd.Ready())
		bb.Update(b.Close)
		set("bb_upper", i, bb.Upper, bb.Ready())
		set("bb_middle", i, bb.Middle, bb.Ready())
		set("bb_lower", i, bb.Lower, bb.Ready())
		srsi.Update(b.Close)
		set("stochrsi_k", i, srsi.K, srsi.Ready())
		set("stochrsi_d", i, srsi.D, srsi.Ready())
	}
	// Exact-seeded indicators are compared from their first output.
	exact := map[string]float64{
		"sma20": 1e-9, "ema20": 1e-9, "wma20": 1e-9, "rsi14": 1e-8, "atr14": 1e-8, "mfi14": 1e-8,
		"obv": 1e-9, "roc10": 1e-8, "slope14": 1e-7, "stddev20": 1e-7,
		"bb_upper": 1e-9, "bb_middle": 1e-9, "bb_lower": 1e-9,
	}
	for k, tol := range exact {
		compare(t, k, g[k], s[k].v, s[k].r, 0, tol)
	}
	// Recursive indicators whose TA-Lib seeding differs slightly are compared
	// after 300 bars, by which point any seeding difference has decayed.
	late := map[string]float64{
		"kama10": 1e-8, "adx14": 1e-7, "pdi14": 1e-7, "mdi14": 1e-7,
		"macd": 1e-7, "macd_signal": 1e-7, "macd_hist": 1e-7, "stochrsi_k": 1e-6, "stochrsi_d": 1e-6,
	}
	for k, tol := range late {
		compare(t, k, g[k], s[k].v, s[k].r, 300, tol)
	}
}
