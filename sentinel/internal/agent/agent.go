// Package agent is Sentinel's reasoning layer: a Claude analyst that
// investigates with Sentinel's tools and returns a structured assessment.
//
// Determinism first: every number the agent may cite comes from a tool result,
// and the agent cannot change a score, direction, level or size. Its job is to
// reason over the evidence (confluence, model skill, volatility outlook,
// positioning, options sentiment, track record), surface conflicts, and say
// plainly when there is no demonstrated edge. A grounding check flags any
// number in the assessment that does not appear in the tool results.
package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/anthropics/anthropic-sdk-go/v2"
	"github.com/anthropics/anthropic-sdk-go/v2/option"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/tools"
)

// Model is the default analyst model.
const Model = "claude-opus-5-5"

// Assessment is the agent's final, structured output.
type Assessment struct {
	Symbol         string   `json:"symbol"`
	Stance         string   `json:"stance"` // concur, caution, disagree, no_trade
	Headline       string   `json:"headline"`
	Reasoning      string   `json:"reasoning"`
	Evidence       []string `json:"evidence"`
	Risks          []string `json:"risks"`
	Invalidation   []string `json:"what_would_change_my_mind"`
	WatchLevels    []Level  `json:"watch_levels"`
	EdgeAssessment string   `json:"edge_assessment"`
}

// Level is a price the agent wants watched, with why.
type Level struct {
	Price  float64 `json:"price"`
	Reason string  `json:"reason"`
}

// Result is a run's outcome.
type Result struct {
	Assessment   *Assessment `json:"assessment,omitempty"`
	Text         string      `json:"text,omitempty"` // free-form answer when no assessment was submitted
	ToolCalls    []string    `json:"tool_calls"`
	Ungrounded   []float64   `json:"ungrounded_numbers,omitempty"`
	Model        string      `json:"model"`
	InputTokens  int64       `json:"input_tokens"`
	OutputTokens int64       `json:"output_tokens"`
	StopReason   string      `json:"stop_reason"`
}

// Agent drives the tool loop.
type Agent struct {
	Client       anthropic.Client
	Model        string
	Tools        []tools.Tool
	MaxTurns     int
	Effort       anthropic.BetaOutputConfigEffort
	UseFallbacks bool
	Log          func(format string, args ...any)
}

// New creates an agent; the API key comes from the environment (ANTHROPIC_API_KEY
// or an `ant auth login` profile) unless opts override it.
func New(ts []tools.Tool, opts ...option.RequestOption) *Agent {
	return &Agent{Client: anthropic.NewClient(opts...), Model: Model, Tools: ts, MaxTurns: 12,
		Effort: anthropic.BetaOutputConfigEffortHigh, UseFallbacks: true}
}

const systemPrompt = `You are the SURGE Sentinel analyst: a quantitative crypto market analyst working on top of a deterministic indicator engine.

How Sentinel works (so you can reason about its outputs):
- Seven confluence layers (trend, reversal, momentum, timing, microstructure, options sentiment, cross-asset context) vote independently for long and short; each layer is capped so correlated indicators cannot fake confluence. A signal needs a minimum score, enough agreeing layers, a trend or reversal route, limited disagreement and no adverse BTC shock.
- The volatility forecast (HAR-RV) is the engine's most reliably predictive component; its out-of-sample R^2 and band coverage are reported with it.
- The direction model is skill-gated: it abstains (reports the base rate) unless its recent out-of-sample AUC and Brier skill beat chance. Walk-forward research found little stable directional edge in technical features after fees, so an abstaining model is normal and should be said plainly.
- edge_status (proven / unproven / negative) is the realised, fee-adjusted record of fired signals for that symbol and side.
- Costs: OKX US Lv1 fees (0.20% maker entry, 0.35% taker exit) plus slippage are charged in R; cost_r shows how much of a 1R stop they consume. The primary venue is spot-only, so SHORT means exit or avoid unless the user trades elsewhere.

Rules:
- Use the tools to gather evidence before concluding. Start with analyze_symbol; add volatility_forecast, model_report, derivatives_positioning, options_sentiment, scan_market or run_backtest when they would change your view.
- Every number you state must come from a tool result. Never invent prices, probabilities, levels or statistics, and never change the engine's numbers.
- Be candid about uncertainty and about missing edge. If the evidence does not support a trade, say so; "no_trade" is a respectable answer.
- Do not give personalised financial advice or position sizes beyond the engine's own plan fields.
- Finish by calling submit_assessment exactly once. Stances: concur (the engine's signal is well supported), caution (supported but with material conflicts), disagree (the evidence argues against the engine's signal), no_trade (no actionable setup or no demonstrated edge).`

