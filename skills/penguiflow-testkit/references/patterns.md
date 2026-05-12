# Testing Patterns

## Pattern: linear flow, happy path

```python
import pytest
from penguiflow import Headers, Message, Node, NodePolicy, create
from penguiflow.testkit import run_one, assert_node_sequence

async def parse(msg: Message, _ctx) -> Message:
    return msg.model_copy(update={"payload": msg.payload.upper()})

@pytest.mark.asyncio
async def test_parse_uppercases():
    node = Node(parse, name="parse", policy=NodePolicy(validate="none"))
    flow = create(node.to())
    msg = Message(payload="hello", headers=Headers(tenant="t"))
    assert await run_one(flow, msg) == "HELLO"
    assert_node_sequence(msg.trace_id, ["parse"])
```

## Pattern: router

```python
from penguiflow import predicate_router

async def handle_a(msg, _ctx): return {"by": "a"}
async def handle_b(msg, _ctx): return {"by": "b"}

router = predicate_router("route", lambda m: "a" if m.payload["k"] == "a" else "b")
a = Node(handle_a, name="a", policy=NodePolicy(validate="none"))
b = Node(handle_b, name="b", policy=NodePolicy(validate="none"))

async def test_router_a():
    flow = create(router.to(a, b), a.to(), b.to())
    msg = Message(payload={"k": "a"}, headers=Headers(tenant="t"))
    result = await run_one(flow, msg)
    assert result == {"by": "a"}
    assert_node_sequence(msg.trace_id, ["route", "a"])

async def test_router_b():
    flow = create(router.to(a, b), a.to(), b.to())
    msg = Message(payload={"k": "b"}, headers=Headers(tenant="t"))
    result = await run_one(flow, msg)
    assert result == {"by": "b"}
    assert_node_sequence(msg.trace_id, ["route", "b"])
```

## Pattern: fan-out + join

```python
from penguiflow import join_k

async def fanout(msg, ctx):
    for v in msg.payload:
        await ctx.emit(msg.model_copy(update={"payload": v}), to=worker)

async def double(msg, _ctx):
    return msg.model_copy(update={"payload": msg.payload * 2})

async def deliver(msg, _ctx):
    return msg.payload

fanout_node = Node(fanout, name="fanout", policy=NodePolicy(validate="none"))
worker = Node(double, name="double", policy=NodePolicy(validate="none"))
join = join_k("join", k=3)
final = Node(deliver, name="final", policy=NodePolicy(validate="none"))

async def test_fanout_join():
    flow = create(
        fanout_node.to(worker),
        worker.to(join),
        join.to(final),
        final.to(),
    )
    msg = Message(payload=[1, 2, 3], headers=Headers(tenant="t"))
    result = await run_one(flow, msg)
    assert sorted(result) == [2, 4, 6]
```

Don't assert sequence for parallel fans — order between branches is non-deterministic.

## Pattern: retry then success

```python
from penguiflow.testkit import simulate_error, get_recorded_events

async def test_retries_then_success():
    flaky = Node(
        simulate_error("flaky", "SIM", fail_times=2),
        name="flaky",
        policy=NodePolicy(max_retries=2, backoff_base=0.01, validate="none"),
    )
    flow = create(flaky.to())
    msg = Message(payload={"ok": True}, headers=Headers(tenant="t"))

    result = await run_one(flow, msg)
    assert result == {"ok": True}

    events = get_recorded_events(msg.trace_id)
    retries = [e for e in events if e.event_type == "node_retry"]
    assert len(retries) == 2
```

Keep backoff tiny (`backoff_base=0.01`) so tests stay fast.

## Pattern: terminal failure to Rookery

```python
from penguiflow import FlowError, FlowErrorCode

async def test_permanent_failure():
    permanent = Node(
        simulate_error("perm", "DOWN", fail_times=10),
        name="perm",
        policy=NodePolicy(max_retries=1, backoff_base=0.01, validate="none"),
    )
    flow = create(permanent.to(), emit_errors_to_rookery=True)
    msg = Message(payload={}, headers=Headers(tenant="t"))

    result = await run_one(flow, msg)
    assert isinstance(result, FlowError)
    assert result.code == FlowErrorCode.NODE_EXCEPTION
    assert result.node_name == "perm"

    events = get_recorded_events(msg.trace_id)
    assert any(e.event_type == "node_failed" for e in events)
```

## Pattern: timeout

