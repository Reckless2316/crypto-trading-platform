package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"text/tabwriter"
	"time"

	"github.com/anthropics/anthropic-sdk-go/v2"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/agent"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/desk"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/engine"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/feed"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/ledger"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/mcp"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/server"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/tools"
)

// Version is set at build time with -ldflags "-X main.Version=...".
var Version = "1.0.0"

const defaultUniverse = "BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,DOGE-USDT,LINK-USDT,AVAX-USDT,LTC-USDT"

func newService(ef *engineFlags, universe []string, ref string) (*tools.Service, error) {
	cfg, err := ef.config()
	if err != nil {
		return nil, err
	}
	return &tools.Service{Config: cfg, OKX: ef.okx(), Deribit: feed.NewDeribit(), Ref: ref, Universe: universe}, nil
}

func signalContext() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
}

func cmdAnalyze(args []string) error {
	fs := flag.NewFlagSet("analyze", flag.ExitOnError)
	var ef engineFlags
	ef.register(fs)
	sym := fs.String("symbol", "BTC-USDT", "OKX spot instrument")
	ref := fs.String("ref", "BTC-USDT", "reference symbol for cross-asset context")
	full := fs.Bool("full", false, "print the full snapshot JSON instead of the brief")
	asText := fs.Bool("text", false, "print a human-readable summary")
	_ = fs.Parse(args)
	svc, err := newService(&ef, nil, *ref)
	if err != nil {
		return err
	}
	ctx, cancel := signalContext()
	defer cancel()
	snap, err := svc.Snapshot(ctx, *sym, ef.tf)
	if err != nil {
		return err
	}
	if *full {
		return writeJSON(os.Stdout, snap)
	}
	b := tools.MakeBrief(snap)
	if !*asText {
		return writeJSON(os.Stdout, b)
	}
	fmt.Print(textBrief(b))
	return nil
}

func textBrief(b tools.Brief) string {
	var s strings.Builder
	fmt.Fprintf(&s, "%s %s  close %.8g  (bar %s UTC)\n", b.Symbol, b.TF, b.Price, b.BarTime.Format("2006-01-02 15:04"))
	if !b.Ready {
		fmt.Fprintf(&s, "warming up: %s\n", b.Warmup)
		return s.String()
	}
	sg := b.Signal
	fmt.Fprintf(&s, "Sentinel score %+.1f   regime %s   signal %s (confidence %.0f, tier %s, edge %s over %d fired)\n",
		b.Score, b.Regime.Kind, sg.Action, sg.Confidence, sg.Tier, sg.EdgeStatus, sg.Track.N)
	fmt.Fprintf(&s, "  %s\n", sg.Summary)
	f := b.Forecast
	fmt.Fprintf(&s, "Volatility next %d bars: 1-sigma %.2f%% (%s x%.2f)  68%% %.8g..%.8g  90%% %.8g..%.8g  [R^2 %.3f vs naive %.3f]\n",
		f.HorizonBars, f.SigmaHorizonPc, f.State, f.Expansion, f.Range68[0], f.Range68[1], f.Range90[0], f.Range90[1], f.R2, f.R2Naive)
	m := b.Model
	fmt.Fprintf(&s, "Direction model: long %s (AUC %.3f), short %s (AUC %.3f); P(target) long %.1f%% short %.1f%%; costs %.2fR\n",
		active(m.LongActive), m.LongAUC, active(m.ShortActive), m.ShortAUC, m.PLong*100, m.PShort*100, m.CostR)
	fmt.Fprintf(&s, "Confluence long %.2f (%d layers) vs short %.2f (%d layers)\n", b.Confluence.Long, b.Confluence.LayersLong, b.Confluence.Short, b.Confluence.LayersShort)
	for _, v := range append(append([]engine.Vote{}, b.Confluence.TopLong...), b.Confluence.TopShort...) {
		side := "LONG "
		if v.Side < 0 {
			side = "SHORT"
		}
		fmt.Fprintf(&s, "  %s %-15s %.2f  %s\n", side, v.Layer, v.Weight, v.Detail)
	}
	s.WriteString("Gate:\n")
	for _, g := range sg.Gate {
		mark := "PASS"
		if !g.Pass {
			mark = "FAIL"
		}
		fmt.Fprintf(&s, "  %s %-16s %s\n", mark, g.Name, g.Detail)
	}
	if p := sg.Plan; p != nil {
		fmt.Fprintf(&s, "Plan %s: entry %.8g stop %.8g tp1 %.8g tp2 %.8g  cost %.2fR  expected %+.2fR  risk %.2f%% equity\n",
			p.Side, p.Entry, p.Stop, p.TP1, p.TP2, p.CostR, p.ExpectedR, p.RiskPct)
	}
	return s.String()
}

