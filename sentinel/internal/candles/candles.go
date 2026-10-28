// Package candles recognises the 33 candlestick patterns of the MotiveWave
// Candlestick Pattern Study (single, double and triple bar) using TA-Lib-style
// body/shadow definitions, with every size threshold normalised by ATR rather
// than by a fixed percentage so the rules hold across coins and timeframes.
//
// Priority follows the MotiveWave study: triple-bar patterns win over
// double-bar, which win over single-bar. Reversal patterns require the opposing
// trend context (a hammer is only bullish after a decline), matching the
// study's optional trend filter, which Sentinel always enables.
package candles

import (
	"math"

	"github.com/Reckless2316/crypto-trading-platform/sentinel/internal/market"
)

// Bias of a pattern.
const (
	Bearish = -1
	Neutral = 0
	Bullish = 1
)

// Pattern is a detected candlestick pattern completing on the newest bar.
type Pattern struct {
	Name     string  `json:"name"`
	Bias     int     `json:"bias"`
	Bars     int     `json:"bars"`
	Strength float64 `json:"strength"` // 0..1, larger for more decisive formations
}

type candle struct {
	o, h, l, c       float64
	body, rng        float64
	upper, lower     float64
	bull, bear       bool
	bodyTop, bodyBot float64
	mid              float64
}

func mk(b market.Bar) candle {
	k := candle{o: b.Open, h: b.High, l: b.Low, c: b.Close}
	k.body = math.Abs(b.Close - b.Open)
	k.rng = b.High - b.Low
	k.bodyTop = math.Max(b.Open, b.Close)
	k.bodyBot = math.Min(b.Open, b.Close)
	k.upper = b.High - k.bodyTop
	k.lower = k.bodyBot - b.Low
	k.bull = b.Close > b.Open
	k.bear = b.Close < b.Open
	k.mid = (b.Open + b.Close) / 2
	return k
}

// thresholds are expressed in ATR units.
type th struct {
	atr float64
}

func (t th) long(k candle) bool     { return k.body >= 0.9*t.atr }
func (t th) small(k candle) bool    { return k.body <= 0.35*t.atr }
func (t th) doji(k candle) bool     { return k.rng > 0 && k.body <= 0.1*k.rng && k.body <= 0.12*t.atr }
func (t th) tiny(x float64) bool    { return x <= 0.08*t.atr }
func (t th) near(a, b float64) bool { return math.Abs(a-b) <= 0.06*t.atr }

