# docs/02_design.md — RET-C2-027 Markdown & Clearance Plan Generator

## 1. Template Metadata

| Field | Value |
|---|---|
| **Template ID** | RET-C2-027 |
| **Category** | Cat 2 (domain-specific document-generation pipeline) |
| **Industry** | RET (Retail) |
| **L1 Base (framework base class)** | `AgentBaseGraph` — direct framework inheritance |
| **Inner graph base** | `BaseGraph` — direct framework inheritance |
| **Pattern** | Document generation — two-layer nested Cat 2 architecture |
| **Generation mode** | Deterministic. Every figure in the plan is computed from the submitted records and the configured rules; no language model is invoked. |

---

## 2. Architecture Overview

The outer backbone is the fixed framework backbone and is never modified. Its `main`
slot is a `GraphNode` subclass (`MarkdownPlanGraphNode`) that delegates the whole
domain workflow to a separate inner `BaseGraph` (`DomainWorkflowGraph`).

```
Outer backbone (AgentBaseGraph — fixed, never override add_edges()):
  START
    -> initialize        (framework initialize node — schema version, session id, trust level)
    -> pre_process       (PreProcessNode — the request boundary: validate every caller field)
    -> main              (MarkdownPlanGraphNode — delegates to the inner graph)
         |
         +---> Inner DomainWorkflowGraph (BaseGraph — 5 domain nodes):
                START
                  --> input_parse       (InputParseNode — order the records by markdown urgency)
                  --> template_select   (TemplateSelectNode — resolve the plan profile)
                  --> data_enrich       (DataEnrichNode — discount ceilings and category groups)
                  --> document_generate (DocumentGenerateNode — compute and render the plan)
                  --> quality_check     (QualityCheckNode — enforce the plan invariants)
                END
         |
         (merge_output maps markdown_plan --> outer state.result)
    --> post_process      (PostProcessNode — the output boundary)
    --> finalize          (framework finalize node — response metadata, total elapsed time)
  END
```

### Layer Responsibility Table

| Layer | Class | File | Responsibility |
|---|---|---|---|
| Outer backbone | `RetC2027Agent` | `src/graph/graph.py` | `AgentBaseGraph` subclass; fills the 5 backbone slots; never overrides `add_edges()` |
| Subgraph bridge | `MarkdownPlanGraphNode` | `src/graph/graph.py` | `GraphNode` subclass in the `main` slot; implements `get_subgraph()`, `extract_input()`, `merge_output()`, `_parent_config()` |
| Caller bridge | `set_caller_contract` / `get_caller_contract` | `src/graph/context_bridge.py` | Carries the validated contract across the outer/inner boundary |
| Inner domain graph | `DomainWorkflowGraph` | `src/graph/domain_workflow_graph.py` | `BaseGraph` subclass; registers the 5 domain nodes; wires the linear topology; seeds the inner initial state |
| Request boundary | `PreProcessNode` | `src/nodes/pre_process_node.py` | Validates every caller field against `src/services/caller_contract.py`; writes `caller_contract` |
| Domain node 1 | `InputParseNode` | `src/nodes/input_parse_node.py` | Scores markdown urgency; orders the records deterministically; writes `parsed_skus` |
| Domain node 2 | `TemplateSelectNode` | `src/nodes/template_select_node.py` | Resolves the plan profile from the dominant category; writes `selected_profile` |
| Domain node 3 | `DataEnrichNode` | `src/nodes/data_enrich_node.py` | Computes per-product ceilings and category groups; writes `enriched_data` |
| Domain node 4 | `DocumentGenerateNode` | `src/nodes/document_generate_node.py` | Computes the schedule, channel split and clearance projection; renders the plan; writes `markdown_plan_raw` |
| Domain node 5 | `QualityCheckNode` | `src/nodes/quality_check_node.py` | Enforces the plan invariants on the rendered document; writes `markdown_plan` and `clearance_metadata` |
| Output boundary | `PostProcessNode` | `src/nodes/post_process_node.py` | Refuses to release a plan that breaks the stated invariant; writes `formatted_output` |
| Request contract | `caller_contract` | `src/services/caller_contract.py` | The single definition of what a caller may send, and of the personal-data shapes handled in both directions |