func active(b bool) string {
	if b {
		return "ACTIVE"
	}
	return "abstains"
}

func cmdScan(args []string) error {
	fs := flag.NewFlagSet("scan", flag.ExitOnError)
	var ef engineFlags
	ef.register(fs)
	syms := fs.String("symbols", defaultUniverse, "comma-separated OKX spot instruments (max 25)")
	ref := fs.String("ref", "BTC-USDT", "reference symbol")
	asJSON := fs.Bool("json", false, "print JSON")
	_ = fs.Parse(args)
	svc, err := newService(&ef, splitSymbols(*syms), *ref)
	if err != nil {
		return err
	}
	ctx, cancel := signalContext()
	defer cancel()
	rows, err := svc.Scan(ctx, nil, ef.tf)
	if err != nil {
		return err
	}
	if *asJSON {
		return writeJSON(os.Stdout, rows)
	}
	tw := tabwriter.NewWriter(os.Stdout, 0, 2, 2, ' ', 0)
	fmt.Fprintln(tw, "SYMBOL\tPRICE\tSCORE\tACTION\tCONF\tTIER\tEDGE\tREGIME\tVOL\tSIGMA%")
	for _, r := range rows {
		fmt.Fprintf(tw, "%s\t%.8g\t%+.1f\t%s\t%.0f\t%s\t%s\t%s\t%s\t%.2f\n", r.Symbol, r.Price, r.Score, r.Action, r.Confidence, r.Tier, r.EdgeStatus, r.Regime, r.VolState, r.SigmaPct)
	}
	return tw.Flush()
}

func cmdServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	var ef engineFlags
	ef.register(fs)
	syms := fs.String("symbols", defaultUniverse, "live universe (OKX spot instruments)")
	ref := fs.String("ref", "BTC-USDT", "reference symbol (added to the universe if missing)")
	addr := fs.String("addr", "127.0.0.1:8090", "listen address (keep it on localhost unless you set -token)")
	token := fs.String("token", os.Getenv("SENTINEL_TOKEN"), "bearer token required for POST endpoints (env SENTINEL_TOKEN)")
	ledgerPath := fs.String("ledger", "data/sentinel-ledger.jsonl", "hash-chained signal ledger (empty to disable)")
	bars := fs.Int("bars", 0, "warm-up history per symbol in base bars (0 = automatic)")
	wsURL := fs.String("ws", feed.OKXBusinessWS, "OKX business websocket URL")
	poll := fs.Bool("poll", false, "use REST polling instead of the websocket")
	noAgent := fs.Bool("no-agent", false, "disable the analyst endpoint even if an API key is present")
	strict := fs.Bool("strict", false, "only mark signals actionable once their edge is proven (or the model is skilled)")
	verbose := fs.Bool("v", false, "debug logging")
	_ = fs.Parse(args)
	log := logger(*verbose)

	cfg, err := ef.config()
	if err != nil {
		return err
	}
	cfg.RequireProvenEdge = *strict
	universe := splitSymbols(*syms)
	if *ref != "" && !contains(universe, *ref) {
		universe = append([]string{*ref}, universe...)
	}
	if host, _, err := net.SplitHostPort(*addr); err == nil && host != "127.0.0.1" && host != "localhost" && host != "::1" && *token == "" {
		return errors.New("refusing to listen on a non-loopback address without -token")
	}

	ctx, cancel := signalContext()
	defer cancel()
	okx := ef.okx()
	n := *bars
	if n == 0 {
		n = tools.HistoryBars(cfg.Base)
	}
	log.Info("warming up", "symbols", universe, "tf", cfg.Base.Name, "bars", n)
	hist, err := fetchAll(ctx, okx, universe, cfg.Base, n)
	if err != nil {
		return err
	}
	d := desk.New(universe, *ref, cfg, log)
	d.Warmup(hist)
	log.Info("warm-up complete", "model_samples", d.Models.Long.Samples())

	if *ledgerPath != "" {
		if err := os.MkdirAll(filepath.Dir(*ledgerPath), 0o755); err != nil {
			return err
		}
		lg, err := ledger.Open(*ledgerPath)
		if err != nil {
			return err
		}
		defer lg.Close()
		d.Ledger = lg
		seq, head := lg.Head()
		log.Info("ledger open", "path", *ledgerPath, "entries", seq, "head", head)
	}

	svc := &tools.Service{Live: d, Config: cfg, OKX: okx, Deribit: feed.NewDeribit(), Ref: *ref, LedgerPath: *ledgerPath, Universe: universe}
	srv := &server.Server{Desk: d, Service: svc, Token: *token, Log: log}
	if !*noAgent && haveAnthropicCredentials() {
		srv.Agent = agent.New(svc.Tools())
		log.Info("analyst agent enabled", "model", agent.Model)
	} else {
		log.Info("analyst agent disabled (no ANTHROPIC_API_KEY)")
	}

	events := make(chan feed.BarEvent, 256)
	go func() {
		var err error
		if *poll {
			err = feed.Poll(ctx, okx, universe, cfg.Base, events, log)
		} else {
			err = feed.Stream(ctx, *wsURL, okx, universe, cfg.Base, events, log)
		}
		if err != nil && ctx.Err() == nil {
			log.Error("live feed stopped", "err", err)
		}
	}()
	go d.Run(ctx, events)
	go d.RefreshContext(ctx, okx, feed.NewDeribit())

	hs := &http.Server{Addr: *addr, Handler: srv.Handler(), ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		sctx, c := context.WithTimeout(context.Background(), 5*time.Second)
		defer c()
		_ = hs.Shutdown(sctx)
	}()
	log.Info("serving", "url", "http://"+*addr)
	if err := hs.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

