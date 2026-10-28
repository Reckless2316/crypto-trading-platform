package engine

import (
	"fmt"
	"math"
	"sync"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/model"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/ta"
)

// Config holds the engine's published constants. Unlike closed signal
// services, every threshold is visible and configurable.
type Config struct {
	Symbol string           `json:"symbol"`
	Base   market.Timeframe `json:"-"`

	// Triple-barrier geometry, in ATR of the base frame.
	StopATR float64 `json:"stop_atr"` // 1R = StopATR * ATR
	TargetR float64 `json:"target_r"` // profit barrier in R
	Horizon int     `json:"horizon"`  // vertical barrier, in base bars

	// Costs, percent of notional (OKX US Lv1 defaults: maker entry, taker exit).
	FeeEntryPct float64 `json:"fee_entry_pct"`
	FeeExitPct  float64 `json:"fee_exit_pct"`
	SlippagePct float64 `json:"slippage_pct"`

	// Signal gate.
	MinScore        float64 `json:"min_score"`
	MinLayers       int     `json:"min_layers"`
	MaxDisagreement float64 `json:"max_disagreement"` // losing/winning score ratio cap
	MinEdgeR        float64 `json:"min_edge_r"`       // net expected R the model must show
	WarmSamples     int     `json:"warm_samples"`     // resolved labels before the model can gate

	// Price-aware de-duplication.
	CooldownBars int     `json:"cooldown_bars"`
	CooldownATR  float64 `json:"cooldown_atr"`

	MaxRiskPct float64 `json:"max_risk_pct"` // cap on suggested equity risk per trade

	// RequireProvenEdge makes a signal actionable only once this symbol/side's
	// live track record (or an active, skilled model) shows positive net edge.
	RequireProvenEdge bool `json:"require_proven_edge"`

	Model model.FTRLConfig `json:"model"` // online learner hyper-parameters
}

// DefaultConfig returns conservative defaults for a base timeframe.
func DefaultConfig(symbol string, base market.Timeframe) Config {
	return Config{
		Symbol: symbol, Base: base,
		StopATR: 1.5, TargetR: 2, Horizon: 48,
		FeeEntryPct: 0.20, FeeExitPct: 0.35, SlippagePct: 0.05,
		MinScore: 5, MinLayers: 3, MaxDisagreement: 0.5, MinEdgeR: 0.05, WarmSamples: 300,
		CooldownBars: 12, CooldownATR: 1.0,
		MaxRiskPct: 2,
		Model:      model.DefaultFTRL(),
	}
}

// CostR is the round-trip cost expressed in R for a stop distance.
func (c Config) CostR(entry, stopDist float64) float64 {
	if stopDist <= 0 {
		return 0
	}
	return (c.FeeEntryPct + c.FeeExitPct + c.SlippagePct) / 100 * entry / stopDist
}

// Models is the long/short classifier pair. One pair can be shared by every
// engine in a universe (pooled cross-sectional learning) as long as bars are
// fed in global time order.
type Models struct {
	Long, Short *model.Classifier

	mu      sync.Mutex
	nonWinR [2]float64 // running mean R of non-winning outcomes (long, short)
	nonWinN [2]int
}

// NewModels creates an untrained pair with the given learner settings.
func NewModels(cfg model.FTRLConfig) *Models {
	names := FeatureNames()
	return &Models{
		Long:    model.NewClassifier(names, cfg),
		Short:   model.NewClassifier(names, cfg),
		nonWinR: [2]float64{-1, -1},
	}
}

func sideIdx(dir int) int {
	if dir > 0 {
		return 0
	}
	return 1
}

func (m *Models) observeNonWin(dir int, r float64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	i := sideIdx(dir)
	m.nonWinN[i]++
	// Shrink toward -1R until there is data; then a running mean.
	m.nonWinR[i] += (r - m.nonWinR[i]) / float64(m.nonWinN[i]+5)
}

// NonWinR is the expected R of an outcome that does not reach the target.
func (m *Models) NonWinR(dir int) float64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.nonWinR[sideIdx(dir)]
}

// RefState is the reference asset's state, passed to other engines.
type RefState struct {
	Symbol  string
	Time    time.Time
	MidROC  float64
	Trend   int
	Shock   int
	ReturnZ float64
}

// SignalStats tracks the realised outcomes of emitted signals (net of costs).
type SignalStats struct {
	Count      int     `json:"count"`
	Wins       int     `json:"wins"`
	Losses     int     `json:"losses"`
	Timeouts   int     `json:"timeouts"`
	SumR       float64 `json:"sum_r"`
	GrossWinR  float64 `json:"gross_win_r"`
	GrossLossR float64 `json:"gross_loss_r"`
}

