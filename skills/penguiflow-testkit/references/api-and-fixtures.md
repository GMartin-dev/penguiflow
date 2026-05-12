# `penguiflow.testkit` API Reference

All helpers live in `penguiflow.testkit`. Import them directly:
```python
from penguiflow.testkit import (
    run_one,
    assert_node_sequence,
    simulate_error,
    get_recorded_events,
    assert_preserves_message_envelope,
)
```

## `run_one(flow, message, *, registry=None, timeout_s=1.0)`

End-to-end single-trace runner. `registry` and `timeout_s` are **keyword-only**.

### Parameters
- `flow: PenguiFlow` — built via `create(...)`.
- `message: Message` — **must** be a `penguiflow.types.Message`. Anything else raises `TypeError`.
- `registry: ModelRegistry | None` — pass when any node uses `NodePolicy.validate != "none"` (the default).
- `timeout_s: float | None` — wall-clock cap on the test (default `1.0`); `None` disables the timeout.

### Returns
The first `fetch()` result. Payload type depends on what the egress node returns.

### Lifecycle
1. Mark `message.trace_id` as a tracked trace in the global recorder state.
2. `flow.run(registry=registry)`.
3. `await flow.emit(message)` (plain emit, **not** trace-scoped).
4. `result = await asyncio.wait_for(flow.fetch(), timeout_s)` when `timeout_s` is not None, else `await flow.fetch()`.
5. `await flow.stop()` in a `finally`.
6. Event capture for the trace is performed by the recorder middleware attached at flow build time (set up automatically on the first call).

### Raises
- `TypeError` if `message` isn't a `Message`.
- `asyncio.TimeoutError` if the trace doesn't complete within `timeout_s`.
- Whatever the node raises if `emit_errors_to_rookery=False` and a node fails terminally.

## `assert_node_sequence(trace_id, expected_names)`

Compares the deduped sequence of `node_start` events to `expected_names`.

### Parameters
- `trace_id: str` — the same id you emitted with.
- `expected_names: list[str]` — node names in expected order.

### Behavior
- Drops duplicates (e.g., retries of the same node count once).
- Compares as ordered list — order matters.
- Raises `AssertionError` with a diff on mismatch.

### Caveats
- For parallel fan-out, the inter-branch order is non-deterministic. Assert smaller invariants per branch.
- For trace cancel scenarios, expect a shorter sequence than the happy path.

## `simulate_error(node_name, code, *, fail_times=1, result=None, result_factory=None, exception_type=RuntimeError)`

Builds an async callable for use inside `Node(...)`. `fail_times`, `result`, `result_factory`, and `exception_type` are **keyword-only**.

### Parameters
- `node_name: str` — embedded in the simulated exception message for diagnostics.
- `code: FlowErrorCode | str` — embedded in the exception text (a `FlowErrorCode` is stringified to its value).
- `fail_times: int = 1` — first N invocations raise; must be `>= 1`.
- `result: Any | None = None` — value returned on success after the failures are exhausted (default: echo the inbound message).
- `result_factory: Callable[[Any], Awaitable[Any] | Any] | None = None` — async or sync function called with the inbound message to compute the success return. Mutually exclusive with `result`.
- `exception_type: type[Exception] = RuntimeError` — class raised on each simulated failure.

### Returned exception shape
The wrapper raises `exception_type(f"[{code}] simulated failure in {node_name} (attempt {n})")`. The runtime wraps it in `FlowError(code=NODE_EXCEPTION, ...)` if retries exhaust — `code` becomes part of the message, not the runtime error code.

### Common patterns

**Two failures then success (test `max_retries=2`):**
```python
node = Node(
    simulate_error("flaky", "SIM", fail_times=2),
    name="flaky",
    policy=NodePolicy(max_retries=2, backoff_base=0.01),
)
```

**Always fail (test terminal failure):**
```python
node = Node(
    simulate_error("perm", "DOWN", fail_times=999),
    name="perm",
    policy=NodePolicy(max_retries=2),   # 3 attempts total, all fail
)
```