---

## 3. Request Contract

Two channels carry the same payload, and both are validated by one module.

| Channel | Shape |
|---|---|
| `input` (free text) | A plain merchandising note, or a JSON document: a bare array of product records, or an object with `skus` and the optional plan parameters |
| `input_context` (structured invocation parameters) | The same fields as real keys. Where both channels carry a field, the structured one wins |

### Fields

| Field | Required | Contract |
|---|---|---|
| `skus[].sku_id` | yes | Inert label, `[a-z0-9_]{1,32}` after normalisation |
| `skus[].category` | yes | Inert label, `[a-z0-9_]{1,32}` after normalisation |
| `skus[].days_on_shelf` | yes | Finite whole number, 0–3650 |
| `skus[].sell_through_rate` | yes | Finite number; accepted as a share (0.0–1.0) or a percentage (0–100) |
| `skus[].current_price` | yes | Finite number, greater than 0 and at most 1e9 |
| `skus[].margin_floor_pct` | no | Finite number, 0–100; default 0 |
| `skus[].note` | no | Short text, at most 400 characters, whitespace-collapsed; not rendered into the plan |
| `channel` | no | Inert label |
| `horizon_weeks` | no | Finite whole number, 1–12 |
| `max_discount_pct` | no | Finite number, 0–100. Narrowed against the configured cap — a caller may tighten the ceiling, never widen it |

Structural caps: at most 500 records, at most 24 keys per record, at most 200,000
characters of free text, and a 256 KB cap on the serialized structured parameters
enforced at the HTTP entry point.

Every stopped request names the field and never repeats its value.

### Declining a request without terminating the run

A request that is not served stops in one of two ways, chosen by what the caller
can do about it:

- **A value the caller can correct** — no product records, a field that fails its
  contract, free text past the size cap. The run **completes**, carrying a reason
  code (`EMPTY_INPUT`, `QUESTION_TOO_LONG`, `INVALID_REQUEST`) through state. No
  contract is published, so no plan is generated; `PostProcessNode` turns the code
  into the fixed caller-facing sentence held in `src/services/failure_message.py`
  and puts it in the caller-facing slot, and the main-slot node skips the inner
  graph so the settled reason is not overwritten by a vaguer second one.
  Terminating instead would end the calling surface's turn and surface only an
  exception type, leaving the reason reachable solely from the audit trail — the
  caller could not correct the figure and send the request again on the same
  conversation. The reason code is an internal state field and is not published in
  the response envelope: the caller reads the sentence, not the code.
- **A refusal the agent owns** — a disallowed instruction pattern on either
  channel, an output-boundary violation, a broken plan invariant at the quality
  gate. These **terminate** with an error status. Rewording the request is not a
  route past them, so presenting them as correctable would be a false statement
  about the agent's behaviour.

The branch is chosen at the call site by the reason class the stop carries — the
screen refuses directly, and the contract parser raises an error whose own class
decides which of the two applies — never by inspecting the message text.

### Why numbers go through a finite + bounded parser

`float("NaN")` and `float("Infinity")` parse successfully and arrive intact through
raw JSON. Every comparison against a non-finite value is False, so an unchecked
figure would make a discount ceiling neither enforced nor reported, and the plan
would render `nan%` with nothing in the log to explain it. Booleans are rejected
explicitly because `True` is an `int` in Python.

### Disallowed instructions

The screen covers chat-template control tokens as a class (`<|...|>`, `[INST]`,
`<<SYS>>`) as well as directive phrases, because a screen written around phrases
alone does not see the token forms at all. The parsed payload is walked depth-first
including mapping KEYS, so an escape sequence in the raw request text cannot hide a
pattern from it, and nesting is bounded. Each string is screened as received, after
the personal-data strip, with zero-width and bidi characters removed, and
re-assembled with simple markup removed — a strip is not a refusal, and it can turn
a directive no pattern matched into plain prose that reads perfectly.

Every directive pattern requires a verb AND its object, so ordinary merchandising
prose ("the buyer will act as agreed and ignore the old promotion calendar") is not
refused. Refusing genuine work is the more damaging of the two failure directions.

---

