package ta

import (
	"math"
	"sort"
)

// ATR is Wilder's average true range (TA-Lib seeding: the first true range is
// taken on the second bar; the first ATR is the SMA of the first period TRs).
type ATR struct {
	rma       *RMA
	prevClose float64
	n         int
	TR        float64
}

func NewATR(period int) *ATR { return &ATR{rma: NewRMA(period)} }

func (a *ATR) Update(high, low, close float64) float64 {
	a.n++
	if a.n == 1 {
		a.prevClose = close
		a.TR = high - low
		return 0
	}
	a.TR = trueRange(high, low, a.prevClose)
	a.prevClose = close
	return a.rma.Update(a.TR)
}
func (a *ATR) Ready() bool    { return a.rma.Ready() }
func (a *ATR) Value() float64 { return a.rma.Value() }

func trueRange(high, low, prevClose float64) float64 {
	return math.Max(high-low, math.Max(math.Abs(high-prevClose), math.Abs(low-prevClose)))
}

// ADX computes Wilder's directional movement system (+DI, -DI, ADX), with
// TA-Lib's seeding: DM/TR sums start from period-1 values, ADX from the mean of
// the first period DX values.
type ADX struct {
	period                int
	n                     int
	prevH, prevL, prevC   float64
	sumPDM, sumMDM, sumTR float64
	dxCount               int
	dxSum                 float64
	PlusDI, MinusDI       float64
	DX, Value             float64
	ready                 bool
}

func NewADX(period int) *ADX { return &ADX{period: period} }

func (a *ADX) Update(high, low, close float64) {
	a.n++
	if a.n == 1 {
		a.prevH, a.prevL, a.prevC = high, low, close
		return
	}
	up := high - a.prevH
	down := a.prevL - low
	pdm, mdm := 0.0, 0.0
	if up > 0 && up > down {
		pdm = up
	} else if down > 0 && down > up {
		mdm = down
	}
	tr := trueRange(high, low, a.prevC)
	a.prevH, a.prevL, a.prevC = high, low, close
	p := float64(a.period)
	if a.n <= a.period { // accumulate period-1 raw values (bars 2..period)
		a.sumPDM += pdm
		a.sumMDM += mdm
		a.sumTR += tr
		return
	}
	a.sumPDM = a.sumPDM - a.sumPDM/p + pdm
	a.sumMDM = a.sumMDM - a.sumMDM/p + mdm
	a.sumTR = a.sumTR - a.sumTR/p + tr
	if a.sumTR > 0 {
		a.PlusDI = 100 * a.sumPDM / a.sumTR
		a.MinusDI = 100 * a.sumMDM / a.sumTR
	}
	if s := a.PlusDI + a.MinusDI; s > 0 {
		a.DX = 100 * math.Abs(a.PlusDI-a.MinusDI) / s
	} else {
		a.DX = 0
	}
	if a.dxCount < a.period {
		a.dxCount++
		a.dxSum += a.DX
		if a.dxCount == a.period {
			a.Value = a.dxSum / p
			a.ready = true
		}
		return
	}
	a.Value = (a.Value*(p-1) + a.DX) / p
}
func (a *ADX) Ready() bool { return a.ready }

// StdDev is a rolling population standard deviation with mean.
type StdDev struct {
	win       *ring
	sum, sum2 float64
	count     int
	Mean, SD  float64
}

func NewStdDev(period int) *StdDev { return &StdDev{win: newRing(period)} }

func (s *StdDev) Update(v float64) float64 {
	old, full := s.win.push(v)
	s.sum += v
	s.sum2 += v * v
	if full {
		s.sum -= old
		s.sum2 -= old * old
	}
	s.count++
	if s.count%2048 == 0 { // bound cancellation drift
		s.sum, s.sum2 = 0, 0
		for i := 0; i < s.win.len(); i++ {
			x := s.win.at(i)
			s.sum += x
			s.sum2 += x * x
		}
	}
	n := float64(s.win.len())
	s.Mean = s.sum / n
	variance := s.sum2/n - s.Mean*s.Mean
	if variance < 0 {
		variance = 0
	}
	s.SD = math.Sqrt(variance)
	return s.SD
}
func (s *StdDev) Ready() bool { return s.win.full() }

