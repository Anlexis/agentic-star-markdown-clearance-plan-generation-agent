# docs/03_test_spec.md — RET-C2-027 Markdown & Clearance Plan Generator

## Test Strategy

- Framework: pytest. No platform connection and no network access is required.
- Two layers:
  - **Unit** — the request contract, the planning arithmetic and the two boundary
    nodes, each exercised through `execute(state)` with ONE argument, exactly as the
    graph runtime calls it.
  - **Boundary** — end-to-end through the real HTTP entry point, plus the structural
    checks that hold for every template.
- Every boundary case goes in as an HTTP request and comes back as an HTTP response,
  so what is asserted is what a caller receives. A hand-built state can validate a
  layer that cannot fire in reality; these cannot.

### Test files

| File | Scope |
|---|---|
| `tests/unit/test_caller_contract.py` | The request contract: bounded numbers, inert labels, personal-data shapes, the instruction screen, request assembly |
| `tests/unit/test_plan_nodes.py` | Ordering, profile selection, ceilings, schedule arithmetic, the rendered plan, the quality gate |
| `tests/unit/test_pre_post_process_nodes.py` | The request boundary and the output boundary, called directly |
| `tests/unit/test_runtime_config.py` | The declared runtime configuration reaches the graph |
| `tests/unit/test_graph_composition.py` | Backbone slots, the nested boundary, and the trust level every node declares |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | Framework compliance (TC-06, TC-07) |
| `tests/proof_of_boundary/test_pb_invoke_endpoint.py` | End-to-end through `/invoke` |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | PB-6 — node call order and the trust-gate denial path |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | PB-7 — human-review interrupt propagation (not applicable to this template) |
| `tests/proof_of_boundary/test_import_isolation.py` | PB-4 — platform SDK import isolation |
| `tests/proof_of_boundary/test_import_isolation_pb03.py` | PB-03 — the narrower platform SDK import scan |
| `tests/proof_of_boundary/test_state_safety.py` | PB-2 / PB-5 — the state schema carries no credential fields and no unserialisable types |

---

## Unit Cases

### TC-01 — A valid request produces a complete plan computed from the caller's records

| Field | Value |
|---|---|
| **Where** | `test_plan_nodes.py::TestRenderedPlan` |
| **Input** | A contract carrying two records in different categories |
| **Expected** | Every promised section is present and populated; changing a product code changes the plan; two identical requests render identical plans |

### TC-02 — A field that fails its contract stops the request without echoing the value

| Field | Value |
|---|---|
| **Where** | `test_caller_contract.py::TestFiniteNumbers`, `test_pre_post_process_nodes.py::TestRequestBoundary` |
| **Input** | A record with a non-numeric price, and records missing a required field |
| **Expected** | At the parser, the request raises; at the node, it is **declined** — the run completes carrying `INVALID_REQUEST` and publishes no contract, because this is a value the caller can correct and send again on the same conversation. Either way the message names the field and the rejected value never appears in it. A request carrying no records is declined the same way, carrying `EMPTY_INPUT`. An instruction payload on either channel is the other case: it **terminates**, and the node publishes no contract |

### TC-03 — Every caller-controlled figure is finite and bounded

| Field | Value |
|---|---|
| **Where** | `test_caller_contract.py::TestFiniteNumbers` |
| **Input** | A parametrized matrix — `"NaN"`, `"Infinity"`, `"-Infinity"`, raw `float("nan")`, raw `float("inf")`, `float("-inf")` and an over-magnitude value — applied to every numeric field in turn |
| **Expected** | Each combination stops the request at the parser, which raises rather than returning a figure. The suite also pins the premise: `float("NaN")` succeeds and `float("NaN") > 100` is False. At the node, and end to end, the same figure produces a declined run — see TC-02 and PB-02 |

### TC-04 — The discount ceiling is the tightest of the four limits

| Field | Value |
|---|---|
| **Where** | `test_plan_nodes.py::TestCeilings` |
| **Input** | Records whose category rule, margin floor, configured cap and requested cap disagree |
| **Expected** | The tightest wins; a product's margin floor overrides a looser category rule; a group is planned to its tightest member; a caller may tighten the ceiling but not widen it |

