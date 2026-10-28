// Package backtest runs Sentinel walk-forward over history and reports how
// predictive it was. Every number is prequential: each prediction is made
// before its label is known and the model only learns from labels resolved by
// then, so the report is out-of-sample from the first bar to the last.
package backtest

import (
	"fmt"
	"math"
	"math/rand"
	"sort"
	"strings"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/desk"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/engine"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/model"
)

// SideReport summarises one side.
type SideReport struct {
	Model        model.MetricsReport `json:"model"`
	BaselineR    float64             `json:"baseline_r"`   // mean net R of entering on every bar
	BaselineHit  float64             `json:"baseline_hit"` // target-first rate on every bar
	Signals      int                 `json:"signals"`
	HitRate      float64             `json:"hit_rate"`
	ExpectancyR  float64             `json:"expectancy_r"` // net of costs
	ExpectancyCI [2]float64          `json:"expectancy_ci95"`
	ProfitFactor float64             `json:"profit_factor"`
	TotalR       float64             `json:"total_r"`
	MaxDrawdownR float64             `json:"max_drawdown_r"`
	FirstHalfR   float64             `json:"first_half_expectancy_r"`
	SecondHalfR  float64             `json:"second_half_expectancy_r"`
	ByRegime     map[string]Bucket   `json:"by_regime"`
	ByTier       map[string]Bucket   `json:"by_tier"`
}

// Bucket is a group of signal outcomes.
type Bucket struct {
	N           int     `json:"n"`
	HitRate     float64 `json:"hit_rate"`
	ExpectancyR float64 `json:"expectancy_r"`
}

// Report is the backtest result.
type Report struct {
	Symbols    []string                  `json:"symbols"`
	TF         string                    `json:"tf"`
	From       time.Time                 `json:"from"`
	To         time.Time                 `json:"to"`
	Bars       int                       `json:"bars"`
	Config     engine.Config             `json:"config"`
	Volatility map[string]model.Forecast `json:"volatility"` // final out-of-sample forecast scores per symbol
	Long       SideReport                `json:"long"`
	Short      SideReport                `json:"short"`
	Verdict    []string                  `json:"verdict"`
}

type sigInfo struct {
	regime, tier string
}

// Run replays hist through a desk and builds the report.
func Run(hist map[string][]market.Bar, ref string, cfg engine.Config) Report {
	syms := make([]string, 0, len(hist))
	for s := range hist {
		syms = append(syms, s)
	}
	sort.Strings(syms)
	d := desk.New(syms, ref, cfg, nil)

	// Merge bars in time order, reference first at equal timestamps.
	type tsBar struct {
		sym string
		bar market.Bar
	}
	var all []tsBar
	for s, bars := range hist {
		for _, b := range bars {
			all = append(all, tsBar{s, b})
		}
	}
	sort.SliceStable(all, func(i, j int) bool {
		if !all[i].bar.Time.Equal(all[j].bar.Time) {
			return all[i].bar.Time.Before(all[j].bar.Time)
		}
		return all[i].sym == ref && all[j].sym != ref
	})
	rep := Report{Symbols: syms, TF: cfg.Base.Name, Bars: len(all), Config: cfg}
	if len(all) == 0 {
		rep.Verdict = []string{"no data"}
		return rep
	}
	rep.From, rep.To = all[0].bar.Time, all[len(all)-1].bar.Time

	info := map[string]sigInfo{}
	var outcomes []engine.Outcome
	for _, tb := range all {
		snap, ok := d.OnBar(tb.sym, tb.bar)
		if !ok {
			continue
		}
		if snap.Signal.Emitted {
			info[key(tb.sym, snap.Time, snap.Signal.Direction)] = sigInfo{regime: snap.Regime.Kind, tier: snap.Signal.Tier}
		}
		e, _ := d.Engine(tb.sym)
		outcomes = append(outcomes, e.DrainOutcomes()...)
	}
	sort.SliceStable(outcomes, func(i, j int) bool { return outcomes[i].Time.Before(outcomes[j].Time) })

	rep.Volatility = map[string]model.Forecast{}
	for _, s := range syms {
		e, _ := d.Engine(s)
		rep.Volatility[s] = e.Forecast()
	}
	rep.Long = side(outcomes, 1, info, d.Models.Long.Report())
	rep.Short = side(outcomes, -1, info, d.Models.Short.Report())
	rep.Verdict = verdict(rep)
	return rep
}

func key(sym string, t time.Time, dir int) string { return fmt.Sprintf("%s|%d|%d", sym, t.Unix(), dir) }