func (s *SignalStats) add(r float64, outcome int) {
	s.Count++
	s.SumR += r
	switch outcome {
	case 1:
		s.Wins++
	case -1:
		s.Losses++
	default:
		s.Timeouts++
	}
	if r > 0 {
		s.GrossWinR += r
	} else {
		s.GrossLossR -= r
	}
}

// Summary returns hit rate, expectancy (R/trade) and profit factor.
func (s SignalStats) Summary() (hit, expectancy, pf float64) {
	if s.Count == 0 {
		return 0, 0, 0
	}
	hit = float64(s.Wins) / float64(s.Count)
	expectancy = s.SumR / float64(s.Count)
	if s.GrossLossR > 0 {
		pf = s.GrossWinR / s.GrossLossR
	} else if s.GrossWinR > 0 {
		pf = math.Inf(1)
	}
	return
}

type pending struct {
	idx          int
	time         time.Time
	entry, sl    float64 // sl is 1R in price
	costR        float64
	x            []float64
	logitL, pL   float64
	logitS, pS   float64
	emitted      int // direction of the signal emitted on this bar, 0 if none
	doneL, doneS bool
	bars         int
}

// Outcome is a resolved triple-barrier label (exposed for the ledger).
type Outcome struct {
	Symbol  string    `json:"symbol"`
	Time    time.Time `json:"time"` // bar the prediction was made on
	Side    int       `json:"side"`
	Result  int       `json:"result"` // 1 target, -1 stop, 0 horizon
	R       float64   `json:"r"`      // net of costs
	Emitted bool      `json:"emitted"`
}

// Engine is the per-symbol Sentinel state machine.
type Engine struct {
	cfg             Config
	mid, high       market.Timeframe
	base, midF, hiF *Frame
	aggMid, aggHigh *Aggregator
	models          *Models
	pend            []*pending
	idx             int
	retStd          *ta.StdDev
	vol             *model.VolForecaster
	lastBar         market.Bar
	fc              model.Forecast
	track           [2][]float64 // net R of recent fired signals, per side
	prevClose       float64
	lastRet         float64
	der             *market.Derivatives
	opt             *market.Options
	ref             *RefState
	lastSig         [2]struct {
		idx   int
		price float64
		set   bool
	}
	stats    [2]SignalStats
	last     Snapshot
	outcomes []Outcome // resolved since the last DrainOutcomes
	mu       sync.Mutex
}

// New creates an engine. Pass nil models for a private (unshared) model pair.
func New(cfg Config, models *Models) *Engine {
	if models == nil {
		models = NewModels(cfg.Model)
	}
	mid, high := market.Ladder(cfg.Base)
	return &Engine{
		cfg: cfg, mid: mid, high: high,
		base: NewFrame(cfg.Base), midF: NewFrame(mid), hiF: NewFrame(high),
		aggMid: NewAggregator(cfg.Base.Dur, mid.Dur), aggHigh: NewAggregator(cfg.Base.Dur, high.Dur),
		models: models,
		retStd: ta.NewStdDev(100),
		vol:    model.NewVolForecaster(cfg.Horizon, []int{1, 6, 24, 120}),
	}
}

// Config returns the engine configuration.
func (e *Engine) Config() Config { return e.cfg }

// Models returns the classifier pair.
func (e *Engine) Models() *Models { return e.models }

// SetDerivatives supplies the latest perp snapshot.
func (e *Engine) SetDerivatives(d market.Derivatives) {
	e.mu.Lock()
	e.der = &d
	e.mu.Unlock()
}

// SetOptions supplies the latest options snapshot.
func (e *Engine) SetOptions(o market.Options) {
	e.mu.Lock()
	e.opt = &o
	e.mu.Unlock()
}

// Last returns the latest snapshot.
func (e *Engine) Last() Snapshot {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.last
}

// Forecast returns the latest volatility forecast.
func (e *Engine) Forecast() model.Forecast {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.fc
}

// Stats returns realised signal statistics (long, short).
func (e *Engine) Stats() (long, short SignalStats) {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.stats[0], e.stats[1]
}

// DrainOutcomes returns and clears outcomes resolved since the last call.
func (e *Engine) DrainOutcomes() []Outcome {
	e.mu.Lock()
	defer e.mu.Unlock()
	o := e.outcomes
	e.outcomes = nil
	return o
}