// ZScore returns (v - mean) / sd for the latest value, 0 when sd is zero.
func (s *StdDev) ZScore(v float64) float64 {
	if s.SD == 0 {
		return 0
	}
	return (v - s.Mean) / s.SD
}

// Bollinger bands: SMA +/- k population standard deviations.
type Bollinger struct {
	sd                   *StdDev
	k                    float64
	Upper, Middle, Lower float64
}

func NewBollinger(period int, k float64) *Bollinger {
	return &Bollinger{sd: NewStdDev(period), k: k}
}

func (b *Bollinger) Update(v float64) {
	b.sd.Update(v)
	b.Middle = b.sd.Mean
	b.Upper = b.Middle + b.k*b.sd.SD
	b.Lower = b.Middle - b.k*b.sd.SD
}
func (b *Bollinger) Ready() bool { return b.sd.Ready() }

// PercentB is the position of v within the bands (0 = lower, 1 = upper).
func (b *Bollinger) PercentB(v float64) float64 {
	if w := b.Upper - b.Lower; w > 0 {
		return (v - b.Lower) / w
	}
	return 0.5
}

// Width is the band width relative to the middle band.
func (b *Bollinger) Width() float64 {
	if b.Middle == 0 {
		return 0
	}
	return (b.Upper - b.Lower) / b.Middle
}

// Keltner channel: EMA +/- mult * ATR.
type Keltner struct {
	ema                  *EMA
	atr                  *ATR
	mult                 float64
	Upper, Middle, Lower float64
}

func NewKeltner(period int, mult float64) *Keltner {
	return &Keltner{ema: NewEMA(period), atr: NewATR(period), mult: mult}
}

func (k *Keltner) Update(high, low, close float64) {
	k.Middle = k.ema.Update(close)
	a := k.atr.Update(high, low, close)
	k.Upper = k.Middle + k.mult*a
	k.Lower = k.Middle - k.mult*a
}
func (k *Keltner) Ready() bool { return k.ema.Ready() && k.atr.Ready() }

// Donchian channel over period bars.
type Donchian struct {
	hi, lo       *rollingMinMax
	Upper, Lower float64
}

func NewDonchian(period int) *Donchian {
	return &Donchian{hi: newRollingMinMax(period), lo: newRollingMinMax(period)}
}

func (d *Donchian) Update(high, low float64) {
	d.hi.push(high)
	d.lo.push(low)
	_, d.Upper = d.hi.minmax()
	d.Lower, _ = d.lo.minmax()
}
func (d *Donchian) Ready() bool { return d.hi.win.full() }

// Supertrend is the ATR trailing-stop trend filter.
type Supertrend struct {
	atr          *ATR
	mult         float64
	upper, lower float64
	prevClose    float64
	Dir          int // +1 uptrend, -1 downtrend
	Line         float64
	Flipped      bool
	started      bool
}

func NewSupertrend(period int, mult float64) *Supertrend {
	return &Supertrend{atr: NewATR(period), mult: mult, Dir: 1}
}

func (s *Supertrend) Update(high, low, close float64) {
	a := s.atr.Update(high, low, close)
	s.Flipped = false
	if !s.atr.Ready() {
		s.prevClose = close
		return
	}
	mid := (high + low) / 2
	bu := mid + s.mult*a
	bl := mid - s.mult*a
	if !s.started {
		s.upper, s.lower = bu, bl
		s.started = true
		if close < mid {
			s.Dir = -1
		}
	} else {
		if bu < s.upper || s.prevClose > s.upper {
			s.upper = bu
		}
		if bl > s.lower || s.prevClose < s.lower {
			s.lower = bl
		}
		if s.Dir == 1 && close < s.lower {
			s.Dir, s.Flipped = -1, true
		} else if s.Dir == -1 && close > s.upper {
			s.Dir, s.Flipped = 1, true
		}
	}
	if s.Dir == 1 {
		s.Line = s.lower
	} else {
		s.Line = s.upper
	}
	s.prevClose = close
}
func (s *Supertrend) Ready() bool { return s.started }