## 4. Plan Arithmetic

Every figure below is reproducible by hand from the plan's own legend.

| Quantity | Definition |
|---|---|
| Markdown priority | `100 * (0.6 * min(days_on_shelf / 180, 1) + 0.4 * (100 - sell_through_pct) / 100)`. Orders the records; reported in the plan |
| Product ceiling | The tightest of: the configured `plan.max_discount_pct`, the category's `max_discount_pct`, the caller's requested cap, and `100 - margin_floor_pct` |
| Group ceiling | The tightest ceiling any member of the group carries, so following a group row cannot take a product below its own floor |
| Weekly discount | `round(group_ceiling * week / horizon_weeks)` — a monotonic ramp that lands on the ceiling in the final week |
| Projected clearance | `sell_through_pct + (100 - sell_through_pct) * min(1, avg_discount * uplift_factor / 100)`, capped at 100 |
| Channel split | `plan.channel_split.<category>.online_pct`, with the in-store share as the remainder |

The margin floor is read as a floor on the share of the price that must survive the
markdown: a product carrying a 20% floor may be discounted by at most 80%.

---

## 5. Output Invariant

The plan states its invariant in its own header, and the invariant is enforced twice
— by the quality gate on the rendered document, and again at the output boundary.

1. **No recommended discount exceeds its group's ceiling.** The check reads the
   rendered table cells, not the model they came from: a check that re-read the
   model would only prove the model is self-consistent.
2. **No monetary amount is rendered.** The plan reports percentages, counts and
   weeks. Prices are caller data used to derive the ceilings; publishing them back
   adds nothing a merchandiser does not already have, and a document with no
   currency in it cannot mis-round one. This is why the fleet's monetary-precision
   grid does not apply to this template: it renders no monetary aggregates. The
   property is enforced rather than assumed, so a future change to the renderer
   cannot reintroduce amounts silently.
3. **Nothing credential-shaped and no shopper personal data is released.**

### Two independent layers

| Direction | Where | What |
|---|---|---|
| Inbound | `src/services/caller_contract.py` | Personal-data shapes are rewritten out of caller free text before anything is stored; fields named for shopper data are withheld wholesale; every rendered string is restricted to an inert alphabet |
| Outbound | `src/nodes/post_process_node.py` | The released surface — the plan AND the summary statistics — is scanned recursively and refused if it still matches |

Both directions read ONE pattern definition, so they cannot drift apart.

The card-number shape is deliberately narrower than "any long digit run": retail
article numbers ARE long digit runs (EAN-13 is 13 digits, ITF-14 is 14), and a gate
that refused those would refuse the agent's own subject matter. A payment card
number is 16–19 contiguous digits, or written in the groups a card is printed in;
neither shape is a retail article number.

Credential shapes are checked with the framework's own detector rather than a local
pattern list, widened by two extra patterns and never narrowed. A local list that is
narrower anywhere is a containment bypass: the framework raises on a value the node
missed, the node wrapper turns that into a bare error result carrying no cleared
fields, and because partial results are MERGED into graph state the previous
un-gated plan survives in `result` and is released inside the error envelope.

### Containment on violation

Returning an error is not enough on its own. The response envelope resolves to
`formatted_output or result` whatever the status, so a gate that raised — or that
set an error status without clearing the fields — would still ship the un-gated
plan. The output boundary therefore CLEARS every output-bearing field as it blocks,
and the notice it substitutes is truthy: an empty string in `formatted_output` would
activate the fallback and produce the exact leak the clearing exists to prevent.

Violation messages name the pattern and the field, never the matched value.

---

## 6. State Fields

`src/schemas/state.py` extends the framework state. Structured fields are stored as
JSON STRINGS: graph checkpoints are serialized with msgpack, which cannot round-trip
arbitrary containers safely.