**Custom success return:**
```python
node = Node(
    simulate_error("ok", "ONCE", fail_times=1, result={"recovered": True}),
    name="ok",
    policy=NodePolicy(max_retries=1),
)
```

**Custom exception type:**
```python
class MyError(Exception): ...
node = Node(
    simulate_error("typed", "X", fail_times=1, exception_type=MyError),
    name="typed",
    policy=NodePolicy(max_retries=1),
)
```

## `get_recorded_events(trace_id) -> tuple[FlowEvent, ...]`

Returns an immutable snapshot of `FlowEvent`s recorded for the trace during `run_one`.

### Typical assertions
```python
events = get_recorded_events(trace_id)

# Any retry?
assert any(e.event_type == "node_retry" for e in events)

# Count retries
retry_count = sum(1 for e in events if e.event_type == "node_retry")

# Did a specific node time out?
assert any(
    e.event_type == "node_timeout" and e.node_name == "slow"
    for e in events
)

# Total latency on success
success = next(e for e in events if e.event_type == "node_success" and e.node_name == "n1")
assert success.latency_ms < 100
```

### Caveats
- Global recorder — tests should use distinct `trace_id`s to avoid cross-test bleed.
- Snapshot only — call after `run_one` completes.

## `assert_preserves_message_envelope(node, *, message=None, ctx=None) -> Message`

Asserts a node returns a `Message` with intact `headers` and `trace_id`. **Async**. `message` and `ctx` are keyword-only.

### Parameters
- `node: Node | Callable[[Message, Any], Awaitable[Any]]` — the `Node` or its raw async fn.
- `message: Message | None` — input message (default: a synthetic one).
- `ctx: Any | None` — context object (default: an internal stub whose `emit`/`emit_nowait` no-op).

### Returns
The `Message` the node produced. Returned for follow-up assertions in the caller.

### Behavior
1. `await node(message, ctx)` (or `await node.invoke(...)` if you pass a `Node`).
2. Asserts the return is a `Message`.
3. Asserts `result.headers == message.headers`.
4. Asserts `result.trace_id == message.trace_id`.

Catches accidental:
```python
# bug: drops tenant
return Message(payload=new_payload, headers=Headers(tenant="other"))

# correct: model_copy preserves envelope
return msg.model_copy(update={"payload": new_payload})
```

## Fixture patterns

### `conftest.py` helpers

```python
import pytest
from penguiflow import Headers, Message, ModelRegistry

@pytest.fixture
def headers():
    return Headers(tenant="test")

@pytest.fixture
def message(headers):
    return Message(payload={}, headers=headers)

@pytest.fixture
def registry():
    return ModelRegistry()
```

Tests then take only what they need:
```python
async def test_something(message):
    ...
```

### Async fixtures

```python
@pytest.fixture
async def running_flow():
    flow = create(my_node.to())
    flow.run()
    yield flow
    await flow.stop()
```

Useful when multiple test cases share the same flow setup. Beware cross-test trace bleed in `get_recorded_events` — use distinct `trace_id`s per test.

### Isolated stores

```python
@pytest.fixture
def state_store():
    from penguiflow.state import InMemoryStateStore
    return InMemoryStateStore()
```

Always use in-memory backends in tests. Never connect to a real DB / Redis from unit tests.

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `TypeError` from `run_one` | Payload not wrapped in `Message` | `Message(payload=...)` |
| Cross-test event bleed | Reused `trace_id` | New `Message` per test (new trace_id auto) |
| Hanging test | Node returns `None` and isn't a sink, or sink not connected | Reduce `timeout_s` first to fail fast; then fix topology |
| `node_retry` not in events | `max_retries=0` or node returned without raising | Set `max_retries` ≥ `fail_times` |
| Sequence mismatch under parallel routing | Inter-branch order non-deterministic | Assert per-branch invariants |
