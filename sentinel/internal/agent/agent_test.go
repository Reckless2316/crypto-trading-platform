package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/anthropics/anthropic-sdk-go/v2/option"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/tools"
)

func fakeClaude(t *testing.T, responses []string, check func(n int, body map[string]any, h http.Header)) *httptest.Server {
	t.Helper()
	var n atomic.Int32
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/messages" {
			t.Errorf("path %s", r.URL.Path)
		}
		b, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(b, &body)
		i := int(n.Add(1)) - 1
		if check != nil {
			check(i, body, r.Header)
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, responses[min(i, len(responses)-1)])
	}))
}

func msg(stop string, content string) string {
	return fmt.Sprintf(`{"id":"msg_x","type":"message","role":"assistant","model":"claude-opus-5-5","content":%s,"stop_reason":%q,"stop_sequence":null,"usage":{"input_tokens":100,"output_tokens":20}}`, content, stop)
}

var priceTool = tools.Tool{
	Name: "analyze_symbol", Description: "fake analysis",
	Schema: map[string]any{"type": "object", "properties": map[string]any{"symbol": map[string]any{"type": "string"}}, "required": []string{"symbol"}},
	Run: func(ctx context.Context, in json.RawMessage) (any, error) {
		return map[string]any{"price": 81234.5, "support": 80000.0, "signal": "NEUTRAL"}, nil
	},
}

func TestAgentLoop(t *testing.T) {
	assessment := `{"symbol":"BTC-USDT","stance":"no_trade","headline":"No edge.","reasoning":"Signal is neutral.","evidence":["neutral"],"risks":["chop"],"what_would_change_my_mind":["breakout"],"watch_levels":[{"price":80000,"reason":"support"},{"price":12345,"reason":"made up"}],"edge_assessment":"none demonstrated"}`
	srv := fakeClaude(t, []string{
		msg("tool_use", `[{"type":"text","text":"Checking."},{"type":"tool_use","id":"tu_1","name":"analyze_symbol","input":{"symbol":"BTC-USDT"}}]`),
		msg("tool_use", `[{"type":"tool_use","id":"tu_2","name":"submit_assessment","input":`+assessment+`}]`),
	}, func(n int, body map[string]any, h http.Header) {
		if body["model"] != Model {
			t.Errorf("model %v", body["model"])
		}
		if body["fallbacks"] != "default" || !strings.Contains(h.Get("anthropic-beta"), "server-side-fallback-2026-07-01") {
			t.Errorf("fallbacks %v beta %q", body["fallbacks"], h.Get("anthropic-beta"))
		}
		if oc, _ := body["output_config"].(map[string]any); oc["effort"] != "high" {
			t.Errorf("output_config %v", body["output_config"])
		}
		if _, ok := body["thinking"]; ok {
			t.Error("thinking must be omitted for Opus 5.5")
		}
		ts := body["tools"].([]any)
		last := ts[len(ts)-1].(map[string]any)
		if last["name"] != "submit_assessment" || last["strict"] != true {
			t.Errorf("submit tool %v", last)
		}
		msgs := body["messages"].([]any)
		if n == 1 {
			// Second request carries the assistant turn and the tool result.
			res := msgs[2].(map[string]any)["content"].([]any)[0].(map[string]any)
			if res["type"] != "tool_result" || res["tool_use_id"] != "tu_1" || !strings.Contains(fmt.Sprint(res["content"]), "81234.5") {
				t.Errorf("tool result %v", res)
			}
		}
	})
	defer srv.Close()
	a := New([]tools.Tool{priceTool}, option.WithBaseURL(srv.URL), option.WithAPIKey("test"))
	res, err := a.Run(context.Background(), "BTC-USDT", "1h", "")
	if err != nil {
		t.Fatal(err)
	}
	if res.Assessment == nil || res.Assessment.Stance != "no_trade" {
		t.Fatalf("result %+v", res)
	}
	if len(res.Ungrounded) != 1 || res.Ungrounded[0] != 12345 {
		t.Fatalf("grounding check: %v", res.Ungrounded)
	}
	if strings.Join(res.ToolCalls, ",") != "analyze_symbol,submit_assessment" || res.InputTokens != 200 {
		t.Fatalf("calls %v tokens %d", res.ToolCalls, res.InputTokens)
	}
	if !strings.Contains(res.Render(), "WARNING") {
		t.Fatal("render must flag ungrounded levels")
	}
}

func TestAgentRefusalAndProse(t *testing.T) {
	srv := fakeClaude(t, []string{msg("refusal", `[]`)}, nil)
	defer srv.Close()
	a := New(nil, option.WithBaseURL(srv.URL), option.WithAPIKey("test"))
	a.UseFallbacks = false
	if _, err := a.Run(context.Background(), "BTC-USDT", "", ""); err == nil {
		t.Fatal("refusal must be an error")
	}
	srv2 := fakeClaude(t, []string{msg("end_turn", `[{"type":"text","text":"Plain answer."}]`)}, nil)
	defer srv2.Close()
	a2 := New(nil, option.WithBaseURL(srv2.URL), option.WithAPIKey("test"))
	res, err := a2.Run(context.Background(), "BTC-USDT", "", "q")
	if err != nil || res.Text != "Plain answer." {
		t.Fatalf("%+v %v", res, err)
	}
}
