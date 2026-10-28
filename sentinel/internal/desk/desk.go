// Package desk coordinates Sentinel engines across a universe of symbols:
// time-ordered warmup with a shared (pooled) model, the reference-asset (BTC)
// context, periodic derivatives/options refreshes, live bar routing, the
// signal ledger and snapshot subscribers.
package desk

import (
	"context"
	"log/slog"
	"sort"
	"sync"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/engine"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/feed"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/ledger"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// Desk owns one engine per symbol.
type Desk struct {
	Base   market.Timeframe
	Ref    string // reference symbol for cross-asset context, e.g. BTC-USDT
	Models *engine.Models
	Ledger *ledger.Ledger
	Log    *slog.Logger

	mu       sync.RWMutex
	engines  map[string]*engine.Engine
	order    []string
	refState *engine.RefState
	subs     map[chan engine.Snapshot]struct{}
}

// New creates a desk for symbols. cfg is applied to every engine (Symbol is set per engine).
func New(symbols []string, ref string, cfg engine.Config, log *slog.Logger) *Desk {
	d := &Desk{Base: cfg.Base, Ref: ref, Models: engine.NewModels(cfg.Model), Log: log,
		engines: map[string]*engine.Engine{}, subs: map[chan engine.Snapshot]struct{}{}}
	for _, s := range symbols {
		c := cfg
		c.Symbol = s
		d.engines[s] = engine.New(c, d.Models)
		d.order = append(d.order, s)
	}
	return d
}

// Symbols returns the universe in configured order.
func (d *Desk) Symbols() []string { return append([]string(nil), d.order...) }

// Engine returns a symbol's engine.
func (d *Desk) Engine(sym string) (*engine.Engine, bool) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	e, ok := d.engines[sym]
	return e, ok
}

// Snapshots returns the latest snapshot of every symbol.
func (d *Desk) Snapshots() []engine.Snapshot {
	out := make([]engine.Snapshot, 0, len(d.order))
	for _, s := range d.order {
		if e, ok := d.Engine(s); ok {
			out = append(out, e.Last())
		}
	}
	return out
}

// Subscribe returns a channel receiving every new snapshot; cancel to stop.
func (d *Desk) Subscribe() (<-chan engine.Snapshot, func()) {
	ch := make(chan engine.Snapshot, 64)
	d.mu.Lock()
	d.subs[ch] = struct{}{}
	d.mu.Unlock()
	return ch, func() {
		d.mu.Lock()
		if _, ok := d.subs[ch]; ok {
			delete(d.subs, ch)
			close(ch)
		}
		d.mu.Unlock()
	}
}

type tsBar struct {
	sym string
	bar market.Bar
}

// Warmup replays history for every symbol merged in global time order (the
// reference symbol first at equal timestamps), so the pooled model only ever
// learns from labels that were resolvable at each point in time. Warmup
// signals are never written to the ledger.
func (d *Desk) Warmup(hist map[string][]market.Bar) {
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
		return all[i].sym == d.Ref && all[j].sym != d.Ref
	})
	for _, tb := range all {
		d.process(tb.sym, tb.bar, false)
	}
	for _, s := range d.order {
		if e, ok := d.Engine(s); ok {
			e.DrainOutcomes()
		}
	}
}

// OnBar routes one live closed bar.
func (d *Desk) OnBar(sym string, b market.Bar) (engine.Snapshot, bool) {
	return d.process(sym, b, true)
}

func (d *Desk) process(sym string, b market.Bar, live bool) (engine.Snapshot, bool) {
	e, ok := d.Engine(sym)
	if !ok {
		return engine.Snapshot{}, false
	}
	if last := e.Last(); !last.Time.IsZero() && !b.Time.After(last.Time) {
		return last, false // duplicate or out-of-order bar
	}
	d.mu.RLock()
	ref := d.refState
	d.mu.RUnlock()
	if sym == d.Ref {
		ref = nil
	}
	snap := e.Update(b, ref)
	if sym == d.Ref {
		rs := e.RefState()
		d.mu.Lock()
		d.refState = &rs
		d.mu.Unlock()
	}
	if live {
		d.journal(e, snap)
		d.publish(snap)
	}
	return snap, true
}

func (d *Desk) journal(e *engine.Engine, snap engine.Snapshot) {
	if d.Ledger == nil {
		return
	}
	if snap.Signal.Emitted {
		rec := map[string]any{
			"symbol": snap.Symbol, "tf": snap.TF, "bar_time": snap.Time, "action": snap.Signal.Action,
			"route": snap.Signal.Route, "confidence": snap.Signal.Confidence, "tier": snap.Signal.Tier,
			"plan": snap.Signal.Plan, "score": snap.Score, "p_long": snap.Model.PLong, "p_short": snap.Model.PShort,
			"model_warm": snap.Model.Warm, "regime": snap.Regime.Kind,
		}
		if _, err := d.Ledger.Append("signal", rec); err != nil && d.Log != nil {
			d.Log.Error("ledger append failed", "err", err)
		}
	}
	for _, o := range e.DrainOutcomes() {
		if !o.Emitted {
			continue
		}
		if _, err := d.Ledger.Append("outcome", o); err != nil && d.Log != nil {
			d.Log.Error("ledger append failed", "err", err)
		}
	}
}

func (d *Desk) publish(s engine.Snapshot) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	for ch := range d.subs {
		select {
		case ch <- s:
		default: // slow subscriber: drop rather than block the engine
		}
	}
}

// RefreshContext polls derivatives (every 5 minutes) and options (every 15
// minutes) until ctx ends.
func (d *Desk) RefreshContext(ctx context.Context, okx *feed.OKX, der *feed.Deribit) {
	tick := time.NewTicker(5 * time.Minute)
	defer tick.Stop()
	n := 0
	for {
		for _, s := range d.order {
			e, _ := d.Engine(s)
			cctx, cancel := context.WithTimeout(ctx, 30*time.Second)
			changed := false
			if dv, err := okx.Derivatives(cctx, s, 4); err == nil {
				e.SetDerivatives(dv)
				changed = true
			} else if d.Log != nil {
				d.Log.Debug("derivatives unavailable", "symbol", s, "err", err)
			}
			if n%3 == 0 && der != nil {
				if cur, ok := der.Supports(s); ok {
					if o, err := der.Options(cctx, cur); err == nil {
						e.SetOptions(o)
						changed = true
					} else if d.Log != nil {
						d.Log.Debug("options unavailable", "symbol", s, "err", err)
					}
				}
			}
			cancel()
			if changed {
				d.publish(e.Refresh())
			}
		}
		n++
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}

// Run consumes live bar events until ctx ends.
func (d *Desk) Run(ctx context.Context, events <-chan feed.BarEvent) {
	for {
		select {
		case <-ctx.Done():
			return
		case ev := <-events:
			if !ev.Closed {
				continue
			}
			if snap, ok := d.OnBar(ev.Symbol, ev.Bar); ok && d.Log != nil && snap.Signal.Emitted {
				d.Log.Info("signal", "symbol", snap.Symbol, "action", snap.Signal.Action, "tier", snap.Signal.Tier,
					"confidence", snap.Signal.Confidence, "price", snap.Price)
			}
		}
	}
}
