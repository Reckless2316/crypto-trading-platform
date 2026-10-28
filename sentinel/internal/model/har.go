package model

import (
	"math"
	"sort"
)

// VolForecaster is a HAR-RV style volatility forecaster (Corsi, 2009) adapted
// to intraday bars. It regresses the log of the mean Parkinson variance over
// the next h bars on log mean variances over several trailing windows, fitted
// online by recursive least squares with exponential forgetting. Volatility
// clustering makes this one of the most reliably predictable quantities in
// markets, and every forecast is scored out-of-sample against the naive
// persistence forecast before it is trusted.
type VolForecaster struct {
	h      int
	comps  []int
	maxLag int
	vars   []float64 // per-bar variance history, newest last (capped)
	pend   []volPending
	theta  []float64
	P      [][]float64
	lambda float64
	n      int // resolved forecasts learned

	resVar             float64 // EWMA of squared residuals (log space)
	sseModel, sseNaive float64
	sumY, sumY2        float64
	scored             int
	in68, in90, banded int       // horizon closes inside the forecast bands
	zs                 []float64 // recent standardised horizon returns, for empirical bands
	zHead              int
}

// zCap bounds the empirical quantile window.
const zCap = 3000

// Quantiles of standardised horizon returns used for the bands; normal values
// until enough history has resolved, then the empirical distribution (fat
// tails and skew included).
func (v *VolForecaster) bandZ() (q05, q16, q84, q95 float64) {
	if len(v.zs) < 300 {
		return -1.645, -1, 1, 1.645
	}
	z := append([]float64(nil), v.zs...)
	sort.Float64s(z)
	q := func(p float64) float64 { return z[int(p*float64(len(z)-1))] }
	return q(0.05), q(0.16), q(0.84), q(0.95)
}

type volPending struct {
	x      []float64
	pred   float64
	naive  float64
	acc    float64
	bars   int
	entry  float64
	sigmaH float64 // forecast horizon sigma, 0 before the model is used
	band   [4]float64
}

// NewVolForecaster forecasts mean variance over the next h bars using trailing
// windows comps (in bars), e.g. {1, 6, 24, 120} on 1h bars.
func NewVolForecaster(h int, comps []int) *VolForecaster {
	maxLag := h
	for _, c := range comps {
		maxLag = max(maxLag, c)
	}
	d := len(comps) + 1
	P := make([][]float64, d)
	for i := range P {
		P[i] = make([]float64, d)
		P[i][i] = 100
	}
	return &VolForecaster{h: h, comps: comps, maxLag: maxLag, theta: make([]float64, d), P: P, lambda: 0.9995, resVar: 0.25}
}

// parkinsonVar is the per-bar Parkinson variance of log price.
func parkinsonVar(high, low float64) float64 {
	if high <= 0 || low <= 0 || high < low {
		return 0
	}
	r := math.Log(high / low)
	return r * r / (4 * math.Ln2)
}

const varFloor = 1e-12

func (v *VolForecaster) meanLast(k int) float64 {
	k = min(k, len(v.vars))
	s := 0.0
	for _, x := range v.vars[len(v.vars)-k:] {
		s += x
	}
	return math.Max(s/float64(k), varFloor)
}

// Forecast is the volatility outlook for the next h bars.
type Forecast struct {
	Ready        bool    `json:"ready"`
	Trusted      bool    `json:"trusted"` // beats naive persistence out-of-sample
	Horizon      int     `json:"horizon_bars"`
	SigmaBar     float64 `json:"sigma_per_bar"` // forecast per-bar log-return volatility
	SigmaHorizon float64 `json:"sigma_horizon"` // over the whole horizon
	CurrentSigma float64 `json:"current_sigma_per_bar"`
	Expansion    float64 `json:"expansion"`  // forecast / trailing-h realised volatility
	R2           float64 `json:"r2_oos"`     // out-of-sample R^2 in log variance
	R2Naive      float64 `json:"r2_naive"`   // same for the persistence forecast
	Coverage68   float64 `json:"coverage68"` // share of horizon closes inside the +/-1 sigma band (target 0.68)
	Coverage90   float64 `json:"coverage90"` // share inside the 5%..95% band (target 0.90)
	Samples      int     `json:"samples"`
	Z05          float64 `json:"band_z05"` // band quantiles of the close after the horizon, in horizon-sigma units
	Z16          float64 `json:"band_z16"`
	Z84          float64 `json:"band_z84"`
	Z95          float64 `json:"band_z95"`
}