| Field | Written by | Contents |
|---|---|---|
| `validated_input` | `PreProcessNode` | Short, personal-data-stripped summary of the request |
| `caller_contract` | `PreProcessNode` | JSON string of the validated contract |
| `plan_config` | inner graph initial state | JSON string of the live plan settings |
| `parsed_skus` | `InputParseNode` | JSON string of the ordered records |
| `selected_profile` | `TemplateSelectNode` | Inert profile key |
| `enriched_data` | `DataEnrichNode` | JSON string of the planning dataset |
| `markdown_plan_raw` | `DocumentGenerateNode` | The rendered plan, before the quality gate |
| `markdown_plan` | `QualityCheckNode` | The plan, once it has passed every check |
| `clearance_metadata` | `QualityCheckNode` | JSON string of the plan summary statistics |
| `result` / `formatted_output` | `merge_output` / `PostProcessNode` | The released plan |

---

## 7. Configuration

| File | Role |
|---|---|
| `config/agent.yaml` | Static manifest — identity, entry point, trust level, compile-time requirements. Every key sits at ROOT level; there is no `agent:` block |
| `config/config.yaml` | Runtime parameters — `max_retry`, `timeout_s`, and the `plan` block |

Runtime settings travel by ONE route. `MarkdownPlanGraphNode._parent_config()` reads
`config/config.yaml`, validates each value through the same bounded parsers caller
data uses, and passes the result to the inner graph's constructor; the inner graph
republishes it into inner state. Domain nodes read it from state.

This matters because a node's `execute()` is called with the state and nothing else.
A node that accepted a second `config` parameter would receive `None` on every real
invocation and fall back silently to its module defaults, while unit tests that
passed one explicitly stayed green. Every test in this repository calls `execute()`
with one argument for that reason.

---

## 8. Trust Model

The manifest advertises `VERIFIED_EXTERNAL`, and every node declares exactly that.
A node demanding more would be unreachable through the public entry point: the
framework's trust gate runs before `execute()` on every invocation, so every real
request would stop there and the agent would return an error for every call.

In a standalone deployment nothing establishes caller trust on its own. The HTTP
adapter is the entry-point auth boundary: when `INVOKE_AUTH_TOKEN` is set, a caller
that no upstream middleware vouched for must present it as a Bearer token and is
then run at `VERIFIED_EXTERNAL`. Middleware-established trust is never demoted.

The adapter also screens the structured parameters for credential shapes before
`invoke()`, using the same detector the framework's own gate calls. The framework's
first node copies those parameters verbatim into its result and the gate scans every
value of every result, so a credential-shaped string anywhere in them fails the first
node before any template code runs — an opaque error naming nothing. The request
cannot succeed either way; refusing it at the adapter changes an opaque failure into
an actionable one that names the field.

---

## 9. Failure Modes

| Condition | Behaviour |
|---|---|
| No product records supplied | Declined at the request boundary — the run completes carrying `EMPTY_INPUT`, no contract is published and nothing is generated |
| A field fails its contract | Declined, naming the field and never its value — the run completes carrying `INVALID_REQUEST` |
| A disallowed instruction pattern | Refused and the run terminates, naming the pattern and never the payload |
| A credential shape in the structured parameters | Refused at the HTTP adapter with status 400, naming the field |
| The structured parameters exceed 256 KB | Refused at the HTTP adapter with status 413 |
| A rendered plan breaks an invariant | The quality gate withholds the plan and the run ends in error |
| A released value matches a disallowed shape | The output boundary blocks, clears every output-bearing field and substitutes a truthy notice |

---

## 10. Design Decisions

| Decision | Alternative considered | Why |
|---|---|---|
| Deterministic computation | Language-model generation | Every figure is reproducible and the same request always produces the same plan. The prior revision called a model through an import the framework does not carry, so the call failed on every invocation and a fixed placeholder document was returned whatever the caller sent |
| Rules in `config/config.yaml` | Rules loaded from a file path named in config | A path that did not exist degraded to an empty policy and the plan rendered without the rules it claimed to apply, silently |
| No monetary amounts in the plan | Amounts rounded to a published grid | A grid has to be enforced for every representation a number can take; not rendering amounts at all removes the class instead of policing it, and costs a merchandiser nothing |
| Structured parameters for the records | The records inside the free-text field | The framework masks the free-text field at every node boundary, and product codes that look like account numbers trip the masking heuristics, so the pipeline would plan from corrupted data |
| Shared state schema across both graphs | Separate inner and outer schemas | One definition, no field duplication, and the bridge carries only the validated contract |
