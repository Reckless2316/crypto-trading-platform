package engine

import (
	"encoding/json"
	"math"
	"testing"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/testutil"
)

func TestAggregatorEmitsOnlyCompleteBars(t *testing.T) {
	a := NewAggregator(15*time.Minute, time.Hour)
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	var out []market.Bar
	for i := 0; i < 8; i++ {
		b := market.Bar{Time: start.Add(time.Duration(i) * 15 * time.Minute), Open: float64(i + 1), High: float64(i + 2), Low: float64(i), Close: float64(i + 1), Volume: 1}
		got := a.Add(b)
		if i%4 != 3 && len(got) != 0 {
			t.Fatalf("bar %d emitted an incomplete hour", i)
		}
		out = append(out, got...)
	}
	if len(out) != 2 {
		t.Fatalf("got %d hourly bars", len(out))
	}
	h := out[0]
	if !h.Time.Equal(start) || h.Open != 1 || h.Close != 4 || h.High != 5 || h.Low != 0 || h.Volume != 4 {
		t.Fatalf("bad hourly bar %+v", h)
	}
}

func TestAggregatorWeeksStartMonday(t *testing.T) {
	a := NewAggregator(24*time.Hour, 7*24*time.Hour)
	mon := time.Date(2026, 10, 26, 0, 0, 0, 0, time.UTC) // a Monday
	var out []market.Bar
	for i := 0; i < 7; i++ {
		out = append(out, a.Add(market.Bar{Time: mon.Add(time.Duration(i) * 24 * time.Hour), Open: 1, High: 1, Low: 1, Close: 1})...)
	}
	if len(out) != 1 || !out[0].Time.Equal(mon) {
		t.Fatalf("weekly bars %+v", out)
	}
}

func TestTripleBarrierLabels(t *testing.T) {
	cfg := DefaultConfig("X", market.MustTimeframe("1h"))
	cfg.Horizon = 3
	e := New(cfg, nil)
	x := make([]float64, len(featureNames))
	e.pend = []*pending{{entry: 100, sl: 1, x: x, pL: 0.3, pS: 0.3}}
	// Bar 1 touches the long target (100 + 2R = 102) without touching 99.
	e.resolve(market.Bar{Open: 100, High: 102.5, Low: 99.5, Close: 102})
	// The same bar also hit the short stop (101): both sides resolve.
	if len(e.outcomes) != 2 || e.outcomes[0].Side != 1 || e.outcomes[0].Result != 1 || e.outcomes[0].R != 2 ||
		e.outcomes[1].Side != -1 || e.outcomes[1].Result != -1 {
		t.Fatalf("outcomes %+v", e.outcomes)
	}
	if len(e.pend) != 0 {
		t.Fatalf("both sides resolved, pending %d", len(e.pend))
	}
	// When stop and target are touched in one bar, the stop is assumed first.
	e.outcomes = nil
	e.pend = []*pending{{entry: 100, sl: 1, x: x}}
	e.resolve(market.Bar{Open: 100, High: 103, Low: 98, Close: 100})
	for _, o := range e.outcomes {
		if o.Result != -1 {
			t.Fatalf("ambiguous bar must resolve as a stop: %+v", o)
		}
	}
	// Horizon timeout scores the close in R, net of costs.
	e.outcomes = nil
	e.pend = []*pending{{entry: 100, sl: 1, costR: 0.1, x: x}}
	for i := 0; i < 3; i++ {
		e.resolve(market.Bar{Open: 100, High: 100.5, Low: 99.5, Close: 100.4})
	}
	if len(e.outcomes) != 2 || e.outcomes[0].Result != 0 || math.Abs(e.outcomes[0].R-(0.4-0.1)) > 1e-9 {
		t.Fatalf("timeout outcomes %+v", e.outcomes)
	}
}

func TestTrackRecordStatus(t *testing.T) {
	if _, st := trackRecord(nil); st != "unproven" {
		t.Fatal(st)
	}
	win := make([]float64, 40)
	for i := range win {
		win[i] = 1 + 0.1*float64(i%3)
	}
	if tr, st := trackRecord(win); st != "proven" || tr.N != 40 || tr.HitRate != 1 {
		t.Fatalf("%v %+v", st, tr)
	}
	lose := make([]float64, 40)
	for i := range lose {
		lose[i] = -1 + 0.05*float64(i%2)
	}
	if _, st := trackRecord(lose); st != "negative" {
		t.Fatal(st)
	}
}

