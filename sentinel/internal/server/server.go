// Package server exposes the live desk over HTTP: JSON snapshots, a
// server-sent-events stream, the tool endpoints, the analyst agent and an
// embedded dashboard.
package server

import (
	"context"
	"crypto/subtle"
	_ "embed"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/agent"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/desk"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/tools"
)

//go:embed dashboard.html
var dashboardHTML []byte

// Server is the HTTP front end.
type Server struct {
	Desk    *desk.Desk
	Service *tools.Service
	Agent   *agent.Agent // nil when no API key is configured
	Token   string       // optional bearer token for POST endpoints
	Log     *slog.Logger

	agentMu sync.Mutex // one agent run at a time (each costs API tokens)
}

// Handler builds the route table.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", s.dashboard)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "symbols": s.Desk.Symbols(), "timeframe": s.Desk.Base.Name, "agent": s.Agent != nil})
	})
	mux.HandleFunc("GET /api/briefs", s.briefs)
	mux.HandleFunc("GET /api/snapshot/{symbol}", s.snapshot)
	mux.HandleFunc("GET /api/stream", s.stream)
	mux.HandleFunc("GET /api/tools", s.listTools)
	mux.HandleFunc("POST /api/tools/{name}", s.auth(s.callTool))
	mux.HandleFunc("POST /api/agent", s.auth(s.runAgent))
	mux.HandleFunc("GET /api/ledger", func(w http.ResponseWriter, r *http.Request) {
		out, err := s.Service.LedgerTail(50)
		if err != nil {
			writeErr(w, http.StatusInternalServerError, err)
			return
		}
		writeJSON(w, http.StatusOK, out)
	})
	return securityHeaders(mux)
}

func securityHeaders(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Referrer-Policy", "no-referrer")
		h.ServeHTTP(w, r)
	})
}

// auth protects endpoints that spend money or compute. With no token set the
// server only accepts same-origin browser requests (the dashboard) and
// non-browser clients; cross-site form posts are refused.
func (s *Server) auth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.Token != "" {
			got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			if subtle.ConstantTimeCompare([]byte(got), []byte(s.Token)) != 1 {
				writeErr(w, http.StatusUnauthorized, fmt.Errorf("missing or wrong bearer token"))
				return
			}
		}
		if o := r.Header.Get("Origin"); o != "" && !sameOrigin(o, r.Host) {
			writeErr(w, http.StatusForbidden, fmt.Errorf("cross-origin request refused"))
			return
		}
		if ct := r.Header.Get("Content-Type"); ct != "" && !strings.HasPrefix(ct, "application/json") {
			writeErr(w, http.StatusUnsupportedMediaType, fmt.Errorf("use application/json"))
			return
		}
		next(w, r)
	}
}

func sameOrigin(origin, host string) bool {
	o := strings.TrimPrefix(strings.TrimPrefix(origin, "http://"), "https://")
	return o == host
}

func (s *Server) dashboard(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'")
	w.Write(dashboardHTML)
}

func (s *Server) briefs(w http.ResponseWriter, r *http.Request) {
	snaps := s.Desk.Snapshots()
	out := make([]tools.Brief, 0, len(snaps))
	for _, sn := range snaps {
		out = append(out, tools.MakeBrief(sn))
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) snapshot(w http.ResponseWriter, r *http.Request) {
	sym := strings.ToUpper(r.PathValue("symbol"))
	e, ok := s.Desk.Engine(sym)
	if !ok {
		writeErr(w, http.StatusNotFound, fmt.Errorf("%s is not in the live universe %v", sym, s.Desk.Symbols()))
		return
	}
	writeJSON(w, http.StatusOK, e.Last())
}

// stream pushes a Brief for every new snapshot as server-sent events.
func (s *Server) stream(w http.ResponseWriter, r *http.Request) {
	fl, ok := w.(http.Flusher)
	if !ok {
		writeErr(w, http.StatusInternalServerError, fmt.Errorf("streaming unsupported"))
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	ch, cancel := s.Desk.Subscribe()
	defer cancel()
	send := func(event string, v any) error {
		b, err := json.Marshal(v)
		if err != nil {
			return err
		}
		if _, err := fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event, b); err != nil {
			return err
		}
		fl.Flush()
		return nil
	}
	for _, sn := range s.Desk.Snapshots() {
		if send("brief", tools.MakeBrief(sn)) != nil {
			return
		}
	}
	ping := time.NewTicker(20 * time.Second)
	defer ping.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case sn, ok := <-ch:
			if !ok {
				return
			}
			if send("brief", tools.MakeBrief(sn)) != nil {
				return
			}
		case <-ping.C:
			if _, err := io.WriteString(w, ": ping\n\n"); err != nil {
				return
			}
			fl.Flush()
		}
	}
}

func (s *Server) listTools(w http.ResponseWriter, r *http.Request) {
	var out []map[string]any
	for _, t := range s.Service.Tools() {
		out = append(out, map[string]any{"name": t.Name, "description": t.Description, "input_schema": t.Schema})
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) callTool(w http.ResponseWriter, r *http.Request) {
	t, ok := tools.Find(s.Service.Tools(), r.PathValue("name"))
	if !ok {
		writeErr(w, http.StatusNotFound, fmt.Errorf("unknown tool"))
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<16))
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Minute)
	defer cancel()
	out, err := t.Run(ctx, body)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) runAgent(w http.ResponseWriter, r *http.Request) {
	if s.Agent == nil {
		writeErr(w, http.StatusServiceUnavailable, fmt.Errorf("analyst agent disabled: set ANTHROPIC_API_KEY (or log in with `ant auth login`) and restart"))
		return
	}
	var req struct {
		Symbol    string `json:"symbol"`
		Timeframe string `json:"timeframe"`
		Question  string `json:"question"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&req); err != nil || req.Symbol == "" {
		writeErr(w, http.StatusBadRequest, fmt.Errorf("body must be {\"symbol\": \"BTC-USDT\", \"question\": \"...\"}"))
		return
	}
	if len(req.Question) > 2000 {
		writeErr(w, http.StatusBadRequest, fmt.Errorf("question too long"))
		return
	}
	if !s.agentMu.TryLock() {
		writeErr(w, http.StatusTooManyRequests, fmt.Errorf("an analyst run is already in progress"))
		return
	}
	defer s.agentMu.Unlock()
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Minute)
	defer cancel()
	res, err := s.Agent.Run(ctx, strings.ToUpper(req.Symbol), req.Timeframe, req.Question)
	if err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, code int, err error) {
	writeJSON(w, code, map[string]any{"error": err.Error()})
}
