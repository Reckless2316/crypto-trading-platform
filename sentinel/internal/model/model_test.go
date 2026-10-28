package model

import (
	"encoding/json"
	"math"
	"math/rand"
	"testing"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/testutil"
)

// synthetic: y depends on x0 (signal) and not on x1 (noise).
func synthetic(n int, seed int64) ([][]float64, []float64) {
	rng := rand.New(rand.NewSource(seed))
	X := make([][]float64, n)
	y := make([]float64, n)
	for i := range X {
		x0, x1 := rng.Float64()*2-1, rng.Float64()*2-1
		X[i] = []float64{x0, x1}
		if rng.Float64() < Sigmoid(2*x0-0.5) {
			y[i] = 1
		}
	}
	return X, y
}

func TestClassifierLearnsAndGateActivates(t *testing.T) {
	c := NewClassifier([]string{"signal", "noise"}, DefaultFTRL())
	X, y := synthetic(6000, 1)
	for i := range X {
		logit, p := c.Predict(X[i])
		c.Learn(X[i], logit, p, y[i])
	}
	r := c.Report()
	if r.AUC < 0.65 || r.BrierSkill <= 0 {
		t.Fatalf("expected real skill, got AUC %.3f BSS %.4f", r.AUC, r.BrierSkill)
	}
	st := c.Status(300)
	if !st.Active {
		t.Fatalf("gate should be active: %+v", st)
	}
	ex := c.Explain([]float64{1, 1}, 2)
	if ex[0].Feature != "signal" || ex[0].Weight <= 0 {
		t.Fatalf("explanation %+v", ex)
	}
	// Round-trip the learned state.
	b, err := json.Marshal(c)
	if err != nil {
		t.Fatal(err)
	}
	var c2 Classifier
	if err := json.Unmarshal(b, &c2); err != nil {
		t.Fatal(err)
	}
	_, p1 := c.Predict([]float64{0.5, 0})
	_, p2 := c2.Predict([]float64{0.5, 0})
	if math.Abs(p1-p2) > 1e-12 {
		t.Fatalf("restored model predicts %v vs %v", p2, p1)
	}
}

func TestGateStaysClosedOnNoise(t *testing.T) {
	c := NewClassifier([]string{"a", "b"}, DefaultFTRL())
	rng := rand.New(rand.NewSource(2))
	for i := 0; i < 5000; i++ {
		x := []float64{rng.Float64()*2 - 1, rng.Float64()*2 - 1}
		y := 0.0
		if rng.Float64() < 0.3 {
			y = 1
		}
		logit, p := c.Predict(x)
		c.Learn(x, logit, p, y)
	}
	if st := c.Status(300); st.Active {
		t.Fatalf("a model trained on noise must abstain: %+v", st)
	}
}

func TestMetricsAUC(t *testing.T) {
	m := NewMetrics()
	for _, x := range []struct{ p, y float64 }{{0.9, 1}, {0.8, 1}, {0.3, 0}, {0.2, 0}} {
		m.Add(x.p, x.y)
	}
	if m.AUC() != 1 {
		t.Fatalf("perfect ranking AUC = %v", m.AUC())
	}
	m2 := NewMetrics()
	for _, x := range []struct{ p, y float64 }{{0.5, 1}, {0.5, 0}} {
		m2.Add(x.p, x.y)
	}
	if m2.AUC() != 0.5 {
		t.Fatalf("tied AUC = %v", m2.AUC())
	}
}

func TestRidgeLogistic(t *testing.T) {
	X, y := synthetic(4000, 3)
	beta, err := FitRidgeLogistic(X, y, nil, 1, 30)
	if err != nil {
		t.Fatal(err)
	}
	if math.Abs(beta[1]-2) > 0.3 || math.Abs(beta[2]) > 0.25 || math.Abs(beta[0]+0.5) > 0.2 {
		t.Fatalf("coefficients %v, want about [-0.5 2 0]", beta)
	}
}

func TestVolForecasterBeatsNaiveOnGARCH(t *testing.T) {
	bars := testutil.GARCH(12000, time.Unix(0, 0), time.Hour, 7)
	v := NewVolForecaster(24, []int{1, 6, 24, 120})
	var f Forecast
	for _, b := range bars {
		f = v.Update(b.High, b.Low, b.Close)
	}
	if !f.Ready || !f.Trusted || f.R2 <= f.R2Naive {
		t.Fatalf("forecast should beat naive persistence on clustered volatility: %+v", f)
	}
	if math.Abs(f.Coverage68-0.68) > 0.05 || math.Abs(f.Coverage90-0.90) > 0.04 {
		t.Fatalf("bands not calibrated: 68%%->%.3f 90%%->%.3f", f.Coverage68, f.Coverage90)
	}
}
