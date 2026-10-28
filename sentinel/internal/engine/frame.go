// Package engine turns a stream of closed base-timeframe bars into Sentinel
// readings, layered confluence scores, model probabilities and signals.
package engine

import (
	"math"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/candles"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/ta"
)

// Reading is every indicator value for one timeframe at one closed bar.
// Oscillators are in their native units; distances are in ATR multiples.
type Reading struct {
	TF        string    `json:"tf"`
	Time      time.Time `json:"time"`
	Close     float64   `json:"close"`
	Ready     bool      `json:"ready"`
	LongReady bool      `json:"long_ready"` // EMA200 available
	Bars      int       `json:"bars"`

	EMA20      float64 `json:"ema20"`
	EMA50      float64 `json:"ema50"`
	EMA200     float64 `json:"ema200"`
	HMA        float64 `json:"hma"`
	HMASlope   float64 `json:"hma_slope_atr"` // HMA change over the last bar, in ATR
	KAMASlope  float64 `json:"kama_slope_atr"`
	DistEMA50  float64 `json:"dist_ema50_atr"`
	DistEMA200 float64 `json:"dist_ema200_atr"`

	RSI         float64 `json:"rsi"`
	RSIFast     float64 `json:"rsi_fast"`
	PrevRSIFast float64 `json:"-"`

	MACD      float64 `json:"macd"`
	MACDSig   float64 `json:"macd_signal"`
	MACDHist  float64 `json:"macd_hist"`
	MACDDelta float64 `json:"macd_hist_delta"`

	ATR           float64 `json:"atr"`
	ATRPct        float64 `json:"atr_pct"`
	VolPercentile float64 `json:"vol_percentile"`
	ParkinsonVol  float64 `json:"parkinson_vol"`

	ADX     float64 `json:"adx"`
	PlusDI  float64 `json:"plus_di"`
	MinusDI float64 `json:"minus_di"`

	PercentB       float64 `json:"bb_percent_b"`
	BBWidth        float64 `json:"bb_width"`
	BBUpper        float64 `json:"bb_upper"`
	BBLower        float64 `json:"bb_lower"`
	Squeeze        bool    `json:"squeeze"`
	SqueezeRelease bool    `json:"squeeze_release"`

	StochK         float64 `json:"stochrsi_k"`
	StochD         float64 `json:"stochrsi_d"`
	StochCrossUp   bool    `json:"stochrsi_cross_up"`
	StochCrossDown bool    `json:"stochrsi_cross_down"`

	MFI      float64 `json:"mfi"`
	CMF      float64 `json:"cmf"`
	OBVSlope float64 `json:"obv_slope"` // OBV regression slope / average volume
	RelVol   float64 `json:"rel_volume"`
	VWAP     float64 `json:"vwap"`
	VWAPDev  float64 `json:"vwap_dev_sd"`

	SupertrendDir  int     `json:"supertrend_dir"`
	SupertrendFlip bool    `json:"supertrend_flip"`
	Supertrend     float64 `json:"supertrend"`
	DonchianPos    float64 `json:"donchian_pos"`
	DonchianBreak  int     `json:"donchian_break"`
	ER             float64 `json:"efficiency_ratio"`
	ZScore         float64 `json:"zscore"`
	ROC            float64 `json:"roc"`

	Structure  int     `json:"structure"`
	Divergence int     `json:"divergence"`
	Streak     int     `json:"streak"`
	BodyRatio  float64 `json:"body_ratio"`
	CandleDir  int     `json:"candle_dir"`

	Pattern *candles.Pattern `json:"pattern,omitempty"`

	Support        float64 `json:"support"`
	Resistance     float64 `json:"resistance"`
	SupportDist    float64 `json:"support_dist_atr"`
	ResistanceDist float64 `json:"resistance_dist_atr"`
}

// Frame is the indicator bank for one timeframe.
type Frame struct {
	tf market.Timeframe

	ema20, ema50, ema200 *ta.EMA
	hma                  *ta.HMA
	kama                 *ta.KAMA
	rsi, rsiFast         *ta.RSI
	macd                 *ta.MACD
	atr                  *ta.ATR
	adx                  *ta.ADX
	bb                   *ta.Bollinger
	kc                   *ta.Keltner
	stoch                *ta.StochRSI
	mfi                  *ta.MFI
	cmf                  *ta.CMF
	obv                  *ta.OBV
	obvSlope             *ta.LinRegSlope
	volSMA               *ta.SMA
	relVol               *ta.RelativeVolume
	vwap                 *ta.SessionVWAP
	st                   *ta.Supertrend
	don                  *ta.Donchian
	er                   *ta.EfficiencyRatio
	z                    *ta.StdDev
	roc                  *ta.ROC
	park                 *ta.Parkinson
	volRank              *ta.PercentRank
	swings               *ta.Swings
	streak               ta.Streak

	window    []market.Bar // last 3 bars for pattern recognition
	n         int
	kamaPrev  float64
	prevSqz   bool
	prevDonHi float64
	prevDonLo float64
	last      Reading
}