### TC-05 — The quality gate enforces the plan invariants on the rendered document

| Field | Value |
|---|---|
| **Where** | `test_plan_nodes.py::TestQualityGate` |
| **Input** | A rendered plan with a section removed, a section emptied, a schedule cell raised above the group ceiling, and a monetary amount appended |
| **Expected** | Each is rejected, the plan is withheld, and the message names the group or the invariant. The monetary scan is probed both ways: amounts in every marker form are found, and product codes, percentages and week numbers are not |

### TC-06 / TC-07 — Framework compliance

| Field | Value |
|---|---|
| **Where** | `test_framework_compliance_tc06_tc07.py` |
| **Expected** | A node that omits a trust-level declaration, or that overrides a final security gate, fails at class definition time |

---

## Boundary Cases

### PB-01 — The public path does real work

| Field | Value |
|---|---|
| **Where** | `test_pb_invoke_endpoint.py::TestPublicPathDoesRealWork` |
| **Method** | Real HTTP requests with a Bearer token |
| **Expected** | The plan carries the caller's own product codes; different records produce different plans; a tighter requested ceiling changes the schedule; a wider one does not; the requested horizon changes the number of weeks; the output boundary node appears in the node history |

### PB-02 — The request boundary stops hostile and malformed input end to end

| Field | Value |
|---|---|
| **Where** | `test_pb_invoke_endpoint.py::TestRequestBoundaryRejections` |
| **Method** | Real HTTP requests |
| **Expected** | Non-finite and over-magnitude figures, and a request carrying no records, are **declined**: the run completes, the body is the reason sentence, and no plan is rendered — these are values the caller can correct and send again on the same conversation. A chat-template control token **terminates** with no output, because rewording is not a route past a refusal. A credential shape in the structured parameters is refused with status 400 naming the field but never the value, while ordinary domain text on the same field still passes |

### PB-03 — The adapter screen matches the framework detector exactly

| Field | Value |
|---|---|
| **Where** | `test_pb_invoke_endpoint.py::TestRequestBoundaryRejections` |
| **Method** | A property, not a sample: for each of several contexts, the adapter refuses if and only if the framework's own detector finds something |
| **Expected** | The two sets are identical, so the refusal can name the field without widening or narrowing the block set |

### PB-04 — The output boundary contains a leak driven from caller data

| Field | Value |
|---|---|
| **Where** | `test_pb_invoke_endpoint.py::TestOutputBoundaryContainment` |
| **Method** | A product code that is a payment-card shape — a valid inert label, so it reaches the rendered plan through the ordinary path |
| **Expected** | The envelope carries an error, the code appears nowhere in the response, the substituted notice is truthy (an empty one would activate the envelope's fallback to the un-gated plan), the block is attributed to the output boundary node in the node history, and no traceback or source path is released. The fail-closed direction is probed too: EAN-13, ITF-14 and UPC-A codes still plan |

### PB-05 — Clean-path control

| Field | Value |
|---|---|
| **Where** | `test_pb_invoke_endpoint.py::TestOutputBoundaryContainment` |
| **Expected** | The same request shape still produces its real answer, so a refuse-everything gate cannot pass the suite |

### PB-06 — Node call order and the trust-gate denial path

| Field | Value |
|---|---|
| **Where** | `test_pb_invoke_order.py` |
| **Expected** | Every concrete node runs trust gate, node start, input gate, `execute()`, output gate, node complete — in that order; an under-privileged caller is refused before `execute()` runs |

### PB-07 — Human-review interrupt propagation

| Field | Value |
|---|---|
| **Where** | `test_pb7_hitl_interrupt_propagation.py` |
| **Expected** | Skipped: `config/config.yaml` does not enable human review, so the case is not applicable to this template |

### PB-2 / PB-5 — State safety

| Field | Value |
|---|---|
| **Where** | `test_state_safety.py` |
| **Expected** | The state schema declares no credential-like field name and no unserialisable type |

### PB-4 / PB-03 — Import isolation

| Field | Value |
|---|---|
| **Where** | `test_import_isolation.py`, `test_import_isolation_pb03.py` |
| **Method** | AST scan of every file under `src/` |
| **Expected** | Zero imports of the platform SDK |