var submitSchema = map[string]any{
	"symbol":   map[string]any{"type": "string"},
	"stance":   map[string]any{"type": "string", "enum": []string{"concur", "caution", "disagree", "no_trade"}},
	"headline": map[string]any{"type": "string", "description": "one sentence"},
	"reasoning": map[string]any{"type": "string",
		"description": "the argument, citing tool outputs; under 250 words"},
	"evidence": map[string]any{"type": "array", "items": map[string]any{"type": "string"},
		"description": "the strongest facts behind the stance"},
	"risks":                     map[string]any{"type": "array", "items": map[string]any{"type": "string"}},
	"what_would_change_my_mind": map[string]any{"type": "array", "items": map[string]any{"type": "string"}},
	"watch_levels": map[string]any{"type": "array", "items": map[string]any{
		"type": "object", "additionalProperties": false, "required": []string{"price", "reason"},
		"properties": map[string]any{"price": map[string]any{"type": "number"}, "reason": map[string]any{"type": "string"}}}},
	"edge_assessment": map[string]any{"type": "string",
		"description": "what the model skill, edge_status and costs say about whether there is a measurable edge"},
}

var submitRequired = []string{"symbol", "stance", "headline", "reasoning", "evidence", "risks", "what_would_change_my_mind", "watch_levels", "edge_assessment"}

func (a *Agent) toolParams() []anthropic.BetaToolUnionParam {
	out := make([]anthropic.BetaToolUnionParam, 0, len(a.Tools)+1)
	for _, t := range a.Tools {
		props, _ := t.Schema["properties"].(map[string]any)
		req, _ := t.Schema["required"].([]string)
		tp := anthropic.BetaToolParam{
			Name:        t.Name,
			Description: anthropic.String(t.Description),
			InputSchema: anthropic.BetaToolInputSchemaParam{Properties: props, Required: req,
				ExtraFields: map[string]any{"additionalProperties": false}},
		}
		out = append(out, anthropic.BetaToolUnionParam{OfTool: &tp})
	}
	sub := anthropic.BetaToolParam{
		Name:        "submit_assessment",
		Description: anthropic.String("Submit the final structured assessment. Call once, after gathering evidence."),
		InputSchema: anthropic.BetaToolInputSchemaParam{Properties: submitSchema, Required: submitRequired,
			ExtraFields: map[string]any{"additionalProperties": false}},
		Strict: anthropic.Bool(true),
	}
	out = append(out, anthropic.BetaToolUnionParam{OfTool: &sub})
	return out
}

// Run answers a question about a symbol (question may be empty for a standard read).
func (a *Agent) Run(ctx context.Context, symbol, timeframe, question string) (*Result, error) {
	if question == "" {
		question = "Give me your read on this market right now and whether Sentinel's signal (if any) is worth acting on."
	}
	user := fmt.Sprintf("Symbol: %s\nTimeframe: %s\nUTC time: %s\n\nQuestion: %s", symbol, defaultTF(timeframe),
		time.Now().UTC().Format(time.RFC3339), question)
	params := anthropic.BetaMessageNewParams{
		Model:     anthropic.Model(a.Model),
		MaxTokens: 16000,
		System: []anthropic.BetaTextBlockParam{{
			Text:         systemPrompt,
			CacheControl: anthropic.NewBetaCacheControlEphemeralParam(),
		}},
		Tools:        a.toolParams(),
		OutputConfig: anthropic.BetaOutputConfigParam{Effort: a.Effort},
		Messages:     []anthropic.BetaMessageParam{anthropic.NewBetaUserMessage(anthropic.NewBetaTextBlock(user))},
	}
	if a.UseFallbacks {
		params.Fallbacks = anthropic.BetaFallbacksParamOfDefault()
		params.Betas = []anthropic.AnthropicBeta{anthropic.AnthropicBetaServerSideFallback2026_07_01}
	}
	res := &Result{}
	var evidence strings.Builder
	for turn := 0; turn < a.MaxTurns; turn++ {
		msg, err := a.Client.Beta.Messages.New(ctx, params)
		if err != nil {
			var apiErr *anthropic.Error
			if errors.As(err, &apiErr) {
				return nil, fmt.Errorf("claude API error %d: %s", apiErr.StatusCode, apiErr.Error())
			}
			return nil, err
		}
		res.Model = string(msg.Model)
		res.InputTokens += msg.Usage.InputTokens
		res.OutputTokens += msg.Usage.OutputTokens
		res.StopReason = string(msg.StopReason)
		params.Messages = append(params.Messages, msg.ToParam())

		switch msg.StopReason {
		case anthropic.BetaStopReasonRefusal:
			return res, errors.New("the model declined this request")
		case anthropic.BetaStopReasonMaxTokens:
			return res, errors.New("the model hit max_tokens before finishing")
		}

		var results []anthropic.BetaContentBlockParamUnion
		var text strings.Builder
		for _, block := range msg.Content {
			switch b := block.AsAny().(type) {
			case anthropic.BetaTextBlock:
				text.WriteString(b.Text)
			case anthropic.BetaToolUseBlock:
				raw := json.RawMessage(b.JSON.Input.Raw())
				if b.Name == "submit_assessment" {
					var as Assessment
					if err := json.Unmarshal(raw, &as); err != nil {
						results = append(results, anthropic.NewBetaToolResultBlock(b.ID, "invalid assessment: "+err.Error(), true))
						continue
					}
					res.Assessment = &as
					res.ToolCalls = append(res.ToolCalls, b.Name)
					res.Ungrounded = ungrounded(as, evidence.String())
					return res, nil
				}
				res.ToolCalls = append(res.ToolCalls, b.Name)
				a.logf("tool %s %s", b.Name, string(raw))
				out, isErr := a.call(ctx, b.Name, raw)
				evidence.WriteString(out)
				evidence.WriteByte('\n')
				results = append(results, anthropic.NewBetaToolResultBlock(b.ID, out, isErr))
			}
		}
		if len(results) == 0 {
			// The model answered in prose without submitting; return its text.
			res.Text = strings.TrimSpace(text.String())
			return res, nil
		}
		params.Messages = append(params.Messages, anthropic.NewBetaUserMessage(results...))
	}
	return res, fmt.Errorf("no assessment after %d turns", a.MaxTurns)
}

