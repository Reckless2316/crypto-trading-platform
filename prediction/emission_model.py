#!/usr/bin/env python3
"""Basic emission model extension for Markov prototype — connects to BTC price observations."""
# Pure Python — no numpy dependency for the demo

# Emission probabilities: given a hidden state, what's the probability of observing each price change direction?
# Simplified for demo: emission is a mapping from state -> probability of positive/negative/near-zero observations
EMISSION_PROBS = {
    "L": {"positive": 0.40, "negative": 0.30, "near_zero": 0.30},
    "HU": {"positive": 0.75, "negative": 0.15, "near_zero": 0.10},
    "HD": {"positive": 0.15, "negative": 0.75, "near_zero": 0.10},
}

def classify_observation(price_change_pct: float) -> str:
    """Classify an observation (BTC price % change) into: positive, negative, near_zero."""
    if price_change_pct > 0.01:
        return "positive"
    elif price_change_pct < -0.01:
        return "negative"
    return "near_zero"

def emission_probability(state: str, observation_type: str) -> float:
    """P(observation | state) — basic emission probability."""
    return EMISSION_PROBS.get(state, {"positive": 0.33, "negative": 0.33, "near_zero": 0.34}).get(observation_type, 0.33)

def demo_with_emission():
    """Full demo: simulate BTC price changes, classify observations, show emission probabilities."""
    # Simulated BTC hourly changes (positive = up, negative = down, near zero = range)
    changes = [0.05, -0.02, -0.08, 0.10, 0.15, 0.02, -0.05, -0.12, 0.03, 0.08]
    observations = [classify_observation(c) for c in changes]
    print("BTC hourly % changes:", changes)
    print("Classified observations:", observations)
    for obs in observations:
        for state in ["L", "HU", "HD"]:
            prob = emission_probability(state, obs)
            if prob > 0.15:
                print(f"  P({obs}|{state}) = {prob:.2f}")

if __name__ == "__main__":
    demo_with_emission()
    print()
    print("Phase 2: Basic emission model implemented (pure Python).")
    print("Connects to Phase 1 (BTC price changes) and feeds Phase 3 (strategy signal generation).")