func side(all []engine.Outcome, dir int, info map[string]sigInfo, mr model.MetricsReport) SideReport {
	r := SideReport{Model: mr, ByRegime: map[string]Bucket{}, ByTier: map[string]Bucket{}}
	var base, wins float64
	var nBase int
	var sig []engine.Outcome
	for _, o := range all {
		if o.Side != dir {
			continue
		}
		nBase++
		base += o.R
		if o.Result == 1 {
			wins++
		}
		if o.Emitted {
			sig = append(sig, o)
		}
	}
	if nBase > 0 {
		r.BaselineR = base / float64(nBase)
		r.BaselineHit = wins / float64(nBase)
	}
	r.Signals = len(sig)
	if len(sig) == 0 {
		return r
	}
	rs := make([]float64, len(sig))
	var hits int
	var gw, gl, eq, peak float64
	regime := map[string][]engine.Outcome{}
	tier := map[string][]engine.Outcome{}
	for i, o := range sig {
		rs[i] = o.R
		if o.Result == 1 {
			hits++
		}
		if o.R > 0 {
			gw += o.R
		} else {
			gl -= o.R
		}
		eq += o.R
		peak = math.Max(peak, eq)
		r.MaxDrawdownR = math.Max(r.MaxDrawdownR, peak-eq)
		in := info[key(o.Symbol, o.Time, o.Side)]
		regime[in.regime] = append(regime[in.regime], o)
		tier[in.tier] = append(tier[in.tier], o)
	}
	r.HitRate = float64(hits) / float64(len(sig))
	r.TotalR = eq
	r.ExpectancyR = eq / float64(len(sig))
	if gl > 0 {
		r.ProfitFactor = gw / gl
	}
	r.ExpectancyCI = bootstrapCI(rs, 2000, 7)
	h := len(rs) / 2
	r.FirstHalfR, r.SecondHalfR = mean(rs[:h]), mean(rs[h:])
	for k, v := range regime {
		r.ByRegime[k] = bucket(v)
	}
	for k, v := range tier {
		r.ByTier[k] = bucket(v)
	}
	return r
}

func bucket(os []engine.Outcome) Bucket {
	b := Bucket{N: len(os)}
	s, h := 0.0, 0
	for _, o := range os {
		s += o.R
		if o.Result == 1 {
			h++
		}
	}
	if len(os) > 0 {
		b.HitRate = float64(h) / float64(len(os))
		b.ExpectancyR = s / float64(len(os))
	}
	return b
}

func mean(x []float64) float64 {
	if len(x) == 0 {
		return 0
	}
	s := 0.0
	for _, v := range x {
		s += v
	}
	return s / float64(len(x))
}

// bootstrapCI is a percentile bootstrap of the mean with a fixed seed.
func bootstrapCI(x []float64, iters int, seed int64) [2]float64 {
	if len(x) < 2 {
		return [2]float64{mean(x), mean(x)}
	}
	rng := rand.New(rand.NewSource(seed))
	ms := make([]float64, iters)
	for i := range ms {
		s := 0.0
		for j := 0; j < len(x); j++ {
			s += x[rng.Intn(len(x))]
		}
		ms[i] = s / float64(len(x))
	}
	sort.Float64s(ms)
	return [2]float64{ms[int(0.025*float64(iters))], ms[int(0.975*float64(iters))-1]}
}

func verdict(r Report) []string {
	var v []string
	for _, s := range r.Symbols {
		f := r.Volatility[s]
		if f.Samples == 0 {
			continue
		}
		word := "does not beat"
		if f.Trusted {
			word = "beats"
		}
		v = append(v, fmt.Sprintf("%s volatility forecast: out-of-sample R^2 %.3f vs %.3f for naive persistence (%s it) over %d forecasts.", s, f.R2, f.R2Naive, word, f.Samples))
	}
	for _, s := range []struct {
		name string
		r    SideReport
	}{{"long", r.Long}, {"short", r.Short}} {
		m := s.r.Model
		skill := "no skill over the base rate"
		if m.BrierSkill > 0.005 && m.AUC > 0.53 {
			skill = "modest skill over the base rate"
		}
		if m.BrierSkill > 0.02 && m.AUC > 0.58 {
			skill = "material skill over the base rate"
		}
		v = append(v, fmt.Sprintf("%s model: AUC %.3f, Brier skill %+.3f on %d out-of-sample labels: %s.", s.name, m.AUC, m.BrierSkill, m.Samples, skill))
		if s.r.Signals == 0 {
			v = append(v, fmt.Sprintf("%s: no signals passed the gate (a valid outcome when costs exceed the edge).", s.name))
			continue
		}
		edge := "does NOT beat"
		if s.r.ExpectancyCI[0] > s.r.BaselineR {
			edge = "beats (95% CI excludes the baseline)"
		} else if s.r.ExpectancyR > s.r.BaselineR {
			edge = "is above but not statistically separated from"
		}
		v = append(v, fmt.Sprintf("%s signals: %d trades, hit %.1f%%, expectancy %+.3fR net (95%% CI %+.3f..%+.3f) %s the every-bar baseline %+.3fR.",
			s.name, s.r.Signals, s.r.HitRate*100, s.r.ExpectancyR, s.r.ExpectancyCI[0], s.r.ExpectancyCI[1], edge, s.r.BaselineR))
		if s.r.Signals < 30 {
			v = append(v, fmt.Sprintf("%s: fewer than 30 signals; treat the result as anecdotal.", s.name))
		}
	}
	return v
}

