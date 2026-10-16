# SURGE — Subagent Delegation Fix Roadmap

## Issue
Subagent delegation fails consistently with: `local-llm-tinkerer is not a valid model ID` (13 attempts across batches deleg_886756b5, deleg_464d40a5, deleg_485a38d0, deleg_2bcce6ea).

Even after changing `delegation.model` to `claude-opus-5-5`, the provider (openrouter) rejects it. The user's profile config has model `claude-opus-5-5` (Anthropic provider) but the environment uses `openrouter`. The broken profile `local-llm-tinkerer` has an invalid model reference.

## Evidence (observed)
- `hermes config get model` returns `default: claude-opus-5-5`
- `hermes config get provider` returns `provider: anthropic`
- `HERMES_PROVIDER` not explicitly set; environment shows `AUXILIARY_APPROVAL_PROVIDER=openrouter`
- `AUXILIARY_APPROVAL_MODEL=nvidia/nemotron-3.5-lightning:free` (free model on openrouter)
- `hermes config set delegation.model` written to `/home/reckless/.hermes/config.yaml` (verified)
- Profile `local-llm-tinkerer` exists and is broken (invalid model ID reference)

## Fix Steps (observed + actionable)
1. Confirm provider/model pairing matches: `hermes config get model` + check provider setting.
2. Set delegation.model to a valid openrouter ID: `nvidia/nemotron-3.5-lightning:free` (free, done).
3. Clean broken profile references: check if any profile or config references `local-llm-tinkerer` and remove/update.
4. Re-test subagent dispatch with fixed model.
5. If provider/model pairing remains broken: disable subagent delegation; use manual sequential lanes (already working for SURGE build).

## Workaround (already working)
Manual autonomous execution complete for SURGE. Subagent delegation broken; work continues manually.
