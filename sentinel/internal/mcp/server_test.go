package mcp

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/tools"
)

func TestMCPSession(t *testing.T) {
	echo := tools.Tool{
		Name: "echo", Description: "echo input",
		Schema: map[string]any{"type": "object", "properties": map[string]any{"x": map[string]any{"type": "number"}}},
		Run: func(ctx context.Context, in json.RawMessage) (any, error) {
			var v map[string]any
			_ = json.Unmarshal(in, &v)
			if v["x"] == nil {
				return nil, errors.New("x missing")
			}
			return v, nil
		},
	}
	s := &Server{Name: "t", Version: "1", Tools: []tools.Tool{echo}}
	in := strings.Join([]string{
		`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}`,
		`{"jsonrpc":"2.0","method":"notifications/initialized"}`,
		`{"jsonrpc":"2.0","id":2,"method":"tools/list"}`,
		`{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"echo","arguments":{"x":4}}}`,
		`{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"echo","arguments":{}}}`,
		`{"jsonrpc":"2.0","id":5,"method":"nope"}`,
		`not json`,
	}, "\n")
	var out strings.Builder
	if err := s.Serve(context.Background(), strings.NewReader(in), &out); err != nil {
		t.Fatal(err)
	}
	got := map[string]map[string]any{}
	sc := bufio.NewScanner(strings.NewReader(out.String()))
	for sc.Scan() {
		var m map[string]any
		if err := json.Unmarshal(sc.Bytes(), &m); err != nil {
			t.Fatal(err)
		}
		got[string(mustJSON(m["id"]))] = m
	}
	if len(got) != 6 { // 5 responses + the parse error (id null); the notification gets none
		t.Fatalf("responses: %v", out.String())
	}
	init := got["1"]["result"].(map[string]any)
	if init["protocolVersion"] != "2025-03-26" {
		t.Fatalf("negotiated %v", init["protocolVersion"])
	}
	list := got["2"]["result"].(map[string]any)["tools"].([]any)
	if list[0].(map[string]any)["name"] != "echo" {
		t.Fatal("tools/list")
	}
	ok := got["3"]["result"].(map[string]any)
	if ok["isError"] != false || ok["structuredContent"].(map[string]any)["x"].(float64) != 4 {
		t.Fatalf("call: %v", ok)
	}
	if got["4"]["result"].(map[string]any)["isError"] != true {
		t.Fatal("tool errors must be isError results")
	}
	if got["5"]["error"].(map[string]any)["code"].(float64) != -32601 {
		t.Fatal("unknown method")
	}
	if got["null"]["error"].(map[string]any)["code"].(float64) != -32700 {
		t.Fatal("parse error")
	}
}

func mustJSON(v any) []byte { b, _ := json.Marshal(v); return b }