// Text renders the report for a terminal.
func (r Report) Text() string {
	var b strings.Builder
	fmt.Fprintf(&b, "SENTINEL walk-forward backtest  %s  %s  %s -> %s  (%d bars)\n", strings.Join(r.Symbols, ","), r.TF,
		r.From.Format("2006-01-02"), r.To.Format("2006-01-02"), r.Bars)
	fmt.Fprintf(&b, "barriers: stop %.1f ATR, target %.1fR, horizon %d bars; costs %.2f%% + %.2f%% + %.2f%% slippage\n\n",
		r.Config.StopATR, r.Config.TargetR, r.Config.Horizon, r.Config.FeeEntryPct, r.Config.FeeExitPct, r.Config.SlippagePct)
	for _, s := range []struct {
		name string
		r    SideReport
	}{{"LONG", r.Long}, {"SHORT", r.Short}} {
		m := s.r.Model
		fmt.Fprintf(&b, "%s\n", s.name)
		fmt.Fprintf(&b, "  model (prequential): samples %d  base rate %.3f  AUC %.3f  Brier %.4f  Brier skill %+.4f  log-loss skill %+.4f\n",
			m.Samples, m.BaseRate, m.AUC, m.Brier, m.BrierSkill, m.LogLossSkill)
		fmt.Fprintf(&b, "  reliability:")
		for _, bin := range m.Reliability {
			fmt.Fprintf(&b, "  [%s] n=%d pred=%.2f obs=%.2f", bin.Range, bin.Count, bin.MeanPred, bin.Observed)
		}
		fmt.Fprintf(&b, "\n  baseline (enter every bar): hit %.3f  expectancy %+.3fR\n", s.r.BaselineHit, s.r.BaselineR)
		fmt.Fprintf(&b, "  signals: %d  hit %.3f  expectancy %+.3fR  CI95 [%+.3f, %+.3f]  PF %.2f  total %+.1fR  maxDD %.1fR  halves %+.3f / %+.3f\n",
			s.r.Signals, s.r.HitRate, s.r.ExpectancyR, s.r.ExpectancyCI[0], s.r.ExpectancyCI[1], s.r.ProfitFactor, s.r.TotalR, s.r.MaxDrawdownR, s.r.FirstHalfR, s.r.SecondHalfR)
		keys := make([]string, 0, len(s.r.ByRegime))
		for k := range s.r.ByRegime {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			bk := s.r.ByRegime[k]
			fmt.Fprintf(&b, "    regime %-10s n=%-4d hit %.3f  exp %+.3fR\n", k, bk.N, bk.HitRate, bk.ExpectancyR)
		}
		keys = keys[:0]
		for k := range s.r.ByTier {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			bk := s.r.ByTier[k]
			fmt.Fprintf(&b, "    tier   %-10s n=%-4d hit %.3f  exp %+.3fR\n", k, bk.N, bk.HitRate, bk.ExpectancyR)
		}
		b.WriteString("\n")
	}
	for _, s := range r.Symbols {
		f := r.Volatility[s]
		fmt.Fprintf(&b, "VOLATILITY %s: forecast R^2 %.3f (naive %.3f), trusted %v, band coverage %.1f%% (68%% target) / %.1f%% (90%% target), next %d bars sigma %.2f%% (x%.2f)\n",
			s, f.R2, f.R2Naive, f.Trusted, f.Coverage68*100, f.Coverage90*100, f.Horizon, f.SigmaHorizon*100, f.Expansion)
	}
	b.WriteString("\nVERDICT\n")
	for _, v := range r.Verdict {
		fmt.Fprintf(&b, "  - %s\n", v)
	}
	return b.String()
}
