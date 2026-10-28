package feed

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// Deribit reads public options data. Deribit lists options on BTC and ETH only.
type Deribit struct {
	BaseURL string // default https://www.deribit.com
	HTTP    *http.Client
	Now     func() time.Time
}

func NewDeribit() *Deribit {
	return &Deribit{BaseURL: "https://www.deribit.com", HTTP: &http.Client{Timeout: 15 * time.Second}, Now: time.Now}
}

// Supports reports whether Deribit has options for a spot pair's base asset.
func (d *Deribit) Supports(spot string) (string, bool) {
	base := strings.ToUpper(strings.SplitN(spot, "-", 2)[0])
	return base, base == "BTC" || base == "ETH"
}

func (d *Deribit) get(ctx context.Context, method string, q url.Values, out any) error {
	u := strings.TrimRight(d.BaseURL, "/") + "/api/v2/public/" + method + "?" + q.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", "surge-sentinel/1.0")
	resp, err := d.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("deribit %s: HTTP %d: %.200s", method, resp.StatusCode, body)
	}
	var env struct {
		Result json.RawMessage `json:"result"`
		Error  *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return err
	}
	if env.Error != nil {
		return fmt.Errorf("deribit %s: %s", method, env.Error.Message)
	}
	return json.Unmarshal(env.Result, out)
}

type bookRow struct {
	Instrument      string  `json:"instrument_name"`
	OpenInterest    float64 `json:"open_interest"`
	Volume          float64 `json:"volume"`
	MarkIV          float64 `json:"mark_iv"`
	UnderlyingPrice float64 `json:"underlying_price"`
}

// parseInstrument splits BTC-31OCT26-87000-C into expiry, strike and type.
func parseInstrument(name string) (exp time.Time, strike float64, call bool, ok bool) {
	p := strings.Split(name, "-")
	if len(p) != 4 {
		return
	}
	t, err := time.Parse("2Jan06", p[1])
	if err != nil {
		return
	}
	k, err := strconv.ParseFloat(strings.ReplaceAll(p[2], "d", "."), 64)
	if err != nil {
		return
	}
	return t.Add(8 * time.Hour), k, p[3] == "C", true // Deribit expiries settle 08:00 UTC
}

// Options computes put/call ratios (open interest and 24h volume), DVOL and its
// 24h change, and a 25-delta-style skew proxy: the implied volatility of the
// call ~10% out of the money minus the put ~10% out of the money, at the expiry
// closest to 30 days.
func (d *Deribit) Options(ctx context.Context, currency string) (market.Options, error) {
	o := market.Options{Time: d.Now().UTC()}
	var rows []bookRow
	if err := d.get(ctx, "get_book_summary_by_currency", url.Values{"currency": {currency}, "kind": {"option"}}, &rows); err != nil {
		return o, err
	}
	var putOI, callOI, putVol, callVol float64
	type leg struct {
		strike, iv float64
	}
	byExp := map[time.Time]struct{ calls, puts []leg }{}
	spot := 0.0
	for _, r := range rows {
		exp, k, call, ok := parseInstrument(r.Instrument)
		if !ok {
			continue
		}
		if r.UnderlyingPrice > 0 {
			spot = r.UnderlyingPrice
		}
		e := byExp[exp]
		if call {
			callOI += r.OpenInterest
			callVol += r.Volume
			e.calls = append(e.calls, leg{k, r.MarkIV})
		} else {
			putOI += r.OpenInterest
			putVol += r.Volume
			e.puts = append(e.puts, leg{k, r.MarkIV})
		}
		byExp[exp] = e
	}
	if callOI > 0 {
		o.PCRByOI = putOI / callOI
	}
	if callVol > 0 {
		o.PCRByVol = putVol / callVol
	}
	// Skew at the expiry nearest 30 days.
	if spot > 0 {
		target := d.Now().Add(30 * 24 * time.Hour)
		var best time.Time
		bestGap := math.MaxFloat64
		for exp, e := range byExp {
			if exp.Before(d.Now().Add(7*24*time.Hour)) || len(e.calls) == 0 || len(e.puts) == 0 {
				continue
			}
			if g := math.Abs(exp.Sub(target).Hours()); g < bestGap {
				best, bestGap = exp, g
			}
		}
		if !best.IsZero() {
			e := byExp[best]
			nearest := func(legs []leg, k float64) float64 {
				iv, gap := 0.0, math.MaxFloat64
				for _, l := range legs {
					if g := math.Abs(l.strike - k); g < gap && l.iv > 0 {
						iv, gap = l.iv, g
					}
				}
				return iv
			}
			c, p := nearest(e.calls, spot*1.1), nearest(e.puts, spot*0.9)
			if c > 0 && p > 0 {
				o.Skew25 = c - p
			}
		}
	}
	// DVOL now and 24h ago (hourly resolution).
	now := d.Now().UTC()
	var vol struct {
		Data [][]float64 `json:"data"`
	}
	q := url.Values{
		"currency":        {currency},
		"resolution":      {"3600"},
		"start_timestamp": {strconv.FormatInt(now.Add(-26*time.Hour).UnixMilli(), 10)},
		"end_timestamp":   {strconv.FormatInt(now.UnixMilli(), 10)},
	}
	if err := d.get(ctx, "get_volatility_index_data", q, &vol); err == nil && len(vol.Data) >= 2 {
		last := vol.Data[len(vol.Data)-1]
		o.DVOL = last[4]
		// Find the candle ~24h before the last.
		for i := len(vol.Data) - 1; i >= 0; i-- {
			if last[0]-vol.Data[i][0] >= 24*3600*1000 {
				if then := vol.Data[i][4]; then > 0 {
					o.DVOLChange = (o.DVOL/then - 1) * 100
				}
				break
			}
		}
	}
	o.Available = callOI > 0
	return o, nil
}