// haveAnthropicCredentials reports whether the Anthropic SDK can authenticate:
// an API key, an auth token, or an `ant auth login` profile on disk.
func haveAnthropicCredentials() bool {
	if os.Getenv("ANTHROPIC_API_KEY") != "" || os.Getenv("ANTHROPIC_AUTH_TOKEN") != "" {
		return true
	}
	if home, err := os.UserHomeDir(); err == nil {
		if _, err := os.Stat(filepath.Join(home, ".config", "anthropic")); err == nil {
			return true
		}
	}
	return false
}

func effortOf(s string) anthropic.BetaOutputConfigEffort {
	switch strings.ToLower(s) {
	case "low":
		return anthropic.BetaOutputConfigEffortLow
	case "medium":
		return anthropic.BetaOutputConfigEffortMedium
	case "xhigh":
		return anthropic.BetaOutputConfigEffortXhigh
	case "max":
		return anthropic.BetaOutputConfigEffortMax
	}
	return anthropic.BetaOutputConfigEffortHigh
}

func cmdAgent(args []string) error {
	fs := flag.NewFlagSet("agent", flag.ExitOnError)
	var ef engineFlags
	ef.register(fs)
	sym := fs.String("symbol", "BTC-USDT", "OKX spot instrument")
	ref := fs.String("ref", "BTC-USDT", "reference symbol")
	model := fs.String("model", agent.Model, "Claude model id")
	effort := fs.String("effort", "high", "reasoning effort: low, medium, high, xhigh, max")
	noFallback := fs.Bool("no-fallback", false, "disable server-side refusal fallbacks")
	asJSON := fs.Bool("json", false, "print the JSON result")
	verbose := fs.Bool("v", false, "log tool calls to stderr")
	_ = fs.Parse(args)
	if !haveAnthropicCredentials() {
		return errors.New("set ANTHROPIC_API_KEY (or log in with `ant auth login`) to use the analyst agent")
	}
	svc, err := newService(&ef, splitSymbols(defaultUniverse), *ref)
	if err != nil {
		return err
	}
	a := agent.New(svc.Tools())
	a.Model = *model
	a.Effort = effortOf(*effort)
	a.UseFallbacks = !*noFallback
	if *verbose {
		a.Log = func(f string, args ...any) { fmt.Fprintf(os.Stderr, f+"\n", args...) }
	}
	ctx, cancel := signalContext()
	defer cancel()
	res, err := a.Run(ctx, strings.ToUpper(*sym), ef.tf, strings.Join(fs.Args(), " "))
	if err != nil {
		return err
	}
	if *asJSON {
		return writeJSON(os.Stdout, res)
	}
	fmt.Print(res.Render())
	return nil
}

func cmdMCP(args []string) error {
	fs := flag.NewFlagSet("mcp", flag.ExitOnError)
	var ef engineFlags
	ef.register(fs)
	ref := fs.String("ref", "BTC-USDT", "reference symbol")
	universe := fs.String("symbols", defaultUniverse, "default scan universe")
	ledgerPath := fs.String("ledger", "", "signal ledger to expose through signal_ledger (optional)")
	_ = fs.Parse(args)
	svc, err := newService(&ef, splitSymbols(*universe), *ref)
	if err != nil {
		return err
	}
	svc.LedgerPath = *ledgerPath
	ctx, cancel := signalContext()
	defer cancel()
	s := &mcp.Server{Name: "surge-sentinel", Version: Version, Tools: svc.Tools()}
	return s.Serve(ctx, os.Stdin, os.Stdout)
}