// RefState summarises this engine as a reference asset for others.
func (e *Engine) RefState() RefState {
	e.mu.Lock()
	defer e.mu.Unlock()
	h := e.hiF.Last()
	trend := 0
	if h.Ready {
		if h.Close > h.EMA50 && h.SupertrendDir > 0 {
			trend = 1
		} else if h.Close < h.EMA50 && h.SupertrendDir < 0 {
			trend = -1
		}
	}
	z := 0.0
	if e.retStd.Ready() {
		z = e.retStd.ZScore(e.lastRet)
	}
	shock := 0
	if math.Abs(z) >= 3.5 {
		shock = sgn(z)
	}
	return RefState{Symbol: e.cfg.Symbol, Time: e.base.Last().Time, MidROC: e.midF.Last().ROC, Trend: trend, Shock: shock, ReturnZ: z}
}

// Update consumes one closed base bar (and an optional reference state for
// cross-asset context) and returns the new snapshot.
func (e *Engine) Update(b market.Bar, ref *RefState) Snapshot {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.idx++
	if e.prevClose > 0 && b.Close > 0 {
		e.lastRet = math.Log(b.Close / e.prevClose)
		e.retStd.Update(e.lastRet)
	}
	e.prevClose = b.Close
	e.ref = ref

	e.resolve(b)
	e.fc = e.vol.Update(b.High, b.Low, b.Close)

	br := e.base.Update(b)
	for _, mb := range e.aggMid.Add(b) {
		e.midF.Update(mb)
	}
	for _, hb := range e.aggHigh.Add(b) {
		e.hiF.Update(hb)
	}
	e.lastBar = b
	snap := e.evaluate(b, br, true)
	e.last = snap
	return snap
}

// resolve advances pending triple-barrier labels with the new bar.
func (e *Engine) resolve(b market.Bar) {
	keep := e.pend[:0]
	for _, p := range e.pend {
		p.bars++
		timeout := p.bars >= e.cfg.Horizon
		tp := e.cfg.TargetR * p.sl
		if !p.doneL {
			res, r := 0, 0.0
			switch {
			case b.Low <= p.entry-p.sl: // stop first when both are touched in one bar
				res, r = -1, -1
			case b.High >= p.entry+tp:
				res, r = 1, e.cfg.TargetR
			case timeout:
				res, r = 0, (b.Close-p.entry)/p.sl
			default:
				res = 2
			}
			if res != 2 {
				p.doneL = true
				e.finish(p, 1, res, r-p.costR)
			}
		}
		if !p.doneS {
			res, r := 0, 0.0
			switch {
			case b.High >= p.entry+p.sl:
				res, r = -1, -1
			case b.Low <= p.entry-tp:
				res, r = 1, e.cfg.TargetR
			case timeout:
				res, r = 0, (p.entry-b.Close)/p.sl
			default:
				res = 2
			}
			if res != 2 {
				p.doneS = true
				e.finish(p, -1, res, r-p.costR)
			}
		}
		if !(p.doneL && p.doneS) {
			keep = append(keep, p)
		}
	}
	e.pend = keep
}

func (e *Engine) finish(p *pending, dir, res int, netR float64) {
	y := 0.0
	if res == 1 {
		y = 1
	}
	if dir > 0 {
		e.models.Long.Learn(p.x, p.logitL, p.pL, y)
	} else {
		e.models.Short.Learn(p.x, p.logitS, p.pS, y)
	}
	if res != 1 {
		e.models.observeNonWin(dir, netR)
	}
	emitted := p.emitted == dir
	if emitted {
		i := sideIdx(dir)
		e.stats[i].add(netR, res)
		e.track[i] = append(e.track[i], netR)
		if len(e.track[i]) > 500 {
			e.track[i] = e.track[i][len(e.track[i])-500:]
		}
	}
	e.outcomes = append(e.outcomes, Outcome{Symbol: e.cfg.Symbol, Time: p.time, Side: dir, Result: res, R: netR, Emitted: emitted})
	if len(e.outcomes) > 10000 {
		e.outcomes = e.outcomes[len(e.outcomes)-10000:]
	}
}

// GateCheck is one condition of the signal gate.
type GateCheck struct {
	Name   string `json:"name"`
	Pass   bool   `json:"pass"`
	Detail string `json:"detail"`
}