// NewFrame builds the standard indicator bank for a timeframe.
func NewFrame(tf market.Timeframe) *Frame {
	return &Frame{
		tf:       tf,
		ema20:    ta.NewEMA(20),
		ema50:    ta.NewEMA(50),
		ema200:   ta.NewEMA(200),
		hma:      ta.NewHMA(55),
		kama:     ta.NewKAMA(10, 2, 30),
		rsi:      ta.NewRSI(14),
		rsiFast:  ta.NewRSI(7),
		macd:     ta.NewMACD(12, 26, 9),
		atr:      ta.NewATR(14),
		adx:      ta.NewADX(14),
		bb:       ta.NewBollinger(20, 2),
		kc:       ta.NewKeltner(20, 1.5),
		stoch:    ta.NewStochRSI(14, 14, 3),
		mfi:      ta.NewMFI(14),
		cmf:      ta.NewCMF(20),
		obv:      ta.NewOBV(),
		obvSlope: ta.NewLinRegSlope(14),
		volSMA:   ta.NewSMA(20),
		relVol:   ta.NewRelativeVolume(20),
		vwap:     ta.NewSessionVWAP(),
		st:       ta.NewSupertrend(10, 3),
		don:      ta.NewDonchian(20),
		er:       ta.NewEfficiencyRatio(20),
		z:        ta.NewStdDev(20),
		roc:      ta.NewROC(10),
		park:     ta.NewParkinson(20),
		volRank:  ta.NewPercentRank(200),
		swings:   ta.NewSwings(3, 3, 24),
	}
}

// warmupBars is how many bars a frame needs before its core indicators are
// valid. The 200-EMA needs longer; LongReady reports when it is usable.
const warmupBars = 60

// Last returns the most recent reading.
func (f *Frame) Last() Reading { return f.last }

// Swings exposes the pivot tracker (for levels).
func (f *Frame) Swings() *ta.Swings { return f.swings }

