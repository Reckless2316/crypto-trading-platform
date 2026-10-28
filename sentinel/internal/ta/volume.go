package ta

import (
	"math"
	"time"
)

// OBV is on-balance volume (TA-Lib convention: starts at the first bar's volume).
type OBV struct {
	n         int
	prevClose float64
	value     float64
}

func NewOBV() *OBV { return &OBV{} }

func (o *OBV) Update(close, volume float64) float64 {
	o.n++
	switch {
	case o.n == 1:
		o.value = volume
	case close > o.prevClose:
		o.value += volume
	case close < o.prevClose:
		o.value -= volume
	}
	o.prevClose = close
	return o.value
}
func (o *OBV) Value() float64 { return o.value }

// CMF is the Chaikin money flow over period bars, in [-1, 1].
type CMF struct {
	mfv, vol *ring
	sumMFV   float64
	sumVol   float64
	value    float64
}

func NewCMF(period int) *CMF { return &CMF{mfv: newRing(period), vol: newRing(period)} }

func (c *CMF) Update(high, low, close, volume float64) float64 {
	mult := 0.0
	if r := high - low; r > 0 {
		mult = ((close - low) - (high - close)) / r
	}
	m := mult * volume
	if old, full := c.mfv.push(m); full {
		c.sumMFV -= old
	}
	if old, full := c.vol.push(volume); full {
		c.sumVol -= old
	}
	c.sumMFV += m
	c.sumVol += volume
	if c.sumVol > 0 {
		c.value = c.sumMFV / c.sumVol
	}
	return c.value
}
func (c *CMF) Ready() bool    { return c.mfv.full() }
func (c *CMF) Value() float64 { return c.value }

// SessionVWAP resets at each UTC day boundary and tracks the volume-weighted
// standard deviation, so deviation can be expressed in band units.
type SessionVWAP struct {
	day        int64
	pv, v, pv2 float64
	Value, SD  float64
	bars       int
}

func NewSessionVWAP() *SessionVWAP { return &SessionVWAP{day: -1} }

func (s *SessionVWAP) Update(t time.Time, high, low, close, volume float64) {
	d := t.UTC().Unix() / 86400
	if d != s.day {
		s.day = d
		s.pv, s.v, s.pv2, s.bars = 0, 0, 0, 0
	}
	tp := (high + low + close) / 3
	s.pv += tp * volume
	s.pv2 += tp * tp * volume
	s.v += volume
	s.bars++
	if s.v > 0 {
		s.Value = s.pv / s.v
		varc := s.pv2/s.v - s.Value*s.Value
		if varc < 0 {
			varc = 0
		}
		s.SD = math.Sqrt(varc)
	} else {
		s.Value = tp
		s.SD = 0
	}
}

// Deviation is (price - vwap) in VWAP standard deviations (0 when undefined).
func (s *SessionVWAP) Deviation(price float64) float64 {
	if s.SD == 0 {
		return 0
	}
	return (price - s.Value) / s.SD
}

// Bars is the number of bars in the current session.
func (s *SessionVWAP) Bars() int { return s.bars }

// RelativeVolume is volume divided by its SMA.
type RelativeVolume struct {
	sma   *SMA
	value float64
}

func NewRelativeVolume(period int) *RelativeVolume { return &RelativeVolume{sma: NewSMA(period)} }

// Update uses the average of the previous period bars, so a surge is measured
// against the baseline that preceded it.
func (r *RelativeVolume) Update(volume float64) float64 {
	if r.sma.Ready() && r.sma.Value() > 0 {
		r.value = volume / r.sma.Value()
	} else {
		r.value = 1
	}
	r.sma.Update(volume)
	return r.value
}
func (r *RelativeVolume) Ready() bool    { return r.sma.Ready() }
func (r *RelativeVolume) Value() float64 { return r.value }