func (a *Agent) call(ctx context.Context, name string, raw json.RawMessage) (string, bool) {
	t, ok := tools.Find(a.Tools, name)
	if !ok {
		return "unknown tool " + name, true
	}
	cctx, cancel := context.WithTimeout(ctx, 3*time.Minute)
	defer cancel()
	out, err := t.Run(cctx, raw)
	if err != nil {
		return "error: " + err.Error(), true
	}
	b, err := json.Marshal(out)
	if err != nil {
		return "error: " + err.Error(), true
	}
	const max = 60000
	if len(b) > max {
		return string(b[:max]) + "...(truncated)", false
	}
	return string(b), false
}

func (a *Agent) logf(format string, args ...any) {
	if a.Log != nil {
		a.Log(format, args...)
	}
}

func defaultTF(tf string) string {
	if tf == "" {
		return "service default"
	}
	return tf
}

var numRe = regexp.MustCompile(`-?\d+(?:\.\d+)?`)

// ungrounded returns watch-level prices that do not match any number in the
// tool results within 0.1% (levels are the numbers most likely to be acted on).
func ungrounded(as Assessment, evidence string) []float64 {
	var nums []float64
	for _, m := range numRe.FindAllString(evidence, -1) {
		if v, err := strconv.ParseFloat(m, 64); err == nil && v != 0 {
			nums = append(nums, v)
		}
	}
	var bad []float64
	for _, l := range as.WatchLevels {
		ok := false
		for _, n := range nums {
			if math.Abs(n-l.Price) <= 0.001*math.Abs(l.Price) {
				ok = true
				break
			}
		}
		if !ok {
			bad = append(bad, l.Price)
		}
	}
	return bad
}

// Render formats a result for a terminal.
func (r *Result) Render() string {
	var b strings.Builder
	if r.Assessment == nil {
		b.WriteString(r.Text)
		b.WriteString("\n")
	} else {
		as := r.Assessment
		fmt.Fprintf(&b, "%s  [%s]\n%s\n\n%s\n", as.Symbol, strings.ToUpper(as.Stance), as.Headline, as.Reasoning)
		section := func(title string, items []string) {
			if len(items) == 0 {
				return
			}
			fmt.Fprintf(&b, "\n%s:\n", title)
			for _, it := range items {
				fmt.Fprintf(&b, "  - %s\n", it)
			}
		}
		section("Evidence", as.Evidence)
		section("Risks", as.Risks)
		section("What would change my mind", as.Invalidation)
		if len(as.WatchLevels) > 0 {
			b.WriteString("\nWatch levels:\n")
			for _, l := range as.WatchLevels {
				fmt.Fprintf(&b, "  - %.6g  %s\n", l.Price, l.Reason)
			}
		}
		fmt.Fprintf(&b, "\nEdge: %s\n", as.EdgeAssessment)
		if len(r.Ungrounded) > 0 {
			fmt.Fprintf(&b, "\nWARNING: watch levels not found in any tool output: %v\n", r.Ungrounded)
		}
	}
	fmt.Fprintf(&b, "\n(%s; tools: %s; tokens in/out %d/%d)\n", r.Model, strings.Join(r.ToolCalls, ", "), r.InputTokens, r.OutputTokens)
	return b.String()
}
