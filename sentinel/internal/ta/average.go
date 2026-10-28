// Package ta implements streaming (O(1) or O(window) per bar) technical
// indicators. Each indicator consumes one value or bar at a time and exposes
// Ready() plus its latest output, so the same code serves backtests, live
// websocket feeds and the MCP server without recomputing history.
//
// Seeding follows TA-Lib's conventions (SMA-seeded EMA, Wilder-seeded RSI/ATR,
// population standard deviation for Bollinger Bands) and the golden tests in
// this package compare outputs against values produced by the TA-Lib C library.
package ta

import "math"

// ring is a fixed-capacity FIFO of float64 values.
type ring struct {
	buf  []float64
	head int // index of the oldest element
	n    int
}

func newRing(capacity int) *ring {
	if capacity < 1 {
		capacity = 1
	}
	return &ring{buf: make([]float64, capacity)}
}

// push appends v and returns the evicted value (if the ring was full).
func (r *ring) push(v float64) (evicted float64, full bool) {
	if r.n < len(r.buf) {
		r.buf[(r.head+r.n)%len(r.buf)] = v
		r.n++
		return 0, false
	}
	evicted = r.buf[r.head]
	r.buf[r.head] = v
	r.head = (r.head + 1) % len(r.buf)
	return evicted, true
}

func (r *ring) len() int   { return r.n }
func (r *ring) full() bool { return r.n == len(r.buf) }

// at returns the i-th element, oldest first.
func (r *ring) at(i int) float64 { return r.buf[(r.head+i)%len(r.buf)] }

// last returns the most recent element.
func (r *ring) last() float64 { return r.at(r.n - 1) }

// SMA is a simple moving average.
type SMA struct {
	period int
	win    *ring
	sum    float64
	value  float64
	count  int
}

func NewSMA(period int) *SMA { return &SMA{period: period, win: newRing(period)} }

func (s *SMA) Update(v float64) float64 {
	old, full := s.win.push(v)
	s.sum += v
	if full {
		s.sum -= old
	}
	s.count++
	if s.win.full() {
		// Re-sum every 4096 updates to bound floating-point drift.
		if s.count%4096 == 0 {
			s.sum = 0
			for i := 0; i < s.win.len(); i++ {
				s.sum += s.win.at(i)
			}
		}
		s.value = s.sum / float64(s.period)
	}
	return s.value
}
func (s *SMA) Ready() bool    { return s.win.full() }
func (s *SMA) Value() float64 { return s.value }

// EMA is an exponential moving average seeded with the SMA of the first period values.
type EMA struct {
	period int
	k      float64
	seed   float64
	n      int
	value  float64
}

func NewEMA(period int) *EMA { return &EMA{period: period, k: 2 / float64(period+1)} }

func (e *EMA) Update(v float64) float64 {
	e.n++
	switch {
	case e.n < e.period:
		e.seed += v
	case e.n == e.period:
		e.value = (e.seed + v) / float64(e.period)
	default:
		e.value += e.k * (v - e.value)
	}
	return e.value
}
func (e *EMA) Ready() bool    { return e.n >= e.period }
func (e *EMA) Value() float64 { return e.value }

// RMA is Wilder's smoothing (alpha = 1/period), seeded with an SMA.
type RMA struct {
	period int
	seed   float64
	n      int
	value  float64
}

func NewRMA(period int) *RMA { return &RMA{period: period} }

func (r *RMA) Update(v float64) float64 {
	r.n++
	switch {
	case r.n < r.period:
		r.seed += v
	case r.n == r.period:
		r.value = (r.seed + v) / float64(r.period)
	default:
		r.value = (r.value*float64(r.period-1) + v) / float64(r.period)
	}
	return r.value
}
func (r *RMA) Ready() bool    { return r.n >= r.period }
func (r *RMA) Value() float64 { return r.value }

// WMA is a linearly weighted moving average (weights 1..period, newest heaviest).
type WMA struct {
	period int
	win    *ring
	denom  float64
	value  float64
}

func NewWMA(period int) *WMA {
	return &WMA{period: period, win: newRing(period), denom: float64(period*(period+1)) / 2}
}

func (w *WMA) Update(v float64) float64 {
	w.win.push(v)
	if w.win.full() {
		s := 0.0
		for i := 0; i < w.period; i++ {
			s += float64(i+1) * w.win.at(i)
		}
		w.value = s / w.denom
	}
	return w.value
}
func (w *WMA) Ready() bool    { return w.win.full() }
func (w *WMA) Value() float64 { return w.value }

// HMA is the Hull moving average: WMA(2*WMA(n/2) - WMA(n), sqrt(n)).
// It is the trend filter used by the CoinQuant HMA strategy.
type HMA struct {
	half, full, smooth *WMA
	value              float64
}

func NewHMA(period int) *HMA {
	h := period / 2
	if h < 1 {
		h = 1
	}
	s := int(math.Round(math.Sqrt(float64(period))))
	if s < 1 {
		s = 1
	}
	return &HMA{half: NewWMA(h), full: NewWMA(period), smooth: NewWMA(s)}
}

func (h *HMA) Update(v float64) float64 {
	a := h.half.Update(v)
	b := h.full.Update(v)
	if h.full.Ready() {
		h.value = h.smooth.Update(2*a - b)
	}
	return h.value
}
func (h *HMA) Ready() bool    { return h.smooth.Ready() }
func (h *HMA) Value() float64 { return h.value }

// KAMA is Kaufman's adaptive moving average. Matches TA-Lib's seeding: the
// first output uses the previous close as the prior average.
type KAMA struct {
	period     int
	fast, slow float64
	win        *ring // closes, period+1 long
	value      float64
	ready      bool
}

func NewKAMA(period, fastPeriod, slowPeriod int) *KAMA {
	return &KAMA{
		period: period,
		fast:   2 / float64(fastPeriod+1),
		slow:   2 / float64(slowPeriod+1),
		win:    newRing(period + 1),
	}
}

func (k *KAMA) Update(v float64) float64 {
	k.win.push(v)
	if !k.win.full() {
		return k.value
	}
	if !k.ready {
		k.value = k.win.at(k.period - 1)
		k.ready = true
	}
	change := math.Abs(v - k.win.at(0))
	vol := 0.0
	for i := 1; i <= k.period; i++ {
		vol += math.Abs(k.win.at(i) - k.win.at(i-1))
	}
	er := 1.0
	if vol != 0 {
		er = change / vol
	}
	sc := er*(k.fast-k.slow) + k.slow
	sc *= sc
	k.value += sc * (v - k.value)
	return k.value
}
func (k *KAMA) Ready() bool    { return k.ready }
func (k *KAMA) Value() float64 { return k.value }