// EfficiencyRatio is Kaufman's |net change| / sum |bar changes| over period bars:
// near 1 in clean trends, near 0 in chop.
type EfficiencyRatio struct {
	win   *ring
	value float64
}

func NewEfficiencyRatio(period int) *EfficiencyRatio {
	return &EfficiencyRatio{win: newRing(period + 1)}
}

func (e *EfficiencyRatio) Update(v float64) float64 {
	e.win.push(v)
	if !e.win.full() {
		return e.value
	}
	vol := 0.0
	for i := 1; i < e.win.len(); i++ {
		vol += math.Abs(e.win.at(i) - e.win.at(i-1))
	}
	if vol == 0 {
		e.value = 0
	} else {
		e.value = math.Abs(v-e.win.at(0)) / vol
	}
	return e.value
}
func (e *EfficiencyRatio) Ready() bool    { return e.win.full() }
func (e *EfficiencyRatio) Value() float64 { return e.value }

// LinRegSlope is the least-squares slope of the last period values (TA-Lib LINEARREG_SLOPE).
type LinRegSlope struct {
	win   *ring
	value float64
}

func NewLinRegSlope(period int) *LinRegSlope { return &LinRegSlope{win: newRing(period)} }

func (l *LinRegSlope) Update(v float64) float64 {
	l.win.push(v)
	if !l.win.full() {
		return l.value
	}
	n := float64(l.win.len())
	sx := n * (n - 1) / 2
	sxx := n * (n - 1) * (2*n - 1) / 6
	sy, sxy := 0.0, 0.0
	for i := 0; i < l.win.len(); i++ {
		y := l.win.at(i)
		sy += y
		sxy += float64(i) * y
	}
	d := n*sxx - sx*sx
	if d != 0 {
		l.value = (n*sxy - sx*sy) / d
	}
	return l.value
}
func (l *LinRegSlope) Ready() bool    { return l.win.full() }
func (l *LinRegSlope) Value() float64 { return l.value }

// Parkinson is a rolling high-low (Parkinson) volatility estimator, per bar,
// in log-return units.
type Parkinson struct {
	sma   *SMA
	value float64
}

func NewParkinson(period int) *Parkinson { return &Parkinson{sma: NewSMA(period)} }

func (p *Parkinson) Update(high, low float64) float64 {
	if high <= 0 || low <= 0 {
		return p.value
	}
	r := math.Log(high / low)
	v := p.sma.Update(r * r / (4 * math.Ln2))
	p.value = math.Sqrt(v)
	return p.value
}
func (p *Parkinson) Ready() bool    { return p.sma.Ready() }
func (p *Parkinson) Value() float64 { return p.value }

// PercentRank is the fraction of the last period values strictly below the newest value.
type PercentRank struct {
	win     *ring
	scratch []float64
	value   float64
}

func NewPercentRank(period int) *PercentRank {
	return &PercentRank{win: newRing(period), scratch: make([]float64, 0, period)}
}

func (p *PercentRank) Update(v float64) float64 {
	p.win.push(v)
	p.scratch = p.scratch[:0]
	for i := 0; i < p.win.len(); i++ {
		p.scratch = append(p.scratch, p.win.at(i))
	}
	sort.Float64s(p.scratch)
	below := sort.SearchFloat64s(p.scratch, v)
	if n := len(p.scratch); n > 1 {
		p.value = float64(below) / float64(n-1)
	}
	return p.value
}
func (p *PercentRank) Ready() bool    { return p.win.full() }
func (p *PercentRank) Value() float64 { return p.value }
