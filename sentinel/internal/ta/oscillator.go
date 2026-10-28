package ta

import "math"

// RSI is Wilder's relative strength index (TA-Lib seeding: the first output
// uses the simple average of the first period gains and losses).
type RSI struct {
	period           int
	prev             float64
	n                int
	sumGain, sumLoss float64
	avgGain, avgLoss float64
	value            float64
}

func NewRSI(period int) *RSI { return &RSI{period: period} }

func (r *RSI) Update(v float64) float64 {
	r.n++
	if r.n == 1 {
		r.prev = v
		return r.value
	}
	ch := v - r.prev
	r.prev = v
	gain, loss := 0.0, 0.0
	if ch > 0 {
		gain = ch
	} else {
		loss = -ch
	}
	p := float64(r.period)
	switch {
	case r.n <= r.period:
		r.sumGain += gain
		r.sumLoss += loss
		return r.value
	case r.n == r.period+1:
		r.avgGain = (r.sumGain + gain) / p
		r.avgLoss = (r.sumLoss + loss) / p
	default:
		r.avgGain = (r.avgGain*(p-1) + gain) / p
		r.avgLoss = (r.avgLoss*(p-1) + loss) / p
	}
	if s := r.avgGain + r.avgLoss; s > 0 {
		r.value = 100 * r.avgGain / s
	} else {
		r.value = 50
	}
	return r.value
}
func (r *RSI) Ready() bool    { return r.n > r.period }
func (r *RSI) Value() float64 { return r.value }

// MACD is the moving average convergence/divergence oscillator.
type MACD struct {
	fast, slow, signal *EMA
	Line, Signal, Hist float64
	prevHist           float64
	ready              bool
}

func NewMACD(fast, slow, signal int) *MACD {
	return &MACD{fast: NewEMA(fast), slow: NewEMA(slow), signal: NewEMA(signal)}
}

func (m *MACD) Update(v float64) {
	f := m.fast.Update(v)
	s := m.slow.Update(v)
	if !m.slow.Ready() {
		return
	}
	m.Line = f - s
	m.Signal = m.signal.Update(m.Line)
	if m.signal.Ready() {
		m.prevHist = m.Hist
		m.Hist = m.Line - m.Signal
		if !m.ready {
			m.prevHist = m.Hist
		}
		m.ready = true
	}
}
func (m *MACD) Ready() bool { return m.ready }

// HistDelta is the change of the histogram over the last bar (momentum of momentum).
func (m *MACD) HistDelta() float64 { return m.Hist - m.prevHist }

// rollingMinMax keeps the extrema of the last period values.
type rollingMinMax struct{ win *ring }

func newRollingMinMax(period int) *rollingMinMax { return &rollingMinMax{win: newRing(period)} }

func (r *rollingMinMax) push(v float64) { r.win.push(v) }
func (r *rollingMinMax) minmax() (lo, hi float64) {
	lo, hi = math.Inf(1), math.Inf(-1)
	for i := 0; i < r.win.len(); i++ {
		x := r.win.at(i)
		lo = math.Min(lo, x)
		hi = math.Max(hi, x)
	}
	return
}

// StochRSI is TA-Lib's STOCHRSI: a fast stochastic (K over kPeriod, D = SMA(K, dPeriod)) of RSI.
type StochRSI struct {
	rsi    *RSI
	mm     *rollingMinMax
	d      *SMA
	K, D   float64
	prevK  float64
	prevD  float64
	kReady bool
}

func NewStochRSI(rsiPeriod, kPeriod, dPeriod int) *StochRSI {
	return &StochRSI{rsi: NewRSI(rsiPeriod), mm: newRollingMinMax(kPeriod), d: NewSMA(dPeriod)}
}

func (s *StochRSI) Update(v float64) {
	r := s.rsi.Update(v)
	if !s.rsi.Ready() {
		return
	}
	s.mm.push(r)
	if !s.mm.win.full() {
		return
	}
	lo, hi := s.mm.minmax()
	k := 0.0
	if hi > lo {
		k = 100 * (r - lo) / (hi - lo)
	}
	s.prevK, s.prevD = s.K, s.D
	s.K = k
	s.D = s.d.Update(k)
	if !s.kReady {
		s.prevK = k
		s.kReady = true
	}
}
func (s *StochRSI) Ready() bool { return s.d.Ready() }

// CrossUp reports K crossing above D on the last update.
func (s *StochRSI) CrossUp() bool { return s.prevK <= s.prevD && s.K > s.D }

// CrossDown reports K crossing below D on the last update.
func (s *StochRSI) CrossDown() bool { return s.prevK >= s.prevD && s.K < s.D }

// MFI is the money flow index (TA-Lib definition: typical-price comparisons).
type MFI struct {
	period   int
	prevTP   float64
	n        int
	pos, neg *ring
	sumPos   float64
	sumNeg   float64
	value    float64
}

func NewMFI(period int) *MFI {
	return &MFI{period: period, pos: newRing(period), neg: newRing(period)}
}

func (m *MFI) Update(high, low, close, volume float64) float64 {
	tp := (high + low + close) / 3
	m.n++
	if m.n == 1 {
		m.prevTP = tp
		return m.value
	}
	flow := tp * volume
	p, q := 0.0, 0.0
	if tp > m.prevTP {
		p = flow
	} else if tp < m.prevTP {
		q = flow
	}
	m.prevTP = tp
	if old, full := m.pos.push(p); full {
		m.sumPos -= old
	}
	if old, full := m.neg.push(q); full {
		m.sumNeg -= old
	}
	m.sumPos += p
	m.sumNeg += q
	if m.pos.full() {
		if t := m.sumPos + m.sumNeg; t >= 1 {
			m.value = 100 * m.sumPos / t
		} else {
			m.value = 0
		}
	}
	return m.value
}
func (m *MFI) Ready() bool    { return m.pos.full() }
func (m *MFI) Value() float64 { return m.value }

// ROC is the percentage rate of change over period bars.
type ROC struct {
	win   *ring
	value float64
}

func NewROC(period int) *ROC { return &ROC{win: newRing(period + 1)} }

func (r *ROC) Update(v float64) float64 {
	r.win.push(v)
	if r.win.full() && r.win.at(0) != 0 {
		r.value = (v/r.win.at(0) - 1) * 100
	}
	return r.value
}
func (r *ROC) Ready() bool    { return r.win.full() }
func (r *ROC) Value() float64 { return r.value }