func runFixture(t *testing.T) (*Engine, []Snapshot) {
	t.Helper()
	bars := testutil.Fixture(t)
	e := New(DefaultConfig("BTC-USDT", market.MustTimeframe("1h")), nil)
	snaps := make([]Snapshot, 0, len(bars))
	for _, b := range bars {
		snaps = append(snaps, e.Update(b, nil))
	}
	return e, snaps
}

func TestEngineOnRealData(t *testing.T) {
	e, snaps := runFixture(t)
	last := snaps[len(snaps)-1]
	if !last.Ready || !last.Forecast.Ready {
		t.Fatalf("not ready after 2000 bars: %s", last.Warmup)
	}
	if last.Score < -100 || last.Score > 100 {
		t.Fatalf("score %v", last.Score)
	}
	if last.Forecast.Range90[0] >= last.Price || last.Forecast.Range90[1] <= last.Price {
		t.Fatalf("90%% range %v does not bracket price %v", last.Forecast.Range90, last.Price)
	}
	if len(last.Signal.Gate) != 6 {
		t.Fatalf("gate checks %d", len(last.Signal.Gate))
	}
	ready := 0
	for _, s := range snaps {
		if s.Ready {
			ready++
		}
		for _, l := range s.Confluence.Layers {
			if l.Long > layerCap[l.Layer]+1e-9 || l.Short > layerCap[l.Layer]+1e-9 {
				t.Fatalf("layer %s exceeds its cap: %+v", l.Layer, l)
			}
		}
	}
	if ready < 400 {
		t.Fatalf("only %d ready snapshots", ready)
	}
	if e.models.Long.Samples() < 400 {
		t.Fatalf("model learned from %d labels", e.models.Long.Samples())
	}
}

func TestEngineIsDeterministic(t *testing.T) {
	_, a := runFixture(t)
	_, b := runFixture(t)
	ja, _ := json.Marshal(a[len(a)-1])
	jb, _ := json.Marshal(b[len(b)-1])
	if string(ja) != string(jb) {
		t.Fatal("two runs over the same bars differ")
	}
}

func TestSnapshotsNeverRepaint(t *testing.T) {
	// The snapshot for bar i must be identical whether or not later bars exist.
	bars := testutil.Fixture(t)
	cut := 1700
	e1 := New(DefaultConfig("BTC-USDT", market.MustTimeframe("1h")), nil)
	var at Snapshot
	for _, b := range bars[:cut] {
		at = e1.Update(b, nil)
	}
	_, full := runFixture(t)
	ja, _ := json.Marshal(at)
	jb, _ := json.Marshal(full[cut-1])
	if string(ja) != string(jb) {
		t.Fatal("snapshot changed when later bars were added")
	}
}

func TestDeduplicationAndRefresh(t *testing.T) {
	e, snaps := runFixture(t)
	cfg := e.Config()
	last := map[int]Snapshot{}
	idx := map[int]int{}
	for i, s := range snaps {
		if !s.Signal.Emitted {
			continue
		}
		d := s.Signal.Direction
		if p, ok := last[d]; ok && i-idx[d] < cfg.CooldownBars && math.Abs(s.Price-p.Price) < cfg.CooldownATR*s.Base.ATR {
			t.Fatalf("repeat %s signal at bar %d (previous at %d)", s.Signal.Action, i, idx[d])
		}
		last[d], idx[d] = s, i
	}
	pending := len(e.pend)
	before := e.Last()
	e.SetDerivatives(market.Derivatives{Time: before.Time.Add(time.Hour), FundingRate: 0.001, Available: true})
	after := e.Refresh()
	if len(e.pend) != pending {
		t.Fatal("refresh created a label")
	}
	if !after.Time.Equal(before.Time) || after.Derivatives == nil {
		t.Fatal("refresh did not re-evaluate the same bar with the new context")
	}
	found := false
	for _, v := range after.Confluence.Votes {
		if v.Name == "funding_crowded" && v.Side == -1 {
			found = true
		}
	}
	if !found {
		t.Fatal("crowded funding should argue against longs after refresh")
	}
}

func TestFeatureVectorIsBounded(t *testing.T) {
	_, snaps := runFixture(t)
	for _, s := range snaps {
		if !s.Ready {
			continue
		}
		x := buildFeatures(s.Base, s.Mid, s.High, s.Regime, s.Context, s.Forecast.Expansion)
		for i, v := range x {
			if math.IsNaN(v) || math.Abs(v) > 1.0001 {
				t.Fatalf("feature %s = %v", featureNames[i], v)
			}
		}
	}
}
