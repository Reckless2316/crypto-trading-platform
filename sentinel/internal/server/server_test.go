package server

import (
	"bufio"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/desk"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/engine"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/testutil"
	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/tools"
)

func testServer(t *testing.T, token string) (*httptest.Server, *desk.Desk, []market.Bar) {
	t.Helper()
	bars := testutil.GARCH(2400, time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), 15*time.Minute, 5)
	cfg := engine.DefaultConfig("", market.MustTimeframe("15m"))
	d := desk.New([]string{"BTC-USDT"}, "BTC-USDT", cfg, nil)
	d.Warmup(map[string][]market.Bar{"BTC-USDT": bars[:2300]})
	svc := &tools.Service{Live: d, Config: cfg}
	s := &Server{Desk: d, Service: svc, Token: token}
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)
	return ts, d, bars
}

func TestEndpoints(t *testing.T) {
	ts, d, bars := testServer(t, "")
	get := func(path string) (*http.Response, string) {
		r, err := http.Get(ts.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		var sb strings.Builder
		sc := bufio.NewScanner(r.Body)
		for sc.Scan() {
			sb.WriteString(sc.Text())
		}
		r.Body.Close()
		return r, sb.String()
	}
	if r, body := get("/"); r.StatusCode != 200 || !strings.Contains(body, "SURGE SENTINEL") || r.Header.Get("Content-Security-Policy") == "" {
		t.Fatalf("dashboard %d", r.StatusCode)
	}
	var briefs []tools.Brief
	_, body := get("/api/briefs")
	if err := json.Unmarshal([]byte(body), &briefs); err != nil || len(briefs) != 1 || !briefs[0].Ready {
		t.Fatalf("briefs %v %s", err, body[:min(200, len(body))])
	}
	if r, _ := get("/api/snapshot/NOPE-USDT"); r.StatusCode != 404 {
		t.Fatal("unknown symbol")
	}
	// Live analysis from the running desk, through the tool endpoint.
	resp, err := http.Post(ts.URL+"/api/tools/analyze_symbol", "application/json", strings.NewReader(`{"symbol":"BTC-USDT"}`))
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("tool call %v %v", err, resp.StatusCode)
	}
	resp.Body.Close()
	// The agent is disabled without a key.
	resp, _ = http.Post(ts.URL+"/api/agent", "application/json", strings.NewReader(`{"symbol":"BTC-USDT"}`))
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("agent status %d", resp.StatusCode)
	}
	resp.Body.Close()
	// Cross-site requests are refused.
	req, _ := http.NewRequest("POST", ts.URL+"/api/tools/analyze_symbol", strings.NewReader(`{"symbol":"BTC-USDT"}`))
	req.Header.Set("Origin", "https://evil.example")
	req.Header.Set("Content-Type", "application/json")
	resp, _ = http.DefaultClient.Do(req)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("cross-origin status %d", resp.StatusCode)
	}
	resp.Body.Close()

	// SSE: the stream sends current briefs, then one per new closed bar.
	sreq, _ := http.NewRequest("GET", ts.URL+"/api/stream", nil)
	sresp, err := http.DefaultClient.Do(sreq)
	if err != nil {
		t.Fatal(err)
	}
	defer sresp.Body.Close()
	rd := bufio.NewReader(sresp.Body)
	events := 0
	go func() {
		time.Sleep(200 * time.Millisecond)
		d.OnBar("BTC-USDT", bars[2300])
	}()
	deadline := time.Now().Add(5 * time.Second)
	for events < 2 && time.Now().Before(deadline) {
		line, err := rd.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		if strings.HasPrefix(line, "event: brief") {
			events++
		}
	}
	if events < 2 {
		t.Fatalf("got %d SSE events", events)
	}
}

func TestBearerToken(t *testing.T) {
	ts, _, _ := testServer(t, "s3cret")
	resp, _ := http.Post(ts.URL+"/api/tools/scan_market", "application/json", strings.NewReader(`{}`))
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("no token: %d", resp.StatusCode)
	}
	resp.Body.Close()
	req, _ := http.NewRequest("POST", ts.URL+"/api/tools/scan_market", strings.NewReader(`{}`))
	req.Header.Set("Authorization", "Bearer s3cret")
	resp, _ = http.DefaultClient.Do(req)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("with token: %d", resp.StatusCode)
	}
	resp.Body.Close()
}
