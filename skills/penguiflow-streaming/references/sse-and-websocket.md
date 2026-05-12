# SSE, WebSocket, and Programmatic Iteration

`penguiflow.streaming` ships four helpers for shipping chunks to clients.

## `format_sse_event(chunk, *, event_name=None, retry_ms=None) -> str`

Renders a `StreamChunk` as an SSE event string ready to send over `text/event-stream`.

```python
from penguiflow.streaming import format_sse_event

frame = format_sse_event(chunk)
# event: chunk        (or "done" if chunk.done is True)
# id: <seq>
# data: <text>
# data: <meta json>   (when meta is non-empty)
#
```

### Parameters
- `event_name` — Override the default `"chunk"` / `"done"`. Use when your UI listens for multiple custom event types.
- `retry_ms` — When supplied, appends `retry: <ms>` to instruct the client's EventSource on reconnect backoff.

### FastAPI / Starlette SSE recipe

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from penguiflow.streaming import format_sse_event, stream_flow

app = FastAPI()

@app.get("/stream")
async def stream(request_id: str):
    msg = Message(payload={"q": request_id}, headers=Headers(tenant="acme"))
    flow.run()  # or running already

    async def gen():
        async for chunk in stream_flow(flow, msg, to=compose_node, include_final=False):
            yield format_sse_event(chunk)

    return StreamingResponse(gen(), media_type="text/event-stream")
```

The client uses `new EventSource("/stream?request_id=...")` and listens for `"chunk"` / `"done"` events.

### Browser side

```javascript
const es = new EventSource("/stream?request_id=q1");
es.addEventListener("chunk", (e) => append(e.data));
es.addEventListener("done", () => es.close());
```

## `chunk_to_ws_json(chunk, *, extra=None) -> str`

Serializes a `StreamChunk` as JSON for WebSocket transports.

```python
from penguiflow.streaming import chunk_to_ws_json

payload = chunk_to_ws_json(chunk, extra={"client_seq": 42})
# {"stream_id": "...", "seq": 0, "text": "hello ", "done": false, "meta": {}, "client_seq": 42}
```

`extra` is merged into the JSON object. Use for client-correlation ids or routing hints your WS protocol expects.

### FastAPI WebSocket recipe

```python
from fastapi import FastAPI, WebSocket
from penguiflow.streaming import chunk_to_ws_json, stream_flow

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    request = await websocket.receive_json()
    msg = Message(payload=request, headers=Headers(tenant=request["tenant"]))

    async for chunk in stream_flow(flow, msg, to=compose_node, include_final=False):
        await websocket.send_text(chunk_to_ws_json(chunk))

    await websocket.close()
```

## `stream_flow(flow, parent_msg, *, to=None, timeout=None, include_final=False) -> AsyncIterator`

Async iterator over chunks for a single run.

```python
async for chunk in stream_flow(flow, parent_msg, to=compose_node, timeout=30.0):
    handle(chunk)
```

### Behavior
- Calls `await flow.emit(parent_msg, to=to)` internally — don't pre-emit.
- Yields each `StreamChunk` as it arrives.
- When a chunk with `done=True` arrives:
  - `include_final=False` (default): stops.
  - `include_final=True`: continues, yields the next non-chunk payload (typically `FinalAnswer`), then stops.
- `timeout` applies per-fetch (not total). Raises `asyncio.TimeoutError` if no chunk arrives in `timeout` seconds.

### When to use
- Tests that need to assert on the chunk sequence.
- HTTP handlers that need both chunks and the final answer in one iterator.
- Simple bridges where you don't want a separate sink node.

### When NOT to use
- Multi-trace concurrent serving. `stream_flow` fetches from the flow's egress; if multiple traces are running concurrently, you may pick up chunks from a different trace. Use trace-scoped `flow.fetch(trace_id=...)` patterns instead.

## `emit_stream_events(source, ctx, parent_msg, *, adapter=None, to=None, final_meta=None)`

Bridges an async iterable of provider events (e.g., an LLM SDK's streaming response) into `StreamChunk` emissions.

```python
from penguiflow.streaming import emit_stream_events

async def compose(msg, ctx):
    response = llm_client.stream(prompt=msg.payload.prompt)
    await emit_stream_events(
        response,
        ctx,
        parent_msg=msg,
        adapter=lambda event: (event.delta, event.is_last, {"provider": "openai"}),
        to=chunk_sink,
        final_meta={"total_tokens": response.usage.total_tokens},
    )
```

### `adapter`
Callable `event -> (text: str, done: bool, meta: dict)`. The default treats events as strings (`str(event), False, {}`).

For typed providers, write a real adapter:

```python
def openai_adapter(event):
    if event.choices[0].finish_reason == "stop":
        return ("", True, {})
    return (event.choices[0].delta.content or "", False, {})
```

### `final_meta`
Attached to the auto-emitted terminator chunk if the source didn't already emit one with `done=True`. Use for usage statistics, latencies, etc.

### Termination guarantee
`emit_stream_events` ensures a chunk with `done=True` is always emitted at the end — either from the source itself or as a synthesized terminator. Consumers can rely on the close signal.

## Choosing among the helpers

| Need | Use |
|---|---|
| SSE to browser | `format_sse_event` + `stream_flow` (or sink node + manual SSE) |
| WebSocket to browser | `chunk_to_ws_json` + `stream_flow` |
| Test the chunk sequence | `stream_flow(include_final=True)` |
| Wrap an LLM SDK's stream | `emit_stream_events` |
| Custom transport (gRPC stream, MQTT, ...) | Custom sink node — serialize `chunk` yourself |

## Sink-node alternative

For high-concurrency servers, a dedicated sink node decoupled from the HTTP/WS handler scales better than `stream_flow`:

```
compose --(chunk)--> chunk_sink --(forward)--> per-connection queue --(consume)--> HTTP handler
```

The sink looks up the connection by `trace_id` (the chunk's `stream_id`) and pushes the chunk into a per-connection `asyncio.Queue`. The HTTP/WS handler drains its queue. This pattern avoids tying the flow's lifecycle to a single HTTP request.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| SSE shows `data:` but no `event:` | `format_sse_event` called with explicit `event_name=""` | Pass a non-empty name or omit |
| `EventSource` reconnects infinitely | Server closes without `done` event | Always emit a chunk with `done=True` before closing |
| WS client gets the same chunk twice | Producer emits to two `to=...` sinks both forwarding to WS | Pick one sink path |
| `stream_flow` picks up another trace's chunks | Multi-trace concurrent serving | Use trace-scoped fetch in a custom sink |
| Adapter throws on unexpected event | Provider added new event type | Default to `("", False, {})` for unknown events |