// TradePlan is the deterministic trade geometry for a signal. Stop and targets
// are exactly the barriers the model is trained and evaluated on.
type TradePlan struct {
	Side          string  `json:"side"`
	Entry         float64 `json:"entry"`
	Stop          float64 `json:"stop"`
	TP1           float64 `json:"tp1"` // +1R, a partial-profit level
	TP2           float64 `json:"tp2"` // model target barrier
	StopPct       float64 `json:"stop_pct"`
	RewardRisk    float64 `json:"reward_risk"`
	CostR         float64 `json:"cost_r"`
	ExpectedR     float64 `json:"expected_r"`
	HorizonBars   int     `json:"horizon_bars"`
	KellyFraction float64 `json:"kelly_fraction"`
	RiskPct       float64 `json:"suggested_risk_pct"` // of equity, quarter-Kelly capped
	Obstruction   string  `json:"obstruction,omitempty"`
	SpotNote      string  `json:"spot_note,omitempty"`
}

// Signal is the decision for the bar.
type Signal struct {
	Action     string      `json:"action"` // LONG, SHORT, WATCH_LONG, WATCH_SHORT, NEUTRAL
	Direction  int         `json:"direction"`
	Route      string      `json:"route,omitempty"`
	Confidence float64     `json:"confidence"`
	Tier       string      `json:"tier"`
	Gate       []GateCheck `json:"gate"`
	Plan       *TradePlan  `json:"plan,omitempty"`
	Emitted    bool        `json:"emitted"`    // passed the gate and de-duplication
	Actionable bool        `json:"actionable"` // emitted, and allowed by the edge policy
	Suppressed string      `json:"suppressed,omitempty"`
	EdgeStatus string      `json:"edge_status"` // proven, unproven, negative (this symbol and side, live record)
	Track      TrackRecord `json:"track_record"`
	Summary    string      `json:"summary"`
}

// TrackRecord is the realised, cost-adjusted record of fired signals for one
// symbol and side, scored prequentially (each outcome resolved after its signal).
type TrackRecord struct {
	N           int     `json:"n"`
	HitRate     float64 `json:"hit_rate"`
	ExpectancyR float64 `json:"expectancy_r"`
	CILow       float64 `json:"ci95_low"`
	CIHigh      float64 `json:"ci95_high"`
}

// minTrack is the sample size below which a track record proves nothing.
const minTrack = 30

func trackRecord(rs []float64) (TrackRecord, string) {
	t := TrackRecord{N: len(rs)}
	if len(rs) == 0 {
		return t, "unproven"
	}
	var s, s2 float64
	wins := 0
	for _, r := range rs {
		s += r
		s2 += r * r
		if r > 0 {
			wins++
		}
	}
	n := float64(len(rs))
	t.HitRate = float64(wins) / n
	t.ExpectancyR = s / n
	sd := 0.0
	if len(rs) > 1 {
		sd = math.Sqrt(math.Max(0, (s2-n*t.ExpectancyR*t.ExpectancyR)/(n-1)))
	}
	half := 1.96 * sd / math.Sqrt(n)
	t.CILow, t.CIHigh = t.ExpectancyR-half, t.ExpectancyR+half
	switch {
	case len(rs) < minTrack:
		return t, "unproven"
	case t.CILow > 0:
		return t, "proven"
	case t.CIHigh < 0:
		return t, "negative"
	}
	return t, "unproven"
}

// ModelView is the predictive layer's output for the bar.
type ModelView struct {
	Warm       bool                 `json:"warm"`
	Samples    int                  `json:"samples"`
	Long       model.Status         `json:"long_status"`
	Short      model.Status         `json:"short_status"`
	RawPLong   float64              `json:"raw_p_long"` // classifier output before the skill gate
	RawPShort  float64              `json:"raw_p_short"`
	PLong      float64              `json:"p_long"` // P(target before stop): raw if the model is skilled, else the base rate
	PShort     float64              `json:"p_short"`
	EdgeLongR  float64              `json:"edge_long_r"`
	EdgeShortR float64              `json:"edge_short_r"`
	BreakevenP float64              `json:"breakeven_p"`
	CostR      float64              `json:"cost_r"`
	Drivers    []model.Contribution `json:"drivers"`
}

// Snapshot is the complete Sentinel output for one closed bar.
type Snapshot struct {
	Symbol      string              `json:"symbol"`
	TF          string              `json:"tf"`
	Time        time.Time           `json:"time"`
	Price       float64             `json:"price"`
	Ready       bool                `json:"ready"`
	Warmup      string              `json:"warmup,omitempty"`
	Score       float64             `json:"score"` // -100 (strong short) .. +100 (strong long)
	Regime      Regime              `json:"regime"`
	Base        Reading             `json:"base"`
	Mid         Reading             `json:"mid"`
	High        Reading             `json:"high"`
	Confluence  Confluence          `json:"confluence"`
	Model       ModelView           `json:"model"`
	Forecast    ForecastView        `json:"forecast"`
	Signal      Signal              `json:"signal"`
	Context     Context             `json:"context"`
	Derivatives *market.Derivatives `json:"derivatives,omitempty"`
	Options     *market.Options     `json:"options,omitempty"`
}

