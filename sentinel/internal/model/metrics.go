package model

import (
	"fmt"
	"math"
	"sort"
)

// Metrics accumulates prequential scores of probabilistic predictions.
//
// The reference forecaster for skill scores is "climatology": the running base
// rate known *before* each prediction. Positive skill therefore means the model
// beats simply predicting the historical hit rate, which is the honest bar for
// calling an indicator predictive.
type Metrics struct {
	N        int
	Pos      int
	LogLoss  float64
	Brier    float64
	RefBrier float64
	RefLog   float64
	bins     [10]bin
	recent   []pair // ring of the last recentCap predictions, for AUC
	head     int
}

type bin struct {
	n   int
	sum float64 // sum of predicted probabilities
	pos int
}

type pair struct{ p, y float64 }

const recentCap = 5000

func NewMetrics() *Metrics { return &Metrics{} }

// Add scores prediction p against outcome y (0 or 1).
func (m *Metrics) Add(p, y float64) {
	p = math.Min(math.Max(p, 1e-6), 1-1e-6)
	// Climatology reference uses the base rate before this example (Laplace-smoothed).
	ref := (float64(m.Pos) + 1) / (float64(m.N) + 2)
	m.N++
	if y > 0.5 {
		m.Pos++
	}
	m.LogLoss += -(y*math.Log(p) + (1-y)*math.Log(1-p))
	m.Brier += (p - y) * (p - y)
	m.RefBrier += (ref - y) * (ref - y)
	m.RefLog += -(y*math.Log(ref) + (1-y)*math.Log(1-ref))
	k := int(p * 10)
	if k > 9 {
		k = 9
	}
	m.bins[k].n++
	m.bins[k].sum += p
	if y > 0.5 {
		m.bins[k].pos++
	}
	if len(m.recent) < recentCap {
		m.recent = append(m.recent, pair{p, y})
	} else {
		m.recent[m.head] = pair{p, y}
		m.head = (m.head + 1) % recentCap
	}
}

// AUC is the area under the ROC curve of the recent window (0.5 = no skill).
func (m *Metrics) AUC() float64 {
	ps := append([]pair(nil), m.recent...)
	sort.Slice(ps, func(i, j int) bool { return ps[i].p < ps[j].p })
	var pos, neg float64
	for _, x := range ps {
		if x.y > 0.5 {
			pos++
		} else {
			neg++
		}
	}
	if pos == 0 || neg == 0 {
		return 0.5
	}
	// Rank-sum (Mann-Whitney U) with average ranks for ties.
	rankSum := 0.0
	for i := 0; i < len(ps); {
		j := i
		for j < len(ps) && ps[j].p == ps[i].p {
			j++
		}
		avg := float64(i+j+1) / 2 // ranks are 1-based: (i+1 + j) / 2
		for k := i; k < j; k++ {
			if ps[k].y > 0.5 {
				rankSum += avg
			}
		}
		i = j
	}
	return (rankSum - pos*(pos+1)/2) / (pos * neg)
}

// Bin is one reliability-diagram bucket.
type Bin struct {
	Range    string  `json:"range"`
	Count    int     `json:"count"`
	MeanPred float64 `json:"mean_pred"`
	Observed float64 `json:"observed"`
}

// MetricsReport is the JSON view of Metrics.
type MetricsReport struct {
	Samples      int     `json:"samples"`
	BaseRate     float64 `json:"base_rate"`
	LogLoss      float64 `json:"log_loss"`
	Brier        float64 `json:"brier"`
	BrierSkill   float64 `json:"brier_skill"`    // 1 - Brier/Brier(climatology); >0 beats the base rate
	LogLossSkill float64 `json:"log_loss_skill"` // 1 - LogLoss/LogLoss(climatology)
	AUC          float64 `json:"auc"`
	Reliability  []Bin   `json:"reliability"`
}

func (m *Metrics) Report() MetricsReport {
	r := MetricsReport{Samples: m.N, AUC: 0.5}
	if m.N == 0 {
		return r
	}
	n := float64(m.N)
	r.BaseRate = float64(m.Pos) / n
	r.LogLoss = m.LogLoss / n
	r.Brier = m.Brier / n
	if m.RefBrier > 0 {
		r.BrierSkill = 1 - m.Brier/m.RefBrier
	}
	if m.RefLog > 0 {
		r.LogLossSkill = 1 - m.LogLoss/m.RefLog
	}
	r.AUC = m.AUC()
	for i, b := range m.bins {
		if b.n == 0 {
			continue
		}
		r.Reliability = append(r.Reliability, Bin{
			Range:    rangeLabel(i),
			Count:    b.n,
			MeanPred: b.sum / float64(b.n),
			Observed: float64(b.pos) / float64(b.n),
		})
	}
	return r
}

func rangeLabel(i int) string {
	return fmt.Sprintf("%.1f-%.1f", float64(i)/10, float64(i+1)/10)
}
