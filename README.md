# ENE-C2-127 — Electricity Market Extract Anomaly Triage Agent

> **Category**: Cat 2 (domain workflow (a job to be done))
> **Industry**: Energy

## Overview

Given a market-data extract that has already passed upstream quality validation (rows with area, metric, slot, value and source) the agent produces an analyst triage worklist: it enforces the quality-validation gate, normalises the rows against the shipped baseline and threshold policy, detects policy-defined anomalies per area and metric (missing interval, duplicate, discontinuity, threshold exception) with a severity band, attaches the cited policy clauses to each anomaly, and marks every finding as pending a human decision. Detection and clause retrieval are deterministic — the template has no LLM. An extract without a passed quality status is not triaged, one with no valid rows or no anomalies gets an out-of-scope answer instead of invented findings, source references count as citations only when they name an authorised system of record, an ungrounded anomaly group causes the worklist to be withheld, and the worklist is marked DRAFT. The anomaly policy and thresholds shipped here are a small seeded sample — replace them with your own.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | 3.11 or later (`requires-python = ">=3.11"`) |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent raises
`PlatformRequired` during graph compile / start-up preflight rather than starting in a partially
working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
