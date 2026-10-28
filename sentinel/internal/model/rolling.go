package model

import "math"

// Rolling scores the most recent predictions against climatology (the running
// base rate known before each prediction). It is what decides whether a
// directional model has earned the right to influence signals.
type Rolling struct {
	cap   int
	p, y  []float64
	ref   []float64
	head  int
	n     int
	pos   int // all-time positives, for the climatology reference
	total int
}

// NewRolling keeps the last capacity predictions.
func NewRolling(capacity int) *Rolling {
	return &Rolling{cap: capacity, p: make([]float64, capacity), y: make([]float64, capacity), ref: make([]float64, capacity)}
}

// Add records prediction p for outcome y.
func (r *Rolling) Add(p, y float64) {
	ref := (float64(r.pos) + 1) / (float64(r.total) + 2)
	i := r.head
	if r.n < r.cap {
		i = r.n
		r.n++
	} else {
		r.head = (r.head + 1) % r.cap
	}
	r.p[i], r.y[i], r.ref[i] = p, y, ref
	r.total++
	if y > 0.5 {
		r.pos++
	}
}

// BaseRate is the all-time outcome rate (Laplace-smoothed).
func (r *Rolling) BaseRate() float64 { return (float64(r.pos) + 1) / (float64(r.total) + 2) }

// Len is the number of predictions in the window.
func (r *Rolling) Len() int { return r.n }

// Skill returns the window's AUC and Brier skill versus climatology.
func (r *Rolling) Skill() (auc, bss float64) {
	if r.n == 0 {
		return 0.5, 0
	}
	m := NewMetrics()
	var b, rb float64
	for i := 0; i < r.n; i++ {
		m.Add(r.p[i], r.y[i])
		b += (r.p[i] - r.y[i]) * (r.p[i] - r.y[i])
		rb += (r.ref[i] - r.y[i]) * (r.ref[i] - r.y[i])
	}
	auc = m.AUC()
	if rb > 0 {
		bss = 1 - b/rb
	}
	if math.IsNaN(bss) {
		bss = 0
	}
	return auc, bss
}