// ForecastView turns the volatility forecast into price ranges.
type ForecastView struct {
	model.Forecast
	Range68 [2]float64 `json:"range68"` // 16%..84% band of the close after the horizon (empirically calibrated)
	Range90 [2]float64 `json:"range90"` // 5%..95% band
	State   string     `json:"state"`   // expanding, contracting, stable
}

func forecastView(f model.Forecast, price float64) ForecastView {
	v := ForecastView{Forecast: f, State: "stable"}
	if !f.Ready || price <= 0 {
		return v
	}
	v.Range68 = [2]float64{price * math.Exp(f.Z16*f.SigmaHorizon), price * math.Exp(f.Z84*f.SigmaHorizon)}
	v.Range90 = [2]float64{price * math.Exp(f.Z05*f.SigmaHorizon), price * math.Exp(f.Z95*f.SigmaHorizon)}
	switch {
	case f.Expansion >= 1.15:
		v.State = "expanding"
	case f.Expansion <= 0.87:
		v.State = "contracting"
	}
	return v
}

// Refresh re-evaluates the latest closed bar with the current derivatives and
// options context, without creating a new label or touching de-duplication
// state. Use it after SetDerivatives/SetOptions to see their effect at once.
func (e *Engine) Refresh() Snapshot {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.lastBar.Time.IsZero() {
		return e.last
	}
	s := e.evaluate(e.lastBar, e.base.Last(), false)
	// A refresh never turns an emitted signal off or invents a new one.
	if e.last.Signal.Emitted && s.Signal.Action == e.last.Signal.Action {
		s.Signal.Emitted, s.Signal.Actionable, s.Signal.Suppressed = true, e.last.Signal.Actionable, e.last.Signal.Suppressed
	}
	e.last = s
	return s
}

// evaluate scores the bar. commit is false for refreshes, which must not
// record labels or update de-duplication state.
func (e *Engine) evaluate(b market.Bar, br Reading, commit bool) Snapshot {
	mr, hr := e.midF.Last(), e.hiF.Last()
	s := Snapshot{Symbol: e.cfg.Symbol, TF: e.cfg.Base.Name, Time: b.Time, Price: b.Close, Base: br, Mid: mr, High: hr,
		Derivatives: e.der, Options: e.opt, Forecast: forecastView(e.fc, b.Close)}
	s.Ready = br.Ready && mr.Ready && hr.Ready
	if !s.Ready {
		s.Warmup = fmt.Sprintf("base %d/%d, %s %d/%d, %s %d/%d bars", br.Bars, warmupBars, mr.TF, mr.Bars, warmupBars, e.high.Name, hr.Bars, warmupBars)
		if hr.TF == "" {
			s.Warmup = fmt.Sprintf("base %d/%d, %s %d/%d, %s 0/%d bars", br.Bars, warmupBars, e.mid.Name, mr.Bars, warmupBars, e.high.Name, warmupBars)
		}
		s.Signal = Signal{Action: "NEUTRAL", Tier: "-", Summary: "warming up: " + s.Warmup}
		s.Regime = Regime{Kind: RegimeUnknown, Description: "warming up"}
		return s
	}

	s.Regime = classifyRegime(mr, hr)
	if e.ref != nil && e.ref.Symbol != e.cfg.Symbol && !e.ref.Time.IsZero() {
		s.Context = Context{Available: true, RefSymbol: e.ref.Symbol, RelStrength: mr.ROC - e.ref.MidROC,
			RefTrend: e.ref.Trend, RefShock: e.ref.Shock, RefReturnZ: e.ref.ReturnZ}
	}
	s.Confluence = score(br, mr, hr, s.Regime, s.Context, e.der, e.opt, b.Time.Add(e.cfg.Base.Dur))

	// Model: predict now (test), learn later when the barrier resolves (train).
	exp := 1.0
	if e.fc.Ready {
		exp = e.fc.Expansion
	}
	x := buildFeatures(br, mr, hr, s.Regime, s.Context, exp)
	logitL, rawL := e.models.Long.Predict(x)
	logitS, rawS := e.models.Short.Predict(x)
	sl := e.cfg.StopATR * br.ATR
	costR := e.cfg.CostR(b.Close, sl)
	samples := min(e.models.Long.Samples(), e.models.Short.Samples())
	stL, stS := e.models.Long.Status(e.cfg.WarmSamples), e.models.Short.Status(e.cfg.WarmSamples)
	// The skill gate: an unskilled model says the base rate, nothing more.
	pL, pS := stL.BaseRate, stS.BaseRate
	if stL.Active {
		pL = rawL
	}
	if stS.Active {
		pS = rawS
	}
	edgeL := pL*e.cfg.TargetR + (1-pL)*e.models.NonWinR(1) - costR
	edgeS := pS*e.cfg.TargetR + (1-pS)*e.models.NonWinR(-1) - costR
	// Breakeven win probability when a loss costs ~1R plus fees.
	be := (1 + costR) / (e.cfg.TargetR + 1)
	s.Model = ModelView{Warm: samples >= e.cfg.WarmSamples, Samples: samples, Long: stL, Short: stS, RawPLong: rawL, RawPShort: rawS,
		PLong: pL, PShort: pS, EdgeLongR: edgeL, EdgeShortR: edgeS, BreakevenP: be, CostR: costR}
	if stL.Active || stS.Active {
		if pL >= pS {
			s.Model.Drivers = e.models.Long.Explain(x, 6)
		} else {
			s.Model.Drivers = e.models.Short.Explain(x, 6)
		}
	}

	s.Signal = e.decide(&s, b, br, sl, costR, commit)
	s.Score = composite(s)

	if sl > 0 && commit {
		e.pend = append(e.pend, &pending{idx: e.idx, time: b.Time, entry: b.Close, sl: sl, costR: costR, x: x,
			logitL: logitL, pL: rawL, logitS: logitS, pS: rawS, emitted: emittedDir(s.Signal)})
	}
	return s
}

