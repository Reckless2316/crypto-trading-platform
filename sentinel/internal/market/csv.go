package market

import (
	"encoding/csv"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"time"
)

// ReadCSV parses bars from CSV with the header ts,open,high,low,close,volume.
// ts is Unix milliseconds, Unix seconds, or RFC 3339.
func ReadCSV(r io.Reader) ([]Bar, error) {
	cr := csv.NewReader(r)
	cr.FieldsPerRecord = -1
	rows, err := cr.ReadAll()
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, fmt.Errorf("empty csv")
	}
	idx := map[string]int{}
	for i, h := range rows[0] {
		idx[strings.ToLower(strings.TrimSpace(h))] = i
	}
	need := []string{"ts", "open", "high", "low", "close", "volume"}
	for _, k := range need {
		if _, ok := idx[k]; !ok {
			if k == "ts" {
				if j, ok2 := idx["time"]; ok2 {
					idx["ts"] = j
					continue
				}
			}
			return nil, fmt.Errorf("csv missing column %q", k)
		}
	}
	bars := make([]Bar, 0, len(rows)-1)
	for ln, row := range rows[1:] {
		get := func(k string) (float64, error) {
			j := idx[k]
			if j >= len(row) {
				return 0, fmt.Errorf("line %d: short row", ln+2)
			}
			return strconv.ParseFloat(strings.TrimSpace(row[j]), 64)
		}
		t, err := parseTime(row[idx["ts"]])
		if err != nil {
			return nil, fmt.Errorf("line %d: %w", ln+2, err)
		}
		var b Bar
		b.Time = t
		if b.Open, err = get("open"); err != nil {
			return nil, err
		}
		if b.High, err = get("high"); err != nil {
			return nil, err
		}
		if b.Low, err = get("low"); err != nil {
			return nil, err
		}
		if b.Close, err = get("close"); err != nil {
			return nil, err
		}
		if b.Volume, err = get("volume"); err != nil {
			return nil, err
		}
		bars = append(bars, b)
	}
	return bars, nil
}

// LoadCSV reads bars from a file.
func LoadCSV(path string) ([]Bar, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return ReadCSV(f)
}

// WriteCSV writes bars in the format ReadCSV accepts.
func WriteCSV(w io.Writer, bars []Bar) error {
	cw := csv.NewWriter(w)
	if err := cw.Write([]string{"ts", "open", "high", "low", "close", "volume"}); err != nil {
		return err
	}
	f := func(v float64) string { return strconv.FormatFloat(v, 'f', -1, 64) }
	for _, b := range bars {
		if err := cw.Write([]string{strconv.FormatInt(b.Time.UnixMilli(), 10), f(b.Open), f(b.High), f(b.Low), f(b.Close), f(b.Volume)}); err != nil {
			return err
		}
	}
	cw.Flush()
	return cw.Error()
}

func parseTime(s string) (time.Time, error) {
	s = strings.TrimSpace(s)
	if n, err := strconv.ParseInt(s, 10, 64); err == nil {
		if n > 1e11 {
			return time.UnixMilli(n).UTC(), nil
		}
		return time.Unix(n, 0).UTC(), nil
	}
	return time.Parse(time.RFC3339, s)
}
