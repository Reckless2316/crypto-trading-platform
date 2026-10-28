package ta

import (
	"math"
	"sort"
)

// Pivot is a confirmed swing high or low.
type Pivot struct {
	Index int     // bar index of the swing point
	Price float64 // high for swing highs, low for swing lows
	Osc   float64 // oscillator (RSI) value at the swing point
	High  bool
}

// Swings detects fractal swing points: a bar whose high (low) is the strict
// extreme of the `left` bars before and the `right` bars after it. A pivot is
// only confirmed `right` bars later, so detection never looks ahead.
type Swings struct {
	left, right int
	highs, lows *ring
	oscs        *ring
	n           int
	Highs       []Pivot // most recent last, capped
	Lows        []Pivot
	maxKeep     int
	NewHigh     bool // a swing high was confirmed on the last update
	NewLow      bool
}

func NewSwings(left, right, keep int) *Swings {
	w := left + right + 1
	return &Swings{left: left, right: right, highs: newRing(w), lows: newRing(w), oscs: newRing(w), maxKeep: keep}
}

// Update adds a bar with an oscillator reading (pass 0 if not needed).
func (s *Swings) Update(high, low, osc float64) {
	s.highs.push(high)
	s.lows.push(low)
	s.oscs.push(osc)
	s.n++
	s.NewHigh, s.NewLow = false, false
	if !s.highs.full() {
		return
	}
	c := s.left
	ch, cl := s.highs.at(c), s.lows.at(c)
	isHigh, isLow := true, true
	for i := 0; i < s.highs.len(); i++ {
		if i == c {
			continue
		}
		if s.highs.at(i) >= ch {
			isHigh = false
		}
		if s.lows.at(i) <= cl {
			isLow = false
		}
	}
	idx := s.n - 1 - s.right
	if isHigh {
		s.Highs = appendCapped(s.Highs, Pivot{Index: idx, Price: ch, Osc: s.oscs.at(c), High: true}, s.maxKeep)
		s.NewHigh = true
	}
	if isLow {
		s.Lows = appendCapped(s.Lows, Pivot{Index: idx, Price: cl, Osc: s.oscs.at(c)}, s.maxKeep)
		s.NewLow = true
	}
}

func appendCapped(p []Pivot, v Pivot, keep int) []Pivot {
	p = append(p, v)
	if len(p) > keep {
		p = p[len(p)-keep:]
	}
	return p
}

// Structure classifies the last two swing highs and lows:
// +1 higher highs and higher lows, -1 lower highs and lower lows, 0 mixed/unknown.
func (s *Swings) Structure() int {
	if len(s.Highs) < 2 || len(s.Lows) < 2 {
		return 0
	}
	h1, h2 := s.Highs[len(s.Highs)-2], s.Highs[len(s.Highs)-1]
	l1, l2 := s.Lows[len(s.Lows)-2], s.Lows[len(s.Lows)-1]
	switch {
	case h2.Price > h1.Price && l2.Price > l1.Price:
		return 1
	case h2.Price < h1.Price && l2.Price < l1.Price:
		return -1
	}
	return 0
}

// Divergence reports regular RSI divergence confirmed on the last update:
// +1 bullish (price lower low, oscillator higher low), -1 bearish (price higher
// high, oscillator lower high), 0 none. maxGap bounds the bar distance between
// the two pivots; oscMin/oscMax gate bullish divergences to oversold-ish and
// bearish to overbought-ish territory.
func (s *Swings) Divergence(maxGap int, bullOscMax, bearOscMin float64) int {
	if s.NewLow && len(s.Lows) >= 2 {
		a, b := s.Lows[len(s.Lows)-2], s.Lows[len(s.Lows)-1]
		if b.Index-a.Index <= maxGap && b.Price < a.Price && b.Osc > a.Osc && b.Osc < bullOscMax {
			return 1
		}
	}
	if s.NewHigh && len(s.Highs) >= 2 {
		a, b := s.Highs[len(s.Highs)-2], s.Highs[len(s.Highs)-1]
		if b.Index-a.Index <= maxGap && b.Price > a.Price && b.Osc < a.Osc && b.Osc > bearOscMin {
			return -1
		}
	}
	return 0
}

// Level is a support or resistance zone built from clustered swing points.
type Level struct {
	Price   float64 `json:"price"`
	Touches int     `json:"touches"`
	Kind    string  `json:"kind"` // "support" or "resistance" relative to the reference price
}

// Levels clusters recent swing prices within tol (absolute price units, e.g.
// 0.5*ATR) and returns the zones sorted by distance from ref.
func (s *Swings) Levels(ref, tol float64) []Level {
	pts := make([]float64, 0, len(s.Highs)+len(s.Lows))
	for _, p := range s.Highs {
		pts = append(pts, p.Price)
	}
	for _, p := range s.Lows {
		pts = append(pts, p.Price)
	}
	if len(pts) == 0 || tol <= 0 {
		return nil
	}
	sort.Float64s(pts)
	var out []Level
	start := 0
	for i := 1; i <= len(pts); i++ {
		if i == len(pts) || pts[i]-pts[i-1] > tol {
			sum := 0.0
			for _, x := range pts[start:i] {
				sum += x
			}
			lv := Level{Price: sum / float64(i-start), Touches: i - start}
			if lv.Price <= ref {
				lv.Kind = "support"
			} else {
				lv.Kind = "resistance"
			}
			out = append(out, lv)
			start = i
		}
	}
	sort.Slice(out, func(i, j int) bool {
		return math.Abs(out[i].Price-ref) < math.Abs(out[j].Price-ref)
	})
	return out
}

// Streak counts consecutive closes in the same direction (positive for up bars).
type Streak struct {
	prevClose float64
	n         int
	Value     int
}

func (s *Streak) Update(close float64) int {
	s.n++
	if s.n > 1 {
		switch {
		case close > s.prevClose:
			if s.Value > 0 {
				s.Value++
			} else {
				s.Value = 1
			}
		case close < s.prevClose:
			if s.Value < 0 {
				s.Value--
			} else {
				s.Value = -1
			}
		default:
			s.Value = 0
		}
	}
	s.prevClose = close
	return s.Value
}