// Detect returns the highest-priority pattern completing on the last bar of
// window (oldest first, at least 1 bar; 3 needed for triple patterns). atr is
// the current ATR and trend is the prevailing direction (+1, -1 or 0) measured
// before the pattern started. ok is false when nothing matched.
func Detect(window []market.Bar, atr float64, trend int) (p Pattern, ok bool) {
	if len(window) == 0 || atr <= 0 {
		return Pattern{}, false
	}
	t := th{atr: atr}
	n := len(window)
	c0 := mk(window[n-1])
	var c1, c2 candle
	if n >= 2 {
		c1 = mk(window[n-2])
	}
	if n >= 3 {
		c2 = mk(window[n-3])
	}
	strength := func(x float64) float64 { return math.Max(0, math.Min(1, x)) }

	if n >= 3 {
		// --- triple-bar, bullish ---
		if trend <= 0 {
			if c2.bear && t.long(c2) && t.small(c1) && c1.bodyTop < c2.bodyBot+0.3*t.atr && c0.bull && c0.c > c2.mid {
				name := "Morning Star"
				if t.doji(c1) {
					name = "Morning Doji Star"
				}
				if t.doji(c1) && c1.h < c2.l && c1.h < c0.l {
					name = "Bullish Abandoned Baby"
				}
				return Pattern{name, Bullish, 3, strength((c0.c - c2.mid) / t.atr)}, true
			}
		}
		if c2.bull && c1.bull && c0.bull && t.long(c2) && t.long(c1) && t.long(c0) &&
			c1.c > c2.c && c0.c > c1.c && c1.o > c2.bodyBot && c1.o < c2.c && c0.o > c1.bodyBot && c0.o < c1.c &&
			t.tiny(c0.upper) && t.tiny(c1.upper) {
			return Pattern{"Three White Soldiers", Bullish, 3, strength((c0.c - c2.o) / (3 * t.atr))}, true
		}
		if trend <= 0 && c2.bear && t.long(c2) && c1.bull && c1.bodyTop < c2.bodyTop && c1.bodyBot > c2.bodyBot && c0.bull && c0.c > c2.o {
			return Pattern{"Three Inside Up", Bullish, 3, strength((c0.c - c2.o) / t.atr)}, true
		}
		if trend <= 0 && c2.bear && c1.bull && c1.bodyBot < c2.bodyBot && c1.bodyTop > c2.bodyTop && c0.bull && c0.c > c1.c {
			return Pattern{"Three Outside Up", Bullish, 3, strength((c0.c - c1.c) / t.atr)}, true
		}
		// --- triple-bar, bearish ---
		if trend >= 0 {
			if c2.bull && t.long(c2) && t.small(c1) && c1.bodyBot > c2.bodyTop-0.3*t.atr && c0.bear && c0.c < c2.mid {
				name := "Evening Star"
				if t.doji(c1) {
					name = "Evening Doji Star"
				}
				if t.doji(c1) && c1.l > c2.h && c1.l > c0.h {
					name = "Bearish Abandoned Baby"
				}
				return Pattern{name, Bearish, 3, strength((c2.mid - c0.c) / t.atr)}, true
			}
		}
		if c2.bear && c1.bear && c0.bear && t.long(c2) && t.long(c1) && t.long(c0) &&
			c1.c < c2.c && c0.c < c1.c && c1.o < c2.bodyTop && c1.o > c2.c && c0.o < c1.bodyTop && c0.o > c1.c &&
			t.tiny(c0.lower) && t.tiny(c1.lower) {
			return Pattern{"Three Black Crows", Bearish, 3, strength((c2.o - c0.c) / (3 * t.atr))}, true
		}
		if trend >= 0 && c2.bull && t.long(c2) && c1.bear && c1.bodyTop < c2.bodyTop && c1.bodyBot > c2.bodyBot && c0.bear && c0.c < c2.o {
			return Pattern{"Three Inside Down", Bearish, 3, strength((c2.o - c0.c) / t.atr)}, true
		}
		if trend >= 0 && c2.bull && c1.bear && c1.bodyBot < c2.bodyBot && c1.bodyTop > c2.bodyTop && c0.bear && c0.c < c1.c {
			return Pattern{"Three Outside Down", Bearish, 3, strength((c1.c - c0.c) / t.atr)}, true
		}
	}

	if n >= 2 {
		// --- double-bar, bullish ---
		if trend <= 0 {
			if c1.bear && c0.bull && c0.bodyBot <= c1.bodyBot && c0.bodyTop >= c1.bodyTop && c0.body > c1.body {
				return Pattern{"Bullish Engulfing", Bullish, 2, strength(c0.body / (1.5 * t.atr))}, true
			}
			if c1.bear && t.long(c1) && c0.bull && c0.bodyTop < c1.bodyTop && c0.bodyBot > c1.bodyBot {
				return Pattern{"Bullish Harami", Bullish, 2, strength(c1.body / (2 * t.atr))}, true
			}
			if c1.bear && t.long(c1) && c0.bull && c0.o < c1.c && c0.c > c1.mid && c0.c < c1.o {
				return Pattern{"Piercing Line", Bullish, 2, strength((c0.c - c1.mid) / t.atr)}, true
			}
			if c1.bear && c0.bull && t.near(c1.l, c0.l) {
				return Pattern{"Tweezer Bottom", Bullish, 2, 0.4}, true
			}
		}
		if c1.bear && c0.bull && c0.o > c1.o && t.long(c0) {
			return Pattern{"Bullish Kicker", Bullish, 2, strength((c0.o - c1.o) / t.atr)}, true
		}
		// --- double-bar, bearish ---
		if trend >= 0 {
			if c1.bull && c0.bear && c0.bodyTop >= c1.bodyTop && c0.bodyBot <= c1.bodyBot && c0.body > c1.body {
				return Pattern{"Bearish Engulfing", Bearish, 2, strength(c0.body / (1.5 * t.atr))}, true
			}
			if c1.bull && t.long(c1) && c0.bear && c0.bodyTop < c1.bodyTop && c0.bodyBot > c1.bodyBot {
				return Pattern{"Bearish Harami", Bearish, 2, strength(c1.body / (2 * t.atr))}, true
			}
			if c1.bull && t.long(c1) && c0.bear && c0.o > c1.c && c0.c < c1.mid && c0.c > c1.o {
				return Pattern{"Dark Cloud Cover", Bearish, 2, strength((c1.mid - c0.c) / t.atr)}, true
			}
			if c1.bull && c0.bear && t.near(c1.h, c0.h) {
				return Pattern{"Tweezer Top", Bearish, 2, 0.4}, true
			}
		}
		if c1.bull && c0.bear && c0.o < c1.o && t.long(c0) {
			return Pattern{"Bearish Kicker", Bearish, 2, strength((c1.o - c0.o) / t.atr)}, true
		}
	}

	// --- single-bar ---
	if c0.rng <= 0 {
		return Pattern{}, false
	}
	switch {
	case trend <= 0 && t.doji(c0) && c0.lower >= 0.6*c0.rng && t.tiny(c0.upper):
		return Pattern{"Dragonfly Doji", Bullish, 1, strength(c0.lower / (1.5 * t.atr))}, true
	case trend < 0 && c0.lower >= 2*c0.body && c0.upper <= 0.5*c0.body+0.05*t.atr && c0.body > 0 && c0.rng >= 0.8*t.atr:
		return Pattern{"Hammer", Bullish, 1, strength(c0.lower / (2 * t.atr))}, true
	case trend < 0 && c0.upper >= 2*c0.body && c0.lower <= 0.5*c0.body+0.05*t.atr && c0.body > 0 && c0.rng >= 0.8*t.atr:
		return Pattern{"Inverted Hammer", Bullish, 1, strength(c0.upper / (3 * t.atr))}, true
	case c0.bull && t.long(c0) && c0.upper <= 0.05*c0.rng && c0.lower <= 0.05*c0.rng:
		return Pattern{"Bullish Marubozu", Bullish, 1, strength(c0.body / (2 * t.atr))}, true
	case trend >= 0 && t.doji(c0) && c0.upper >= 0.6*c0.rng && t.tiny(c0.lower):
		return Pattern{"Gravestone Doji", Bearish, 1, strength(c0.upper / (1.5 * t.atr))}, true
	case trend > 0 && c0.upper >= 2*c0.body && c0.lower <= 0.5*c0.body+0.05*t.atr && c0.body > 0 && c0.rng >= 0.8*t.atr:
		return Pattern{"Shooting Star", Bearish, 1, strength(c0.upper / (2 * t.atr))}, true
	case trend > 0 && c0.lower >= 2*c0.body && c0.upper <= 0.5*c0.body+0.05*t.atr && c0.body > 0 && c0.rng >= 0.8*t.atr:
		return Pattern{"Hanging Man", Bearish, 1, strength(c0.lower / (3 * t.atr))}, true
	case c0.bear && t.long(c0) && c0.upper <= 0.05*c0.rng && c0.lower <= 0.05*c0.rng:
		return Pattern{"Bearish Marubozu", Bearish, 1, strength(c0.body / (2 * t.atr))}, true
	case t.doji(c0) && c0.rng >= 1.5*t.atr:
		return Pattern{"Long-Legged Doji", Neutral, 1, 0.3}, true
	case t.doji(c0):
		return Pattern{"Doji", Neutral, 1, 0.2}, true
	case t.small(c0) && c0.upper > c0.body && c0.lower > c0.body:
		return Pattern{"Spinning Top", Neutral, 1, 0.1}, true
	}
	return Pattern{}, false
}

// Names lists every pattern Detect can return, in priority order.
var Names = []string{
	"Morning Star", "Morning Doji Star", "Bullish Abandoned Baby", "Three White Soldiers", "Three Inside Up", "Three Outside Up",
	"Evening Star", "Evening Doji Star", "Bearish Abandoned Baby", "Three Black Crows", "Three Inside Down", "Three Outside Down",
	"Bullish Engulfing", "Bullish Harami", "Piercing Line", "Tweezer Bottom", "Bullish Kicker",
	"Bearish Engulfing", "Bearish Harami", "Dark Cloud Cover", "Tweezer Top", "Bearish Kicker",
	"Dragonfly Doji", "Hammer", "Inverted Hammer", "Bullish Marubozu",
	"Gravestone Doji", "Shooting Star", "Hanging Man", "Bearish Marubozu",
	"Long-Legged Doji", "Doji", "Spinning Top",
}
