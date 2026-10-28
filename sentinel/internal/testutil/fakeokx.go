package testutil

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"sort"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// FakeExchange serves OKX public endpoints (candle history paging, recent
// candles, funding, open interest, long/short, mark and ticker) plus empty
// Deribit responses, backed by the given bars. Unknown instruments return
// OKX error 51001. Requests counts every call.
type FakeExchange struct {
	*httptest.Server
	Requests atomic.Int64
}

// NewFakeExchange starts the server; it is closed when the test ends.
func NewFakeExchange(t testing.TB, bars map[string][]market.Bar) *FakeExchange {
	t.Helper()
	fx := &FakeExchange{}
	f := func(v float64) string { return strconv.FormatFloat(v, 'f', -1, 64) }
	row := func(b market.Bar) string {
		return fmt.Sprintf(`["%d","%s","%s","%s","%s","%s","0","0","1"]`, b.Time.UnixMilli(), f(b.Open), f(b.High), f(b.Low), f(b.Close), f(b.Volume))
	}
	mux := http.NewServeMux()
	candles := func(w http.ResponseWriter, r *http.Request, page int) {
		bs, ok := bars[r.URL.Query().Get("instId")]
		if !ok {
			fmt.Fprint(w, `{"code":"51001","msg":"Instrument ID does not exist","data":[]}`)
			return
		}
		hi := len(bs) - 1
		if a := r.URL.Query().Get("after"); a != "" {
			ts, _ := strconv.ParseInt(a, 10, 64)
			hi = sort.Search(len(bs), func(i int) bool { return bs[i].Time.UnixMilli() >= ts }) - 1
		}
		var rows []string
		for i := hi; i >= 0 && i > hi-page; i-- {
			rows = append(rows, row(bs[i]))
		}
		fmt.Fprintf(w, `{"code":"0","msg":"","data":[%s]}`, strings.Join(rows, ","))
	}
	mux.HandleFunc("/api/v5/market/history-candles", func(w http.ResponseWriter, r *http.Request) { candles(w, r, 100) })
	mux.HandleFunc("/api/v5/market/candles", func(w http.ResponseWriter, r *http.Request) { candles(w, r, 300) })
	mux.HandleFunc("/api/v5/public/funding-rate", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":"0","data":[{"fundingRate":"0.0001"}]}`)
	})
	mux.HandleFunc("/api/v5/public/funding-rate-history", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":"0","data":[{"fundingRate":"0.0001"}]}`)
	})
	mux.HandleFunc("/api/v5/rubik/stat/contracts/open-interest-volume", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":"0","data":[["5","100","1"],["4","100","1"],["3","100","1"],["2","100","1"],["1","100","1"]]}`)
	})
	mux.HandleFunc("/api/v5/rubik/stat/contracts/long-short-account-ratio", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":"0","data":[["5","1.5"]]}`)
	})
	mux.HandleFunc("/api/v5/public/mark-price", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":"0","data":[{"markPx":"100"}]}`)
	})
	mux.HandleFunc("/api/v5/market/ticker", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":"0","data":[{"last":"100"}]}`)
	})
	mux.HandleFunc("/api/v2/public/", func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "get_volatility_index_data") {
			fmt.Fprint(w, `{"result":{"data":[]}}`)
			return
		}
		fmt.Fprint(w, `{"result":[]}`)
	})
	fx.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fx.Requests.Add(1)
		mux.ServeHTTP(w, r)
	}))
	t.Cleanup(fx.Close)
	return fx
}
