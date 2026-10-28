// Package model is Sentinel's adaptive predictive layer: an online
// FTRL-Proximal logistic regression (McMahan et al., 2013) whose raw output is
// recalibrated by an online Platt scaler, plus prequential ("test, then train")
// evaluation so every reported metric is out-of-sample by construction.
package model

import (
	"encoding/json"
	"math"
	"sync"
)

// FTRLConfig holds the FTRL-Proximal hyper-parameters.
type FTRLConfig struct {
	Alpha float64 `json:"alpha"` // per-coordinate learning-rate scale
	Beta  float64 `json:"beta"`  // learning-rate smoothing
	L1    float64 `json:"l1"`    // sparsity
	L2    float64 `json:"l2"`    // shrinkage
}

// DefaultFTRL suits dense features in [-1, 1] and a few thousand examples.
func DefaultFTRL() FTRLConfig { return FTRLConfig{Alpha: 0.05, Beta: 1, L1: 0.05, L2: 2} }

// FTRL is a dense FTRL-Proximal logistic regression. Index 0 is the bias.
type FTRL struct {
	cfg  FTRLConfig
	z, n []float64
}

// NewFTRL creates a model for dim features (a bias term is added internally).
func NewFTRL(dim int, cfg FTRLConfig) *FTRL {
	return &FTRL{cfg: cfg, z: make([]float64, dim+1), n: make([]float64, dim+1)}
}

func (m *FTRL) weight(i int) float64 {
	z := m.z[i]
	l1, l2 := m.cfg.L1, m.cfg.L2
	if i == 0 {
		l1, l2 = 0, 0 // the bias carries the base rate and is not regularised
	}
	if math.Abs(z) <= l1 {
		return 0
	}
	sign := 1.0
	if z < 0 {
		sign = -1
	}
	return -(z - sign*l1) / ((m.cfg.Beta+math.Sqrt(m.n[i]))/m.cfg.Alpha + l2)
}

// Logit returns the raw linear score for x.
func (m *FTRL) Logit(x []float64) float64 {
	s := m.weight(0)
	for i, v := range x {
		if v != 0 {
			s += m.weight(i+1) * v
		}
	}
	return s
}

// Update performs one FTRL step on example (x, y) with y in {0, 1}.
func (m *FTRL) Update(x []float64, y float64) {
	p := sigmoid(m.Logit(x))
	g := p - y
	m.step(0, 1, g)
	for i, v := range x {
		if v != 0 {
			m.step(i+1, v, g)
		}
	}
}

func (m *FTRL) step(i int, xi, g float64) {
	gi := g * xi
	w := m.weight(i)
	sigma := (math.Sqrt(m.n[i]+gi*gi) - math.Sqrt(m.n[i])) / m.cfg.Alpha
	m.z[i] += gi - sigma*w
	m.n[i] += gi * gi
}

// Weights returns the current weights (bias first).
func (m *FTRL) Weights() []float64 {
	out := make([]float64, len(m.z))
	for i := range out {
		out[i] = m.weight(i)
	}
	return out
}

// Platt is an online two-parameter logistic recalibration p = s(a*logit + b),
// trained with SGD on the prequential predictions. Its step size decays with
// the sample count: triple-barrier labels overlap, so recent outcomes are
// strongly autocorrelated and a fixed step would chase local hit-rate noise.
type Platt struct {
	A, B float64
	N    float64
	lr   float64
}

func NewPlatt() *Platt { return &Platt{A: 1, B: 0, lr: 0.05} }

func (c *Platt) Apply(logit float64) float64 { return sigmoid(c.A*logit + c.B) }

func (c *Platt) Update(logit, y float64) {
	c.N++
	lr := c.lr / math.Sqrt(c.N)
	g := c.Apply(logit) - y
	c.A -= lr * g * logit
	c.B -= lr * g
	// Keep the map monotone and sane.
	c.A = math.Max(0.05, math.Min(c.A, 5))
	c.B = math.Max(-5, math.Min(c.B, 5))
}

func sigmoid(x float64) float64 {
	if x >= 0 {
		return 1 / (1 + math.Exp(-x))
	}
	e := math.Exp(x)
	return e / (1 + e)
}

// Sigmoid is the logistic function.
func Sigmoid(x float64) float64 { return sigmoid(x) }

// Classifier is a calibrated online binary classifier with prequential metrics.
//
// The classifier is skill-gated: it only counts as Active once its predictions
// over the most recent window beat climatology (positive Brier skill) and rank
// outcomes better than chance (AUC above MinAUC). Until then callers should use
// the base rate, which is what an honest forecaster says when it knows nothing.
type Classifier struct {
	mu      sync.Mutex
	ftrl    *FTRL
	platt   *Platt
	Metrics *Metrics
	recent  *Rolling
	names   []string
}

