// Package feed fetches public market data: OKX spot candles (REST history and
// a live websocket), OKX perpetual-swap positioning, and Deribit options
// sentiment. Nothing here needs credentials, and nothing places orders.
package feed

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// OKX is a public-data REST client.
type OKX struct {
	BaseURL string // default https://www.okx.com
	HTTP    *http.Client
}

// NewOKX returns a client for the global public host. OKX US (us.okx.com)
// serves the same public market endpoints for spot pairs.
func NewOKX() *OKX {
	return &OKX{BaseURL: "https://www.okx.com", HTTP: &http.Client{Timeout: 15 * time.Second}}
}

// barName maps Sentinel timeframes to OKX bar names.
func barName(tf market.Timeframe) (string, error) {
	switch tf.Name {
	case "1m", "5m", "15m", "30m":
		return tf.Name, nil
	case "1h":
		return "1H", nil
	case "2h":
		return "2H", nil
	case "4h":
		return "4H", nil
	case "12h":
		return "12Hutc", nil
	case "1d":
		return "1Dutc", nil
	case "1w":
		return "1Wutc", nil
	}
	return "", fmt.Errorf("okx: unsupported timeframe %s", tf.Name)
}

type okxEnvelope struct {
	Code string          `json:"code"`
	Msg  string          `json:"msg"`
	Data json.RawMessage `json:"data"`
}

func (c *OKX) get(ctx context.Context, path string, q url.Values, out any) error {
	u := strings.TrimRight(c.BaseURL, "/") + path
	if len(q) > 0 {
		u += "?" + q.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", "surge-sentinel/1.0")
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt*attempt) * 500 * time.Millisecond):
			}
		}
		resp, err := c.HTTP.Do(req)
		if err != nil {
			lastErr = err
			continue
		}
		body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
		resp.Body.Close()
		if err != nil {
			lastErr = err
			continue
		}
		if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500 {
			lastErr = fmt.Errorf("okx %s: HTTP %d", path, resp.StatusCode)
			continue
		}
		if resp.StatusCode != http.StatusOK {
			return fmt.Errorf("okx %s: HTTP %d: %.200s", path, resp.StatusCode, body)
		}
		var env okxEnvelope
		if err := json.Unmarshal(body, &env); err != nil {
			return fmt.Errorf("okx %s: %w", path, err)
		}
		if env.Code != "0" {
			if env.Code == "50011" { // rate limit
				lastErr = fmt.Errorf("okx %s: rate limited", path)
				continue
			}
			return fmt.Errorf("okx %s: code %s: %s", path, env.Code, env.Msg)
		}
		return json.Unmarshal(env.Data, out)
	}
	return lastErr
}

func parseCandleRows(rows [][]string, confirmedOnly bool) ([]market.Bar, error) {
	out := make([]market.Bar, 0, len(rows))
	for _, r := range rows {
		if len(r) < 6 {
			return nil, fmt.Errorf("okx candle row too short: %v", r)
		}
		if confirmedOnly && len(r) >= 9 && r[8] != "1" {
			continue
		}
		ts, err := strconv.ParseInt(r[0], 10, 64)
		if err != nil {
			return nil, err
		}
		var f [5]float64
		for i := 0; i < 5; i++ {
			if f[i], err = strconv.ParseFloat(r[i+1], 64); err != nil {
				return nil, err
			}
		}
		out = append(out, market.Bar{Time: time.UnixMilli(ts).UTC(), Open: f[0], High: f[1], Low: f[2], Close: f[3], Volume: f[4]})
	}
	return out, nil
}

// History returns up to n of the most recent *closed* bars, oldest first,
// paging backwards through /market/history-candles.
func (c *OKX) History(ctx context.Context, instID string, tf market.Timeframe, n int) ([]market.Bar, error) {
	bar, err := barName(tf)
	if err != nil {
		return nil, err
	}
	seen := map[int64]market.Bar{}
	after := ""
	for len(seen) < n {
		q := url.Values{"instId": {instID}, "bar": {bar}, "limit": {"100"}}
		if after != "" {
			q.Set("after", after)
		}
		var rows [][]string
		if err := c.get(ctx, "/api/v5/market/history-candles", q, &rows); err != nil {
			return nil, err
		}
		if len(rows) == 0 {
			break
		}
		bars, err := parseCandleRows(rows, true)
		if err != nil {
			return nil, err
		}
		oldest := int64(math.MaxInt64)
		for _, r := range rows {
			if ts, err := strconv.ParseInt(r[0], 10, 64); err == nil && ts < oldest {
				oldest = ts
			}
		}
		for _, b := range bars {
			seen[b.Time.UnixMilli()] = b
		}
		after = strconv.FormatInt(oldest, 10)
		if len(rows) < 100 {
			break
		}
	}
	out := make([]market.Bar, 0, len(seen))
	for _, b := range seen {
		out = append(out, b)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Time.Before(out[j].Time) })
	if len(out) > n {
		out = out[len(out)-n:]
	}
	return out, nil
}