func emittedDir(sig Signal) int {
	if sig.Emitted && (sig.Action == "LONG" || sig.Action == "SHORT") {
		return sig.Direction
	}
	return 0
}

func composite(s Snapshot) float64 {
	c := s.Confluence
	conf := 0.0
	if t := c.Long + c.Short; t > 0 {
		conf = (c.Long - c.Short) / (t + 3) // +3 shrinks low-evidence readings toward 0
	}
	if !s.Model.Long.Active && !s.Model.Short.Active {
		return math.Round(clamp(conf, -1, 1)*1000) / 10
	}
	m := clamp((s.Model.PLong-s.Model.PShort)/math.Max(s.Model.BreakevenP, 0.2), -1, 1)
	return math.Round(clamp(0.5*conf+0.5*m, -1, 1)*1000) / 10
}

func (e *Engine) decide(s *Snapshot, b market.Bar, br Reading, sl, costR float64, commit bool) Signal {
	c := s.Confluence
	dir := c.Direction
	sig := Signal{Action: "NEUTRAL", Tier: "-", EdgeStatus: "n/a"}
	if dir == 0 || sl <= 0 {
		sig.Summary = "no directional confluence"
		return sig
	}
	win, lose, layers := c.side(dir)
	layer := func(name string) LayerScore {
		for _, l := range c.Layers {
			if l.Layer == name {
				return l
			}
		}
		return LayerScore{}
	}
	sideOf := func(l LayerScore) float64 {
		if dir > 0 {
			return l.Long
		}
		return l.Short
	}
	minScore := e.cfg.MinScore
	if s.Regime.Kind == RegimeVolatile {
		minScore++
	}
	var gate []GateCheck
	gate = append(gate, GateCheck{"score", win >= minScore, fmt.Sprintf("%s score %.2f vs minimum %.1f", dirWord(dir), win, minScore)})
	gate = append(gate, GateCheck{"layers", layers >= e.cfg.MinLayers, fmt.Sprintf("%d of %d layers agree (need %d)", layers, len(Layers), e.cfg.MinLayers)})

	trendRoute := sideOf(layer(LayerTrend)) >= 1 && sideOf(layer(LayerMomentum)) >= 1
	revRoute := sideOf(layer(LayerReversal)) >= 1.5 && (sideOf(layer(LayerTiming)) >= 0.5 || sideOf(layer(LayerMomentum)) >= 0.75)
	route := ""
	switch {
	case trendRoute:
		route = "trend"
	case revRoute:
		route = "reversal"
	}
	gate = append(gate, GateCheck{"route", route != "", routeDetail(route)})

	ratio := 0.0
	if win > 0 {
		ratio = lose / win
	}
	gate = append(gate, GateCheck{"disagreement", ratio <= e.cfg.MaxDisagreement, fmt.Sprintf("opposing score %.2f is %.0f%% of the winning score (cap %.0f%%)", lose, ratio*100, e.cfg.MaxDisagreement*100)})
	shockOK := !(s.Context.Available && s.Context.RefShock == -dir)
	gate = append(gate, GateCheck{"reference_shock", shockOK, shockDetail(s.Context, dir)})

	pDir, edge, st := s.Model.PLong, s.Model.EdgeLongR, s.Model.Long
	if dir < 0 {
		pDir, edge, st = s.Model.PShort, s.Model.EdgeShortR, s.Model.Short
	}
	modelOK := !st.Active || edge >= e.cfg.MinEdgeR
	md := fmt.Sprintf("model abstains (%s: %d labels, recent AUC %.3f, Brier skill %+.3f): confluence-only", st.Reason, st.Samples, st.RecentAUC, st.RecentBSS)
	if st.Active {
		md = fmt.Sprintf("skilled model: P(target first) %.1f%% vs breakeven %.1f%%, net edge %+.2fR (need %+.2fR)", pDir*100, s.Model.BreakevenP*100, edge, e.cfg.MinEdgeR)
	}
	gate = append(gate, GateCheck{"model_edge", modelOK, md})
	sig.Gate = gate

	allConf := true
	for _, g := range gate[:5] {
		allConf = allConf && g.Pass
	}
	plan := e.plan(dir, b.Close, sl, costR, pDir, edge, s)

	switch {
	case allConf && modelOK:
		sig.Action, sig.Direction, sig.Route = actionWord(dir), dir, route
		sig.Plan = plan
	case !allConf && st.Active && edge >= 2*e.cfg.MinEdgeR && pDir > s.Model.BreakevenP:
		sig.Action, sig.Direction = "WATCH_"+actionWord(dir), dir
		sig.Plan = plan
	default:
		sig.Direction = 0
	}

	sig.Track, sig.EdgeStatus = trackRecord(e.track[sideIdx(dir)])
	sig.Confidence = e.confidence(s, dir, win, lose, layers, pDir, st.Active, sig.EdgeStatus, plan)
	sig.Tier = tier(sig.Confidence)
	if sig.Action == "NEUTRAL" {
		sig.Tier = "-"
	}

	if sig.Action == "LONG" || sig.Action == "SHORT" {
		i := sideIdx(dir)
		last := e.lastSig[i]
		if !commit && last.set && last.idx == e.idx {
			last.set = false // the original evaluation of this bar fired it
		}
		if last.set && e.idx-last.idx < e.cfg.CooldownBars && math.Abs(b.Close-last.price) < e.cfg.CooldownATR*br.ATR {
			sig.Suppressed = fmt.Sprintf("repeat of the %s signal %d bars ago at %.6g; price has not moved %.1f ATR", actionWord(dir), e.idx-last.idx, last.price, e.cfg.CooldownATR)
		} else {
			sig.Emitted = commit
			if commit {
				e.lastSig[i] = struct {
					idx   int
					price float64
					set   bool
				}{e.idx, b.Close, true}
			}
			sig.Actionable = commit
			if commit && e.cfg.RequireProvenEdge && sig.EdgeStatus != "proven" && !st.Active {
				sig.Actionable = false
				sig.Suppressed = fmt.Sprintf("strict mode: %s edge is %s (%d fired signals, expectancy %+.2fR) and the model has no demonstrated skill", actionWord(dir), sig.EdgeStatus, sig.Track.N, sig.Track.ExpectancyR)
			}
		}
	}
	sig.Summary = summary(sig, s)
	return sig
}