// Update consumes a bar and returns the forecast made at its close.
func (v *VolForecaster) Update(high, low, close float64) Forecast {
	pv := parkinsonVar(high, low)
	// Resolve pending forecasts with this bar's variance.
	keep := v.pend[:0]
	for _, p := range v.pend {
		p.acc += pv
		p.bars++
		if p.bars < v.h {
			keep = append(keep, p)
			continue
		}
		y := math.Log(math.Max(p.acc/float64(v.h), varFloor))
		v.learn(p.x, y)
		if p.sigmaH > 0 && p.entry > 0 && close > 0 {
			z := math.Log(close/p.entry) / p.sigmaH
			v.banded++
			if z >= p.band[1] && z <= p.band[2] {
				v.in68++
			}
			if z >= p.band[0] && z <= p.band[3] {
				v.in90++
			}
			if len(v.zs) < zCap {
				v.zs = append(v.zs, z)
			} else {
				v.zs[v.zHead] = z
				v.zHead = (v.zHead + 1) % zCap
			}
		}
		if v.n > 200 { // score only once the regression has settled
			v.scored++
			v.sseModel += (y - p.pred) * (y - p.pred)
			v.sseNaive += (y - p.naive) * (y - p.naive)
			v.sumY += y
			v.sumY2 += y * y
		}
		e := y - p.pred
		v.resVar += 0.01 * (e*e - v.resVar)
	}
	v.pend = keep

	v.vars = append(v.vars, pv)
	if len(v.vars) > 4*v.maxLag {
		v.vars = append(v.vars[:0], v.vars[len(v.vars)-v.maxLag:]...)
	}
	f := Forecast{Horizon: v.h, Samples: v.n}
	if len(v.vars) < v.maxLag {
		return f
	}
	x := make([]float64, len(v.comps)+1)
	x[0] = 1
	for i, c := range v.comps {
		x[i+1] = math.Log(v.meanLast(c))
	}
	naive := math.Log(v.meanLast(v.h))
	pred := naive
	if v.n > 200 {
		pred = dot(v.theta, x)
	}

	// Mean of a log-normal: exp(mu + s^2/2).
	meanVar := math.Exp(pred + 0.5*v.resVar)
	f.Ready = true
	f.SigmaBar = math.Sqrt(meanVar)
	f.SigmaHorizon = f.SigmaBar * math.Sqrt(float64(v.h))
	f.Z05, f.Z16, f.Z84, f.Z95 = v.bandZ()
	vp := volPending{x: x, pred: pred, naive: naive, entry: close, band: [4]float64{f.Z05, f.Z16, f.Z84, f.Z95}}
	if v.n > 200 {
		vp.sigmaH = f.SigmaHorizon
	}
	v.pend = append(v.pend, vp)
	f.CurrentSigma = math.Sqrt(math.Exp(naive))
	if f.CurrentSigma > 0 {
		f.Expansion = f.SigmaBar / f.CurrentSigma
	}
	if v.scored > 30 {
		mean := v.sumY / float64(v.scored)
		sst := v.sumY2 - float64(v.scored)*mean*mean
		if sst > 0 {
			f.R2 = 1 - v.sseModel/sst
			f.R2Naive = 1 - v.sseNaive/sst
		}
		f.Trusted = f.R2 > f.R2Naive && f.R2 > 0
	}
	if v.banded > 0 {
		f.Coverage68 = float64(v.in68) / float64(v.banded)
		f.Coverage90 = float64(v.in90) / float64(v.banded)
	}
	return f
}

func (v *VolForecaster) learn(x []float64, y float64) {
	d := len(x)
	Px := make([]float64, d)
	for i := 0; i < d; i++ {
		Px[i] = dot(v.P[i], x)
	}
	den := v.lambda + dot(x, Px)
	if den <= 0 {
		return
	}
	k := make([]float64, d)
	for i := range k {
		k[i] = Px[i] / den
	}
	e := y - dot(v.theta, x)
	for i := range v.theta {
		v.theta[i] += k[i] * e
	}
	// P = (P - k (x'P)) / lambda; x'P = Px' since P is symmetric.
	for i := 0; i < d; i++ {
		for j := 0; j < d; j++ {
			v.P[i][j] = (v.P[i][j] - k[i]*Px[j]) / v.lambda
		}
	}
	v.n++
}

func dot(a, b []float64) float64 {
	s := 0.0
	for i := range a {
		s += a[i] * b[i]
	}
	return s
}