// Recent returns the latest closed bars from /market/candles (up to 300).
func (c *OKX) Recent(ctx context.Context, instID string, tf market.Timeframe, n int) ([]market.Bar, error) {
	bar, err := barName(tf)
	if err != nil {
		return nil, err
	}
	if n > 300 {
		n = 300
	}
	var rows [][]string
	if err := c.get(ctx, "/api/v5/market/candles", url.Values{"instId": {instID}, "bar": {bar}, "limit": {strconv.Itoa(n)}}, &rows); err != nil {
		return nil, err
	}
	bars, err := parseCandleRows(rows, true)
	if err != nil {
		return nil, err
	}
	sort.Slice(bars, func(i, j int) bool { return bars[i].Time.Before(bars[j].Time) })
	return bars, nil
}

// SwapID maps a spot pair (BTC-USDT, BTC-USDC) to the global USDT perpetual.
// Perpetuals are data-only for a US operator; Sentinel never trades them.
func SwapID(spot string) string {
	base := strings.SplitN(spot, "-", 2)[0]
	return base + "-USDT-SWAP"
}

// Derivatives builds a positioning snapshot for a spot pair from the global
// USDT perpetual: funding, open interest change, account long/short ratio and
// basis. window is the OI/price comparison span in hours.
func (c *OKX) Derivatives(ctx context.Context, spot string, window int) (market.Derivatives, error) {
	d := market.Derivatives{Time: time.Now().UTC()}
	swap := SwapID(spot)
	ccy := strings.SplitN(spot, "-", 2)[0]

	var fr []struct {
		FundingRate string `json:"fundingRate"`
	}
	if err := c.get(ctx, "/api/v5/public/funding-rate", url.Values{"instId": {swap}}, &fr); err != nil {
		return d, err
	}
	if len(fr) > 0 {
		d.FundingRate, _ = strconv.ParseFloat(fr[0].FundingRate, 64)
	}
	var hist []struct {
		FundingRate string `json:"fundingRate"`
	}
	if err := c.get(ctx, "/api/v5/public/funding-rate-history", url.Values{"instId": {swap}, "limit": {"21"}}, &hist); err == nil && len(hist) > 0 {
		s := 0.0
		for _, h := range hist {
			v, _ := strconv.ParseFloat(h.FundingRate, 64)
			s += v
		}
		d.FundingAvg = s / float64(len(hist))
	}

	var oi [][]string
	if err := c.get(ctx, "/api/v5/rubik/stat/contracts/open-interest-volume", url.Values{"ccy": {ccy}, "period": {"1H"}}, &oi); err == nil && len(oi) > window {
		now, _ := strconv.ParseFloat(oi[0][1], 64)
		then, _ := strconv.ParseFloat(oi[window][1], 64)
		d.OpenInterest = now
		if then > 0 {
			d.OIChangePct = (now/then - 1) * 100
		}
		// Price change over the same window from hourly candles.
		if bars, err := c.Recent(ctx, spot, market.MustTimeframe("1h"), window+2); err == nil && len(bars) > window {
			last, first := bars[len(bars)-1].Close, bars[len(bars)-1-window].Close
			if first > 0 {
				d.PriceChgPct = (last/first - 1) * 100
			}
		}
	}
	var ls [][]string
	if err := c.get(ctx, "/api/v5/rubik/stat/contracts/long-short-account-ratio", url.Values{"ccy": {ccy}, "period": {"1H"}}, &ls); err == nil && len(ls) > 0 {
		d.LongShort, _ = strconv.ParseFloat(ls[0][1], 64)
	}
	var mark []struct {
		MarkPx string `json:"markPx"`
	}
	var tick []struct {
		Last string `json:"last"`
	}
	if c.get(ctx, "/api/v5/public/mark-price", url.Values{"instType": {"SWAP"}, "instId": {swap}}, &mark) == nil &&
		c.get(ctx, "/api/v5/market/ticker", url.Values{"instId": {spot}}, &tick) == nil && len(mark) > 0 && len(tick) > 0 {
		m, _ := strconv.ParseFloat(mark[0].MarkPx, 64)
		s, _ := strconv.ParseFloat(tick[0].Last, 64)
		if s > 0 {
			d.BasisPct = (m/s - 1) * 100
		}
	}
	d.Available = true
	return d, nil
}