```python
import asyncio

async def slow(msg, _ctx):
    await asyncio.sleep(1.0)

async def test_timeout_fires():
    node = Node(
        slow, name="slow",
        policy=NodePolicy(timeout_s=0.05, max_retries=0, validate="none"),
    )
    flow = create(node.to(), emit_errors_to_rookery=True)
    msg = Message(payload={}, headers=Headers(tenant="t"))

    result = await run_one(flow, msg, timeout_s=2.0)
    assert isinstance(result, FlowError)
    assert result.code == FlowErrorCode.NODE_TIMEOUT

    events = get_recorded_events(msg.trace_id)
    assert any(e.event_type == "node_timeout" for e in events)
```

Don't sleep longer than `timeout_s` of `run_one` itself — the test will hit the outer timeout.

## Pattern: envelope preservation

```python
from penguiflow.testkit import assert_preserves_message_envelope

async def upper(msg: Message, _ctx) -> Message:
    return msg.model_copy(update={"payload": msg.payload.upper()})

async def buggy(msg: Message, _ctx) -> Message:
    # bug: drops tenant
    return Message(payload=msg.payload.upper(), headers=Headers(tenant="other"))

async def test_preserves_envelope():
    await assert_preserves_message_envelope(Node(upper, name="upper"))

async def test_buggy_fails_envelope_check():
    import pytest
    with pytest.raises(AssertionError):
        await assert_preserves_message_envelope(Node(buggy, name="buggy"))
```

## Pattern: with `ModelRegistry`

```python
from pydantic import BaseModel
from penguiflow import ModelRegistry

class In(BaseModel): text: str
class Out(BaseModel): upper: str

async def upper(msg: In, _ctx) -> Out:
    return Out(upper=msg.text.upper())

async def test_with_registry():
    registry = ModelRegistry()
    registry.register("upper", In, Out)
    node = Node(upper, name="upper", policy=NodePolicy(validate="both"))
    flow = create(node.to())
    # In payload-only mode, message wraps the typed model
    msg = Message(payload=In(text="hello"), headers=Headers(tenant="t"))
    result = await run_one(flow, msg, registry=registry)
    assert result.upper == "HELLO"
```

## Pattern: stubbing external dependencies

Inject the dependency, don't mock it:

```python
class ApiClient:
    async def fetch(self, q): ...

class FakeClient:
    async def fetch(self, q):
        return {"fake": q}

def make_node(client):
    async def query(msg, _ctx):
        result = await client.fetch(msg.payload)
        return msg.model_copy(update={"payload": result})
    return Node(query, name="query", policy=NodePolicy(validate="none"))

async def test_node_with_fake_client():
    node = make_node(FakeClient())
    flow = create(node.to())
    msg = Message(payload="q", headers=Headers(tenant="t"))
    result = await run_one(flow, msg)
    assert result == {"fake": "q"}
```

Factory functions over module-level singletons. Easier to swap.

## Pattern: assert no retries in happy path

```python
async def test_no_retries_when_node_succeeds():
    node = Node(lambda m, _: m.model_copy(update={"payload": "ok"}),
                name="ok", policy=NodePolicy(max_retries=3, validate="none"))
    flow = create(node.to())
    msg = Message(payload="x", headers=Headers(tenant="t"))
    await run_one(flow, msg)
    events = get_recorded_events(msg.trace_id)
    assert not any(e.event_type == "node_retry" for e in events)
```

Useful regression guard against silently flaky nodes.

## Pattern: testing cancellation

Cancellation isn't directly exercised by `run_one` (it runs to completion or timeout). For cancel tests, drive the flow manually:

```python
async def test_cancel():
    flow = create(slow_node.to())
    flow.run()
    msg = Message(payload={}, headers=Headers(tenant="t"))
    await flow.emit(msg, trace_id=msg.trace_id)
    cancelled = await flow.cancel(msg.trace_id)
    assert cancelled is True
    await flow.stop()
```

See [[penguiflow-core-flows]] `references/cancel-deadlines.md` for the cancellation contract.

## Anti-patterns

- **Mocking the runtime** — use real `create()` flows. The runtime is what you're testing against.
- **Real network calls** — wrap in dependency injection and use fakes.
- **Reusing `trace_id` across tests** — recorder is global; tests pollute each other.
- **Skipping `validate="none"`** in tests where registry isn't configured — runtime fails fast with a clearer error than a mysterious test failure.
- **Long backoffs** — `backoff_base=0.5` ×3 attempts is 3+ seconds per test. Use `backoff_base=0.01` in tests.
