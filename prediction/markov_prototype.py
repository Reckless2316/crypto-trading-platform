#!/usr/bin/env python3
"""Discrete HMM regime prototype with a real log-space Viterbi decoder."""
from __future__ import annotations

import math

try:
    from .emission_model import EMISSION_PROBS, classify_observation
except ImportError:
    from emission_model import EMISSION_PROBS, classify_observation

STATES = ["L", "HU", "HD"]
TRANSITION_MATRIX = [
    [0.70, 0.20, 0.10],
    [0.25, 0.65, 0.10],
    [0.30, 0.10, 0.60],
]
INITIAL_PROBS = [1 / 3, 1 / 3, 1 / 3]


class CryptoRegimeHMM:
    def __init__(self, states: list[str] = None, transition_matrix: list[list[float]] = None,
                 emission_probs: dict = None, initial_probs: list[float] = None):
        self.states = states or STATES
        self.trans = transition_matrix or TRANSITION_MATRIX
        self.emissions = emission_probs or EMISSION_PROBS
        self.initial = initial_probs or [1 / len(self.states)] * len(self.states)
        if len(self.trans) != len(self.states) or any(len(row) != len(self.states) for row in self.trans):
            raise ValueError("Transition matrix dimensions must match states")
        if len(self.initial) != len(self.states):
            raise ValueError("Initial probability dimensions must match states")

    @staticmethod
    def _log(probability: float) -> float:
        return math.log(probability) if probability > 0 else float("-inf")

    def viterbi(self, observations: list[float | str]) -> list[str]:
        """Return the most likely hidden-state path for signed percentage changes."""
        if not observations:
            return []
        symbols = [value if isinstance(value, str) else classify_observation(float(value)) for value in observations]
        scores = [self._log(self.initial[i]) + self._log(self.emissions[self.states[i]].get(symbols[0], 0.0))
                  for i in range(len(self.states))]
        backpointers: list[list[int]] = []
        for symbol in symbols[1:]:
            next_scores = []
            previous_states = []
            for destination, state in enumerate(self.states):
                candidates = [scores[source] + self._log(self.trans[source][destination])
                              for source in range(len(self.states))]
                best_source = max(range(len(candidates)), key=candidates.__getitem__)
                previous_states.append(best_source)
                next_scores.append(candidates[best_source] + self._log(self.emissions[state].get(symbol, 0.0)))
            scores = next_scores
            backpointers.append(previous_states)
        index = max(range(len(scores)), key=scores.__getitem__)
        path = [index]
        for previous_states in reversed(backpointers):
            index = previous_states[index]
            path.append(index)
        return [self.states[i] for i in reversed(path)]

    def predict_next_state_prob(self, current_state_idx: int) -> dict[str, float]:
        return {self.states[i]: float(probability) for i, probability in enumerate(self.trans[current_state_idx])}


if __name__ == "__main__":
    hmm = CryptoRegimeHMM()
    changes = [0.05, -0.02, -0.08, 0.10, 0.15, 0.02, -0.05, -0.12, 0.03, 0.08]
    print("Viterbi regime path:", " -> ".join(hmm.viterbi(changes)))
    print("Next state from low volatility:", hmm.predict_next_state_prob(0))