func (e *Engine) plan(dir int, entry, sl, costR, p, edge float64, s *Snapshot) *TradePlan {
	tp := e.cfg.TargetR * sl
	pl := &TradePlan{Side: actionWord(dir), Entry: entry, StopPct: sl / entry * 100, RewardRisk: e.cfg.TargetR,
		CostR: costR, ExpectedR: edge, HorizonBars: e.cfg.Horizon}
	if dir > 0 {
		pl.Stop, pl.TP1, pl.TP2 = entry-sl, entry+sl, entry+tp
		if r := s.Mid.Resistance; r > entry && r < pl.TP2 {
			pl.Obstruction = fmt.Sprintf("%s resistance %.6g sits before TP2", s.Mid.TF, r)
		}
	} else {
		pl.Stop, pl.TP1, pl.TP2 = entry+sl, entry-sl, entry-tp
		if sp := s.Mid.Support; sp > 0 && sp < entry && sp > pl.TP2 {
			pl.Obstruction = fmt.Sprintf("%s support %.6g sits before TP2", s.Mid.TF, sp)
		}
		pl.SpotNote = "OKX US is spot-only: treat SHORT as exit / avoid longs unless trading a venue that allows shorting"
	}
	if (dir > 0 && s.Model.Long.Active) || (dir < 0 && s.Model.Short.Active) {
		// Kelly for a binary bet paying (T - cost) and losing (1 + cost).
		b := (e.cfg.TargetR - costR) / (1 + costR)
		if b > 0 {
			pl.KellyFraction = math.Max(0, p-(1-p)/b)
		}
		pl.RiskPct = math.Min(e.cfg.MaxRiskPct, 25*pl.KellyFraction) // quarter Kelly, in percent
	} else {
		pl.RiskPct = math.Min(e.cfg.MaxRiskPct, 0.5)
	}
	pl.RiskPct = math.Round(pl.RiskPct*100) / 100
	return pl
}

