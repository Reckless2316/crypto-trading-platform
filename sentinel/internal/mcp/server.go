// Package mcp serves Sentinel's tools over the Model Context Protocol (JSON-RPC
// 2.0, newline-delimited on stdio), so Claude Desktop, Claude Code, Hermes or
// any MCP client can call the engine directly. It replaces the role of the
// JavaScript crypto-indicators-mcp server with Sentinel's verified engine.
package mcp

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"sync"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/tools"
)

// ProtocolVersion is the newest MCP revision this server implements.
const ProtocolVersion = "2025-06-18"

var supported = map[string]bool{"2024-11-05": true, "2025-03-26": true, "2025-06-18": true}

type request struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type response struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

// Server dispatches MCP requests to tools.
type Server struct {
	Name, Version string
	Tools         []tools.Tool
	mu            sync.Mutex // serialises writes
}

// Serve reads requests from r and writes responses to w until EOF or ctx ends.
// Tool calls run concurrently; responses may arrive out of order, as JSON-RPC allows.
func (s *Server) Serve(ctx context.Context, r io.Reader, w io.Writer) error {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 1<<20), 32<<20)
	var wg sync.WaitGroup
	defer wg.Wait()
	for sc.Scan() {
		line := append([]byte(nil), sc.Bytes()...)
		if len(line) == 0 {
			continue
		}
		var req request
		if err := json.Unmarshal(line, &req); err != nil {
			s.write(w, response{JSONRPC: "2.0", ID: json.RawMessage("null"), Error: &rpcError{-32700, "parse error"}})
			continue
		}
		if req.Method == "tools/call" {
			wg.Add(1)
			go func() {
				defer wg.Done()
				s.reply(ctx, w, req)
			}()
			continue
		}
		s.reply(ctx, w, req)
		if ctx.Err() != nil {
			return ctx.Err()
		}
	}
	return sc.Err()
}

func (s *Server) reply(ctx context.Context, w io.Writer, req request) {
	res, rerr := s.handle(ctx, req)
	if len(req.ID) == 0 { // notification: no response
		return
	}
	out := response{JSONRPC: "2.0", ID: req.ID}
	if rerr != nil {
		out.Error = rerr
	} else {
		out.Result = res
	}
	s.write(w, out)
}

func (s *Server) write(w io.Writer, v any) {
	b, err := json.Marshal(v)
	if err != nil {
		b, _ = json.Marshal(response{JSONRPC: "2.0", ID: json.RawMessage("null"), Error: &rpcError{-32603, err.Error()}})
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	w.Write(append(b, '\n'))
}

func (s *Server) handle(ctx context.Context, req request) (any, *rpcError) {
	switch req.Method {
	case "initialize":
		var p struct {
			ProtocolVersion string `json:"protocolVersion"`
		}
		_ = json.Unmarshal(req.Params, &p)
		v := ProtocolVersion
		if supported[p.ProtocolVersion] {
			v = p.ProtocolVersion
		}
		return map[string]any{
			"protocolVersion": v,
			"capabilities":    map[string]any{"tools": map[string]any{"listChanged": false}},
			"serverInfo":      map[string]any{"name": s.Name, "version": s.Version},
			"instructions": "SURGE Sentinel: real-time crypto indicators with calibrated volatility forecasts and a skill-gated " +
				"direction model. Start with analyze_symbol or scan_market. Every number is computed deterministically; " +
				"edge_status and model_report say whether a signal type has demonstrated out-of-sample edge after fees. " +
				"Market data is public OKX/Deribit data; no orders can be placed through this server.",
		}, nil
	case "notifications/initialized", "notifications/cancelled":
		return nil, nil
	case "ping":
		return map[string]any{}, nil
	case "tools/list":
		list := make([]map[string]any, 0, len(s.Tools))
		for _, t := range s.Tools {
			list = append(list, map[string]any{"name": t.Name, "description": t.Description, "inputSchema": t.Schema,
				"annotations": map[string]any{"readOnlyHint": true, "openWorldHint": true}})
		}
		return map[string]any{"tools": list}, nil
	case "tools/call":
		var p struct {
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			return nil, &rpcError{-32602, "invalid params"}
		}
		t, ok := tools.Find(s.Tools, p.Name)
		if !ok {
			return nil, &rpcError{-32602, fmt.Sprintf("unknown tool %q", p.Name)}
		}
		out, err := t.Run(ctx, p.Arguments)
		if err != nil {
			// Tool failures are results with isError so the model can recover.
			return map[string]any{"content": []map[string]any{{"type": "text", "text": "error: " + err.Error()}}, "isError": true}, nil
		}
		b, err := json.Marshal(out)
		if err != nil {
			return nil, &rpcError{-32603, err.Error()}
		}
		res := map[string]any{"content": []map[string]any{{"type": "text", "text": string(b)}}, "isError": false}
		var structured map[string]any
		if json.Unmarshal(b, &structured) == nil {
			res["structuredContent"] = structured
		}
		return res, nil
	}
	return nil, &rpcError{-32601, "method not found: " + req.Method}
}
