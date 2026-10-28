package feed

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// fakeOKX serves 250 hourly candles newest-first in pages of 100, the last one unconfirmed.
func fakeOKX(t *testing.T) *httptest.Server {
	t.Helper()
	const n = 250
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC).UnixMilli()
	row := func(i int) []string {
		ts := base + int64(i)*3600_000
		c := 100 + float64(i)
		confirm := "1"
		if i == n-1 {
			confirm = "0"
		}
		f := func(v float64) string { return strconv.FormatFloat(v, 'f', -1, 64) }
		return []string{strconv.FormatInt(ts, 10), f(c - 0.5), f(c + 1), f(c - 1), f(c), "10", "1000", "1000", confirm}
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v5/market/history-candles", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("bar") != "1H" {
			t.Errorf("bar = %s", r.URL.Query().Get("bar"))
		}
		hi := n - 1
		if a := r.URL.Query().Get("after"); a != "" {
			ts, _ := strconv.ParseInt(a, 10, 64)
			hi = int((ts-base)/3600_000) - 1
		}
		var rows []string
		for i := hi; i >= 0 && i > hi-100; i-- {
			rows = append(rows, `["`+strings.Join(row(i), `","`)+`"]`)
		}
		fmt.Fprintf(w, `{"code":"0","msg":"","data":[%s]}`, strings.Join(rows, ","))
	})
	mux.HandleFunc("/api/v5/market/candles", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `{"code":"0","msg":"","data":[["%s"],["%s"]]}`, strings.Join(row(n-1), `","`), strings.Join(row(n-2), `","`))
	})
	mux.HandleFunc("/api/v5/public/funding-rate", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("instId") != "BTC-USDT-SWAP" {
			t.Errorf("instId %s", r.URL.Query().Get("instId"))
		}
		fmt.Fprint(w, `{"code":"0","data":[{"fundingRate":"0.0002"}]}`)
	})
	mux.HandleFunc("/api/v5/public/funding-rate-history", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":"0","data":[{"fundingRate":"0.0001"},{"fundingRate":"0.0003"}]}`)
	})
	mux.HandleFunc("/api/v5/rubik/stat/contracts/open-interest-volume", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":"0","data":[["5","110","1"],["4","108","1"],["3","105","1"],["2","104","1"],["1","100","1"]]}`)
	})
	mux.HandleFunc("/api/v5/rubik/stat/contracts/long-short-account-ratio", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":"0","data":[["5","2.5"]]}`)
	})
	mux.HandleFunc("/api/v5/public/mark-price", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":"0","data":[{"markPx":"101"}]}`)
	})
	mux.HandleFunc("/api/v5/market/ticker", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":"0","data":[{"last":"100"}]}`)
	})
	return httptest.NewServer(mux)
}

func TestHistoryPagesAndDropsUnconfirmed(t *testing.T) {
	srv := fakeOKX(t)
	defer srv.Close()
	c := NewOKX()
	c.BaseURL = srv.URL
	bars, err := c.History(context.Background(), "BTC-USDT", market.MustTimeframe("1h"), 1000)
	if err != nil {
		t.Fatal(err)
	}
	if len(bars) != 249 {
		t.Fatalf("got %d bars, want 249 confirmed", len(bars))
	}
	for i := 1; i < len(bars); i++ {
		if !bars[i].Time.After(bars[i-1].Time) {
			t.Fatal("bars not ascending")
		}
	}
	short, err := c.History(context.Background(), "BTC-USDT", market.MustTimeframe("1h"), 120)
	if err != nil || len(short) != 120 || short[119].Close != bars[248].Close {
		t.Fatalf("limit: %d bars, err %v", len(short), err)
	}
}

func TestDerivatives(t *testing.T) {
	srv := fakeOKX(t)
	defer srv.Close()
	c := NewOKX()
	c.BaseURL = srv.URL
	d, err := c.Derivatives(context.Background(), "BTC-USDT", 4)
	if err != nil {
		t.Fatal(err)
	}
	if !d.Available || d.FundingRate != 0.0002 || d.FundingAvg < 0.000199 || d.FundingAvg > 0.000201 || d.OpenInterest != 110 || d.LongShort != 2.5 {
		t.Fatalf("%+v", d)
	}
	if d.OIChangePct < 9.99 || d.OIChangePct > 10.01 || d.BasisPct < 0.99 || d.BasisPct > 1.01 {
		t.Fatalf("oi %v basis %v", d.OIChangePct, d.BasisPct)
	}
}

func TestOKXErrorCode(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":"51001","msg":"Instrument ID does not exist","data":[]}`)
	}))
	defer srv.Close()
	c := NewOKX()
	c.BaseURL = srv.URL
	if _, err := c.Recent(context.Background(), "NOPE-USDT", market.MustTimeframe("1h"), 5); err == nil || !strings.Contains(err.Error(), "51001") {
		t.Fatalf("err = %v", err)
	}
}

func TestDeribitOptions(t *testing.T) {
	now := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	exp := now.Add(30 * 24 * time.Hour).Format("2Jan06")
	near := now.Add(2 * 24 * time.Hour).Format("2Jan06")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "get_book_summary_by_currency"):
			fmt.Fprintf(w, `{"result":[
				{"instrument_name":"BTC-%[1]s-110000-C","open_interest":100,"volume":10,"mark_iv":45,"underlying_price":100000},
				{"instrument_name":"BTC-%[1]s-90000-P","open_interest":50,"volume":20,"mark_iv":50,"underlying_price":100000},
				{"instrument_name":"BTC-%[2]s-110000-C","open_interest":10,"volume":0,"mark_iv":99,"underlying_price":100000},
				{"instrument_name":"BTC-PERPETUAL","open_interest":1}]}`, exp, near)
		case strings.HasSuffix(r.URL.Path, "get_volatility_index_data"):
			t0 := now.Add(-25 * time.Hour).UnixMilli()
			fmt.Fprintf(w, `{"result":{"data":[[%d,40,40,40,40],[%d,50,50,50,50]]}}`, t0, t0+25*3600_000)
		}
	}))
	defer srv.Close()
	d := NewDeribit()
	d.BaseURL = srv.URL
	d.Now = func() time.Time { return now }
	o, err := d.Options(context.Background(), "BTC")
	if err != nil {
		t.Fatal(err)
	}
	if !o.Available || o.PCRByOI != 50.0/110 || o.PCRByVol != 2 || o.Skew25 != -5 || o.DVOL != 50 || o.DVOLChange != 25 {
		t.Fatalf("%+v", o)
	}
	if _, ok := d.Supports("SOL-USDT"); ok {
		t.Fatal("SOL has no Deribit options")
	}
}

func TestParseWSMessage(t *testing.T) {
	evs, err := parseWSMessage([]byte(`{"arg":{"channel":"candle1H","instId":"BTC-USDT"},"data":[["1767225600000","1","2","0.5","1.5","10","0","0","1"]]}`))
	if err != nil || len(evs) != 1 || !evs[0].Closed || evs[0].Symbol != "BTC-USDT" || evs[0].Bar.Close != 1.5 {
		t.Fatalf("%+v %v", evs, err)
	}
	if evs, _ := parseWSMessage([]byte(`{"event":"subscribe","arg":{"channel":"candle1H"}}`)); len(evs) != 0 {
		t.Fatal("ack produced events")
	}
	if _, err := parseWSMessage([]byte(`{"event":"error","msg":"bad"}`)); err == nil {
		t.Fatal("error event not surfaced")
	}
	if SwapID("ETH-USDC") != "ETH-USDT-SWAP" {
		t.Fatal(SwapID("ETH-USDC"))
	}
}