func (e *Engine) confidence(s *Snapshot, dir int, win, lose float64, layers int, p float64, active bool, edgeStatus string, plan *TradePlan) float64 {
	pPart := 0.5
	if active {
		pPart = clamp((p-s.Model.BreakevenP)/0.2+0.5, 0, 1)
	}
	cPart := 0.5*clamp((win-e.cfg.MinScore)/6+0.5, 0, 1) + 0.5*clamp(float64(layers)/5, 0, 1)
	dis := 0.0
	if win > 0 {
		dis = clamp(lose/win, 0, 1)
	}
	conf := 100 * (0.45*pPart + 0.35*cPart + 0.20*(1-dis))
	if plan != nil && plan.Obstruction != "" {
		conf -= 10
	}
	if s.Regime.HighTrend == -dir {
		conf -= 10 // counter-trend setups get one step of caution
	}
	if s.Context.Available && s.Context.RefShock != 0 {
		conf -= 10
	}
	// Evidence caps: confluence without a skilled model or a proven record
	// cannot reach the top tiers, and a negative live record caps at D.
	switch {
	case edgeStatus == "negative" && !active:
		conf = math.Min(conf, 40)
	case edgeStatus != "proven" && !active:
		conf = math.Min(conf, 59)
	}
	return math.Round(clamp(conf, 0, 100)*10) / 10
}

func tier(c float64) string {
	switch {
	case c >= 75:
		return "A"
	case c >= 60:
		return "B"
	case c >= 45:
		return "C"
	}
	return "D"
}

func actionWord(dir int) string {
	if dir > 0 {
		return "LONG"
	}
	return "SHORT"
}

func routeDetail(r string) string {
	switch r {
	case "trend":
		return "trend route: higher-timeframe trend and momentum both confirm"
	case "reversal":
		return "reversal route: exhaustion cluster with timing or momentum confirmation"
	}
	return "neither the trend route nor the reversal route is confirmed"
}

func shockDetail(c Context, dir int) string {
	if !c.Available {
		return "no reference asset"
	}
	if c.RefShock == -dir {
		return fmt.Sprintf("%s just moved %.1f sigma against this direction", c.RefSymbol, math.Abs(c.RefReturnZ))
	}
	return fmt.Sprintf("%s return z-score %.1f", c.RefSymbol, c.RefReturnZ)
}

func summary(sig Signal, s *Snapshot) string {
	switch sig.Action {
	case "NEUTRAL":
		failed := []string{}
		for _, g := range sig.Gate {
			if !g.Pass {
				failed = append(failed, g.Name)
			}
		}
		if len(failed) == 0 {
			return "no setup"
		}
		return fmt.Sprintf("no signal: %v failed (regime %s)", failed, s.Regime.Kind)
	case "WATCH_LONG", "WATCH_SHORT":
		return fmt.Sprintf("model sees a %s edge of %+.2fR but confluence is incomplete", dirWord(sig.Direction), pick(sig.Direction, s.Model.EdgeLongR, s.Model.EdgeShortR))
	}
	out := fmt.Sprintf("%s via the %s route, tier %s (%.0f), regime %s", sig.Action, sig.Route, sig.Tier, sig.Confidence, s.Regime.Kind)
	if sig.Suppressed != "" {
		out += " (suppressed: repeat)"
	}
	return out
}

func pick(dir int, a, b float64) float64 {
	if dir > 0 {
		return a
	}
	return b
}
