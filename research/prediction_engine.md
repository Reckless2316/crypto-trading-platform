# Prediction prototype

`prediction/markov_prototype.py` implements a small discrete hidden Markov model with three states (`L`, `HU`, `HD`), an emission table, and log-space Viterbi decoding. The demo classifies signed price changes as positive, negative, or near-zero.

The transition and emission probabilities are hand-set examples. There is no model fitting, volatility estimator, validation against historical data, or trading integration. Treat the output as a software prototype, not a forecast.

The operational dashboard strategy is separate: it uses an EMA 12/26 crossover defined in `strategies/signal_to_bot.py` and `strategies/dsl/ema_12_26_crossover.json`.
