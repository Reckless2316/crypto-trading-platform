package feed

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/coder/websocket"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// BarEvent is a candle update from the live stream. Closed is true exactly
// once per bar, when the exchange confirms it; only closed bars reach the
// engine, so Sentinel signals never repaint.
type BarEvent struct {
	Symbol string
	Bar    market.Bar
	Closed bool
}

// OKXBusinessWS is the OKX public websocket endpoint that carries candle
// channels. OKX documents port 8443; the same endpoint also answers on 443,
// which passes through more corporate firewalls and proxies.
const OKXBusinessWS = "wss://ws.okx.com:443/ws/v5/business"

func channelName(tf market.Timeframe) (string, error) {
	b, err := barName(tf)
	if err != nil {
		return "", err
	}
	return "candle" + b, nil
}

// Stream subscribes to OKX candle channels and delivers events until ctx ends,
// reconnecting with backoff. If the websocket cannot be used it falls back to
// REST polling, so the live pipeline keeps working behind restrictive proxies.
func Stream(ctx context.Context, wsURL string, rest *OKX, symbols []string, tf market.Timeframe, out chan<- BarEvent, log *slog.Logger) error {
	ch, err := channelName(tf)
	if err != nil {
		return err
	}
	if wsURL == "" {
		wsURL = OKXBusinessWS
	}
	backoff := time.Second
	failures := 0
	for ctx.Err() == nil {
		err := streamOnce(ctx, wsURL, ch, symbols, out, log)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		failures++
		log.Warn("okx websocket disconnected", "err", err, "retry_in", backoff)
		if failures >= 3 && rest != nil {
			log.Warn("falling back to REST polling for live candles")
			return Poll(ctx, rest, symbols, tf, out, log)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}
		backoff = min(backoff*2, 30*time.Second)
	}
	return ctx.Err()
}

func streamOnce(ctx context.Context, wsURL, channel string, symbols []string, out chan<- BarEvent, log *slog.Logger) error {
	dctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	conn, _, err := websocket.Dial(dctx, wsURL, nil)
	cancel()
	if err != nil {
		return err
	}
	defer conn.CloseNow()
	conn.SetReadLimit(1 << 20)

	args := make([]map[string]string, 0, len(symbols))
	for _, s := range symbols {
		args = append(args, map[string]string{"channel": channel, "instId": s})
	}
	sub, _ := json.Marshal(map[string]any{"op": "subscribe", "args": args})
	if err := conn.Write(ctx, websocket.MessageText, sub); err != nil {
		return err
	}
	// OKX drops idle connections after 30s: ping every 20s.
	pctx, pcancel := context.WithCancel(ctx)
	defer pcancel()
	go func() {
		t := time.NewTicker(20 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-pctx.Done():
				return
			case <-t.C:
				_ = conn.Write(pctx, websocket.MessageText, []byte("ping"))
			}
		}
	}()
	subscribed := false
	for {
		rctx, rcancel := context.WithTimeout(ctx, 60*time.Second)
		_, msg, err := conn.Read(rctx)
		rcancel()
		if err != nil {
			return err
		}
		if string(msg) == "pong" {
			continue
		}
		evs, err := parseWSMessage(msg)
		if err != nil {
			return err
		}
		if !subscribed && len(evs) > 0 {
			subscribed = true
			log.Info("okx websocket streaming", "url", wsURL, "channel", channel, "symbols", len(symbols))
		}
		for _, ev := range evs {
			select {
			case out <- ev:
			case <-ctx.Done():
				return ctx.Err()
			}
		}
	}
}

// parseWSMessage decodes one OKX candle push (or ignores acks).
func parseWSMessage(msg []byte) ([]BarEvent, error) {
	var m struct {
		Event string `json:"event"`
		Msg   string `json:"msg"`
		Arg   struct {
			Channel string `json:"channel"`
			InstID  string `json:"instId"`
		} `json:"arg"`
		Data [][]string `json:"data"`
	}
	if err := json.Unmarshal(msg, &m); err != nil {
		return nil, fmt.Errorf("okx ws: %w", err)
	}
	if m.Event == "error" {
		return nil, fmt.Errorf("okx ws error: %s", m.Msg)
	}
	if m.Event != "" || !strings.HasPrefix(m.Arg.Channel, "candle") {
		return nil, nil
	}
	var out []BarEvent
	for _, row := range m.Data {
		bars, err := parseCandleRows([][]string{row}, false)
		if err != nil || len(bars) == 0 {
			continue
		}
		closed := len(row) >= 9 && row[8] == "1"
		out = append(out, BarEvent{Symbol: m.Arg.InstID, Bar: bars[0], Closed: closed})
	}
	return out, nil
}

// Poll emits newly closed bars by polling REST every few seconds near each bar
// boundary. It is the fallback when websockets are unavailable.
func Poll(ctx context.Context, rest *OKX, symbols []string, tf market.Timeframe, out chan<- BarEvent, log *slog.Logger) error {
	last := map[string]time.Time{}
	interval := 10 * time.Second
	if tf.Dur <= time.Minute {
		interval = 3 * time.Second
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		for _, s := range symbols {
			bars, err := rest.Recent(ctx, s, tf, 3)
			if err != nil {
				log.Warn("poll failed", "symbol", s, "err", err)
				continue
			}
			for _, b := range bars {
				if b.Time.After(last[s]) {
					if !last[s].IsZero() {
						select {
						case out <- BarEvent{Symbol: s, Bar: b, Closed: true}:
						case <-ctx.Done():
							return ctx.Err()
						}
					}
					last[s] = b.Time
				}
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
		}
	}
}