// Skill gate thresholds.
const (
	SkillWindow = 1500
	MinAUC      = 0.53
	MinBSS      = 0.0
)

// NewClassifier builds a classifier over the named features.
func NewClassifier(names []string, cfg FTRLConfig) *Classifier {
	return &Classifier{ftrl: NewFTRL(len(names), cfg), platt: NewPlatt(), Metrics: NewMetrics(), recent: NewRolling(SkillWindow), names: names}
}

// Status is the skill gate's view of a classifier.
type Status struct {
	Active    bool    `json:"active"`
	Samples   int     `json:"samples"`
	RecentAUC float64 `json:"recent_auc"`
	RecentBSS float64 `json:"recent_brier_skill"`
	BaseRate  float64 `json:"base_rate"`
	Reason    string  `json:"reason"`
}

// Status evaluates the skill gate; minSamples is the warm-up requirement.
func (c *Classifier) Status(minSamples int) Status {
	c.mu.Lock()
	defer c.mu.Unlock()
	auc, bss := c.recent.Skill()
	st := Status{Samples: c.Metrics.N, RecentAUC: auc, RecentBSS: bss, BaseRate: c.recent.BaseRate()}
	switch {
	case c.Metrics.N < minSamples:
		st.Reason = "warming up"
	case auc < MinAUC:
		st.Reason = "no ranking skill in the recent window"
	case bss <= MinBSS:
		st.Reason = "does not beat the base rate in the recent window"
	default:
		st.Active = true
		st.Reason = "out-of-sample skill demonstrated"
	}
	return st
}

// Predict returns the raw logit and the calibrated probability.
func (c *Classifier) Predict(x []float64) (logit, p float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	logit = c.ftrl.Logit(x)
	return logit, c.platt.Apply(logit)
}

// Learn records the prediction that was made when the example was created
// (pPred, logitPred: test) and then trains on the resolved label (train).
func (c *Classifier) Learn(x []float64, logitPred, pPred, y float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.Metrics.Add(pPred, y)
	c.recent.Add(pPred, y)
	c.platt.Update(logitPred, y)
	c.ftrl.Update(x, y)
}

// Contribution is one feature's share of the current logit.
type Contribution struct {
	Feature string  `json:"feature"`
	Value   float64 `json:"value"`
	Weight  float64 `json:"weight"`
	Effect  float64 `json:"effect"` // weight * value, in logit units
}

// Explain returns per-feature contributions to the logit for x, largest |effect| first.
func (c *Classifier) Explain(x []float64, top int) []Contribution {
	c.mu.Lock()
	w := c.ftrl.Weights()
	c.mu.Unlock()
	out := make([]Contribution, 0, len(x))
	for i, v := range x {
		if e := w[i+1] * v; e != 0 {
			out = append(out, Contribution{Feature: c.names[i], Value: v, Weight: w[i+1], Effect: e})
		}
	}
	sortContrib(out)
	if top > 0 && len(out) > top {
		out = out[:top]
	}
	return out
}

func sortContrib(c []Contribution) {
	for i := 1; i < len(c); i++ {
		for j := i; j > 0 && math.Abs(c[j].Effect) > math.Abs(c[j-1].Effect); j-- {
			c[j], c[j-1] = c[j-1], c[j]
		}
	}
}

// Samples is the number of resolved examples learned.
func (c *Classifier) Samples() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.Metrics.N
}

// Report is a JSON-safe snapshot of the prequential metrics.
func (c *Classifier) Report() MetricsReport {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.Metrics.Report()
}

type classifierState struct {
	Names []string   `json:"names"`
	Cfg   FTRLConfig `json:"cfg"`
	Z     []float64  `json:"z"`
	N     []float64  `json:"n"`
	A     float64    `json:"platt_a"`
	B     float64    `json:"platt_b"`
	PN    float64    `json:"platt_n"`
}

// MarshalJSON persists the learned state (metrics are not persisted).
func (c *Classifier) MarshalJSON() ([]byte, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return json.Marshal(classifierState{Names: c.names, Cfg: c.ftrl.cfg, Z: c.ftrl.z, N: c.ftrl.n, A: c.platt.A, B: c.platt.B, PN: c.platt.N})
}

// UnmarshalJSON restores state written by MarshalJSON.
func (c *Classifier) UnmarshalJSON(b []byte) error {
	var s classifierState
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.names = s.Names
	c.ftrl = &FTRL{cfg: s.Cfg, z: s.Z, n: s.N}
	c.platt = &Platt{A: s.A, B: s.B, N: s.PN, lr: 0.05}
	if c.Metrics == nil {
		c.Metrics = NewMetrics()
	}
	if c.recent == nil {
		c.recent = NewRolling(SkillWindow)
	}
	return nil
}