// Update consumes one closed bar and returns the new reading.
func (f *Frame) Update(b market.Bar) Reading {
	f.n++
	prev := f.last
	r := Reading{TF: f.tf.Name, Time: b.Time, Close: b.Close, Bars: f.n}

	r.EMA20 = f.ema20.Update(b.Close)
	r.EMA50 = f.ema50.Update(b.Close)
	r.EMA200 = f.ema200.Update(b.Close)
	r.HMA = f.hma.Update(b.Close)
	kama := f.kama.Update(b.Close)
	r.RSI = f.rsi.Update(b.Close)
	r.PrevRSIFast = prev.RSIFast
	r.RSIFast = f.rsiFast.Update(b.Close)
	f.macd.Update(b.Close)
	r.MACD, r.MACDSig, r.MACDHist, r.MACDDelta = f.macd.Line, f.macd.Signal, f.macd.Hist, f.macd.HistDelta()
	r.ATR = f.atr.Update(b.High, b.Low, b.Close)
	if b.Close > 0 {
		r.ATRPct = r.ATR / b.Close * 100
	}
	r.VolPercentile = f.volRank.Update(r.ATRPct)
	r.ParkinsonVol = f.park.Update(b.High, b.Low)
	f.adx.Update(b.High, b.Low, b.Close)
	r.ADX, r.PlusDI, r.MinusDI = f.adx.Value, f.adx.PlusDI, f.adx.MinusDI

	f.bb.Update(b.Close)
	f.kc.Update(b.High, b.Low, b.Close)
	r.BBUpper, r.BBLower = f.bb.Upper, f.bb.Lower
	r.PercentB = f.bb.PercentB(b.Close)
	r.BBWidth = f.bb.Width()
	r.Squeeze = f.bb.Ready() && f.kc.Ready() && f.bb.Upper < f.kc.Upper && f.bb.Lower > f.kc.Lower
	r.SqueezeRelease = f.prevSqz && !r.Squeeze
	f.prevSqz = r.Squeeze

	f.stoch.Update(b.Close)
	r.StochK, r.StochD = f.stoch.K, f.stoch.D
	r.StochCrossUp, r.StochCrossDown = f.stoch.CrossUp(), f.stoch.CrossDown()

	r.MFI = f.mfi.Update(b.High, b.Low, b.Close, b.Volume)
	r.CMF = f.cmf.Update(b.High, b.Low, b.Close, b.Volume)
	obv := f.obv.Update(b.Close, b.Volume)
	slope := f.obvSlope.Update(obv)
	avgVol := f.volSMA.Update(b.Volume)
	if avgVol > 0 {
		r.OBVSlope = slope / avgVol
	}
	r.RelVol = f.relVol.Update(b.Volume)
	f.vwap.Update(b.Time, b.High, b.Low, b.Close, b.Volume)
	r.VWAP = f.vwap.Value
	r.VWAPDev = f.vwap.Deviation(b.Close)

	f.st.Update(b.High, b.Low, b.Close)
	r.SupertrendDir, r.SupertrendFlip, r.Supertrend = f.st.Dir, f.st.Flipped, f.st.Line

	// Donchian breakout is judged against the channel *before* this bar.
	if f.don.Ready() {
		if b.Close > f.prevDonHi {
			r.DonchianBreak = 1
		} else if b.Close < f.prevDonLo {
			r.DonchianBreak = -1
		}
	}
	f.don.Update(b.High, b.Low)
	f.prevDonHi, f.prevDonLo = f.don.Upper, f.don.Lower
	if w := f.don.Upper - f.don.Lower; w > 0 {
		r.DonchianPos = (b.Close - f.don.Lower) / w
	} else {
		r.DonchianPos = 0.5
	}

	r.ER = f.er.Update(b.Close)
	f.z.Update(b.Close)
	r.ZScore = f.z.ZScore(b.Close)
	r.ROC = f.roc.Update(b.Close)

	f.swings.Update(b.High, b.Low, r.RSI)
	r.Structure = f.swings.Structure()
	r.Divergence = f.swings.Divergence(40, 45, 55)
	r.Streak = f.streak.Update(b.Close)
	if rng := b.Range(); rng > 0 {
		r.BodyRatio = b.Body() / rng
	}
	switch {
	case b.Close > b.Open:
		r.CandleDir = 1
	case b.Close < b.Open:
		r.CandleDir = -1
	}

	if r.ATR > 0 {
		r.HMASlope = (r.HMA - prev.HMA) / r.ATR
		r.KAMASlope = (kama - f.kamaPrev) / r.ATR
		r.DistEMA50 = (b.Close - r.EMA50) / r.ATR
		if f.ema200.Ready() {
			r.DistEMA200 = (b.Close - r.EMA200) / r.ATR
		}
	}
	f.kamaPrev = kama

	// Pattern recognition uses the trend measured before the pattern began:
	// the sign of the 20-EMA slope three bars back.
	f.window = append(f.window, b)
	if len(f.window) > 4 {
		f.window = f.window[len(f.window)-4:]
	}
	if len(f.window) == 4 && r.ATR > 0 {
		trend := 0
		if f.window[0].Close > prev.EMA20 && prev.EMA20 > 0 && prev.DistEMA50 > 0.5 {
			trend = 1
		} else if f.window[0].Close < prev.EMA20 && prev.DistEMA50 < -0.5 {
			trend = -1
		}
		if p, ok := candles.Detect(f.window[1:], r.ATR, trend); ok {
			pp := p
			r.Pattern = &pp
		}
	}

	if r.ATR > 0 {
		r.Support, r.Resistance = math.NaN(), math.NaN()
		for _, lv := range f.swings.Levels(b.Close, 0.5*r.ATR) {
			if lv.Kind == "support" && math.IsNaN(r.Support) {
				r.Support = lv.Price
				r.SupportDist = (b.Close - lv.Price) / r.ATR
			}
			if lv.Kind == "resistance" && math.IsNaN(r.Resistance) {
				r.Resistance = lv.Price
				r.ResistanceDist = (lv.Price - b.Close) / r.ATR
			}
		}
		if math.IsNaN(r.Support) {
			r.Support, r.SupportDist = 0, 0
		}
		if math.IsNaN(r.Resistance) {
			r.Resistance, r.ResistanceDist = 0, 0
		}
	}

	r.Ready = f.n >= warmupBars && f.ema50.Ready() && f.adx.Ready() && f.stoch.Ready() && f.macd.Ready()
	r.LongReady = f.ema200.Ready()
	f.last = r
	return r
}
