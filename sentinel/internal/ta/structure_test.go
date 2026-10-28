package ta

import (
	"math"
	"testing"
)

func TestSwingsConfirmOnlyAfterRightBars(t *testing.T) {
	s := NewSwings(2, 2, 10)
	highs := []float64{10, 11, 15, 12, 11, 10, 9}
	for i, h := range highs {
		s.Update(h, h-1, 50)
		// The 15 high at index 2 can only be confirmed after two lower bars (index 4).
		if i < 4 && len(s.Highs) > 0 {
			t.Fatalf("swing high confirmed early at bar %d", i)
		}
	}
	if len(s.Highs) != 1 || s.Highs[0].Price != 15 || s.Highs[0].Index != 2 {
		t.Fatalf("highs = %+v", s.Highs)
	}
}

func TestBullishDivergence(t *testing.T) {
	s := NewSwings(1, 1, 10)
	// Two swing lows: price makes a lower low (90 < 95) while RSI makes a higher low (35 > 25).
	type bar struct{ h, l, rsi float64 }
	seq := []bar{{105, 100, 50}, {100, 95, 25}, {106, 101, 50}, {104, 99, 45}, {95, 90, 35}, {101, 96, 50}}
	got := 0
	for _, b := range seq {
		s.Update(b.h, b.l, b.rsi)
		if d := s.Divergence(40, 45, 55); d != 0 {
			got = d
		}
	}
	if got != 1 {
		t.Fatalf("divergence = %d, want bullish", got)
	}
}

func TestLevelsClusterAndSort(t *testing.T) {
	s := NewSwings(1, 1, 10)
	s.Highs = []Pivot{{Price: 110}, {Price: 110.4}, {Price: 130}}
	s.Lows = []Pivot{{Price: 90}, {Price: 89.8}}
	lv := s.Levels(100, 1)
	if len(lv) != 3 {
		t.Fatalf("levels = %+v", lv)
	}
	if lv[0].Kind != "support" || math.Abs(lv[0].Price-89.9) > 1e-9 || lv[0].Touches != 2 {
		t.Fatalf("nearest = %+v", lv[0])
	}
	if lv[1].Kind != "resistance" || lv[1].Touches != 2 {
		t.Fatalf("second = %+v", lv[1])
	}
}

func TestSupertrendFlips(t *testing.T) {
	st := NewSupertrend(3, 1)
	flips := 0
	price := 100.0
	for i := 0; i < 60; i++ {
		if i < 30 {
			price += 1
		} else {
			price -= 1.5
		}
		st.Update(price+0.5, price-0.5, price)
		if st.Flipped {
			flips++
		}
	}
	if st.Dir != -1 || flips < 1 {
		t.Fatalf("dir %d flips %d", st.Dir, flips)
	}
}

func TestEfficiencyRatioAndStreak(t *testing.T) {
	er := NewEfficiencyRatio(5)
	for _, v := range []float64{1, 2, 3, 4, 5, 6} {
		er.Update(v)
	}
	if math.Abs(er.Value()-1) > 1e-12 {
		t.Fatalf("straight line ER = %v", er.Value())
	}
	var s Streak
	for _, v := range []float64{1, 2, 3, 2, 1, 0} {
		s.Update(v)
	}
	if s.Value != -3 {
		t.Fatalf("streak = %d", s.Value)
	}
}

func TestHMAAndPercentRank(t *testing.T) {
	h := NewHMA(9)
	for i := 0; i < 50; i++ {
		h.Update(float64(i))
	}
	// The Hull MA tracks a linear trend with almost no lag.
	if !h.Ready() || math.Abs(h.Value()-49) > 0.5 {
		t.Fatalf("hma = %v", h.Value())
	}
	p := NewPercentRank(5)
	for _, v := range []float64{5, 4, 3, 2, 10} {
		p.Update(v)
	}
	if p.Value() != 1 {
		t.Fatalf("rank of the max = %v", p.Value())
	}
}

func TestVWAPResetsDaily(t *testing.T) {
	v := NewSessionVWAP()
	day := int64(86400)
	v.Update(unix(0), 10, 10, 10, 1)
	v.Update(unix(3600), 20, 20, 20, 1)
	if v.Value != 15 {
		t.Fatalf("vwap = %v", v.Value)
	}
	v.Update(unix(day), 30, 30, 30, 1)
	if v.Value != 30 || v.Bars() != 1 {
		t.Fatalf("after reset vwap = %v bars %d", v.Value, v.Bars())
	}
}
