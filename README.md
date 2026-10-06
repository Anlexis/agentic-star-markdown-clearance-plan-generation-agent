# Markdown & Clearance Plan Generation Agent

AI agent for generating retail markdown and clearance plans, built with Agentic Star.

> **Category**: Cat 2 (domain-specific document-generation pipeline)
> **Industry**: Retail
> **Template ID**: RET-C2-027

## Overview

Turns a retailer's slow-moving product list into a week-by-week markdown and clearance plan —
which products go into which discount group, how deep the discount goes in each week of the
horizon, how the stock is split between online and in-store, and how much of it is expected to
have cleared by the end.

The merchandiser sends the product records as structured parameters or as a JSON payload. The
agent validates every field against explicit bounds, works out the deepest discount each product
may be offered at, groups the products by category, computes the schedule and the clearance
projection, and renders the finished plan.

The plan is COMPUTED, not written by a language model: every figure in it is a function of the
submitted records and the configured rules, so the same request always produces the same plan and
every number can be reproduced by hand from the legend the plan carries.

Two properties are enforced at the output boundary rather than assumed. No recommended discount
may exceed the ceiling its group was planned to — the tightest of the configured cap, the category
rule, the caller's own requested cap and each product's margin floor. And the plan renders no
monetary amounts at all: it reports percentages, counts and weeks, so a currency figure can
neither be mis-rounded nor leaked. Shopper personal data and credential-shaped strings are
rewritten out of caller text on the way in and refused release on the way out.

Typical users are retail merchandising teams that run clearance cycles on a schedule and need the
discount policy applied the same way every time, whoever builds the plan.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >= 3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the framework version does not match, the agent fails at
graph compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

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
tests/        unit and boundary tests
config/       agent manifest and runtime parameters
deploy/       local run recipe and a sample invocation payload
docs/         design and operational documentation
```

`config/agent.yaml` is the static manifest (identity and compile-time requirements);
`config/config.yaml` carries the runtime parameters — the discount ceiling, the schedule horizon,
the per-category margin rules and the channel split. See `docs/` for the design and the test
specification.

## Customising

1. Adjust `config/config.yaml` — the discount ceiling, the horizon, and the margin rule and
   channel split for each of your own product categories.
2. Replace the sample payload under `deploy/` with records exported from your own systems.
3. Review the node implementations under `src/nodes/` for domain-specific logic; the request
   contract every caller field is checked against lives in `src/services/caller_contract.py`,
   and the plan arithmetic lives in `src/nodes/document_generate_node.py`.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
