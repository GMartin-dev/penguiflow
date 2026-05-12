# Status, Progressive UI, and Backpressure

## The three-channel pattern

Most production streaming UIs need three logical channels:

1. **Status** — "searching…", "synthesizing…", "calling tool foo". Updates the UI's progress indicator.
2. **Content** — the actual answer tokens.
3. **Final** — the canonical result for `fetch()`, often a `FinalAnswer`.

Implement them in one of two ways:

### Option A: one stream, typed by `meta`

```python
async def compose(msg, ctx):
    await ctx.emit_chunk(parent=msg, text="searching…", meta={"kind": "status"}, to=chunk_sink)
    results = await search(msg.payload.query)

    await ctx.emit_chunk(parent=msg, text="synthesizing…", meta={"kind": "status"}, to=chunk_sink)
    async for token in synthesize(results):
        await ctx.emit_chunk(parent=msg, text=token, meta={"kind": "content"}, to=chunk_sink)
    await ctx.emit_chunk(parent=msg, text="", done=True, to=chunk_sink)

    await ctx.emit(msg.model_copy(update={"payload": FinalAnswer(text=...)}), to=final_node)
```

Pros: one consumer, simple wiring.
Cons: consumer must inspect `meta` for every chunk.

### Option B: distinct `stream_id`s

```python
await ctx.emit_chunk(parent=msg, text="searching…", stream_id="status", to=status_sink)
await ctx.emit_chunk(parent=msg, text=token,        stream_id="answer", to=answer_sink)
```

Pros: clean separation; consumers filter by `stream_id`.
Cons: more wiring; need to emit `done=True` to each stream.

Pick Option A for chat UIs (single feed). Option B for multi-pane UIs ("thinking", "answer", "citations" panels).

## Time-to-first-chunk (TTFC) tuning

User-perceived latency is dominated by TTFC, not total response time. Optimizations:

### Emit status early
The first chunk a user sees should be a status update, not the first content token. Status doesn't require the LLM to start — emit it immediately on tool entry:

```python
async def compose(msg, ctx):
    await ctx.emit_chunk(parent=msg, text="thinking…", meta={"kind": "status"}, to=chunk_sink)
    # ... do work
```

### Pre-emit before slow tools
Before any tool call >500ms, emit a status chunk so the UI doesn't appear frozen.

### Minimize queue depth on the chunk edge
Default `queue_maxsize=64` is fine for most chunk flows. Higher values delay backpressure signals; lower values cause stalls under burst.

### Use a fast LLM client connection
LLM client connection setup (TLS handshake, auth) often dominates TTFC. Reuse connections; pre-warm clients.

## Backpressure on chunk edges

Chunks are normal messages — they go through bounded `asyncio.Queue`s. If the sink is slower than the producer, the producer's `emit_chunk` awaits.

This is a feature, not a bug:
- Prevents memory blowup when a slow client drags the producer.
- Forces explicit sizing.

But if the sink is slow because of network latency to the client (which is normal for streaming), backpressure manifests as choppy delivery. Strategies:

### Increase chunk-edge `queue_maxsize`
For the edge from compose to chunk_sink:

```python
flow = create(
    compose.to(chunk_sink, final),
    chunk_sink.to(),
    final.to(),
    queue_maxsize=256,    # higher for chunk-heavy flows
)
```

256-1024 is reasonable for chunk edges. Don't disable backpressure (`<=0`) — you'll OOM under pathological producers.

### Coalesce chunks
If the producer is much faster than the network, coalesce small chunks into larger ones:

```python
buffer = []
async for token in stream:
    buffer.append(token)
    if len("".join(buffer)) >= 32:    # flush at 32 chars
        await ctx.emit_chunk(parent=msg, text="".join(buffer), to=chunk_sink)
        buffer = []
if buffer:
    await ctx.emit_chunk(parent=msg, text="".join(buffer), to=chunk_sink)
await ctx.emit_chunk(parent=msg, text="", done=True, to=chunk_sink)
```

Trade-off: TTFC suffers slightly; throughput improves.

### Per-connection queue + sink decoupling
The sink-node decoupling pattern (covered in `sse-and-websocket.md`) lets the flow keep running even when a single client drains slowly.

## Status taxonomy

For consistent UX, settle on a status vocabulary:

| `meta["kind"]` | When |
|---|---|
| `status` | Progress hints ("searching…", "synthesizing…") |
| `content` | The actual answer |
| `thinking` | Reasoning trace (if exposed to user) |
| `tool_call` | "Calling foo with X" (often surfaced separately) |
| `citation` | Source references |

Document the vocabulary so frontend and backend agree. If you change it, version your stream protocol.

## Cancellation and streaming

When a client disconnects mid-stream:
1. Detect disconnect in the sink (FastAPI raises `ClientDisconnect`, WebSocket gets a `WebSocketDisconnect`).
2. Call `await flow.cancel(trace_id)` to stop the producer.
3. Producer notices cancellation on the next cooperative checkpoint (await point) and stops.

Don't rely on the producer noticing instantly — keep awaitable boundaries in the producer body (yield to the loop between heavy operations).

See [[penguiflow-core-flows]] `references/cancel-deadlines.md` for cancellation semantics.

## Error handling mid-stream

If the producer raises mid-stream, the sink may have received partial chunks. Options:

### Option 1: emit an error chunk
```python
try:
    async for token in stream:
        await ctx.emit_chunk(parent=msg, text=token, to=chunk_sink)
except Exception as exc:
    await ctx.emit_chunk(
        parent=msg,
        text=f"error: {type(exc).__name__}",
        done=True,
        meta={"kind": "error", "code": exc.__class__.__name__},
        to=chunk_sink,
    )
    raise   # let the runtime convert to FlowError as usual
```

The client sees a final chunk with `meta.kind == "error"` and can render appropriately.

### Option 2: rely on `FlowError` + `emit_errors_to_rookery`
The chunk stream may end without `done=True`; the caller's `fetch(trace_id=...)` returns a `FlowError` and the client renders the error from there.

Pick Option 1 for chat UIs (smoother UX). Option 2 for back-office tools.

## Observability for streams

Track:
- **TTFC** per trace (p50/p95/p99) — by route, by tenant, by user.
- **Chunk rate** (chunks/sec) — bottleneck indicator.
- **Stream duration** (start → `done`) — by route.
- **Abandon rate** (streams without `done=True`) — disconnects, errors.
- **Backpressure events** (`emit_chunk` await time) — capacity indicator.

Wire metrics in the chunk sink (it sees every chunk regardless of fan-out). Don't add metrics inside the producer — it'll measure your code, not the user experience.

## Anti-patterns

- **One chunk per token, naive** — fine for small LLMs but inefficient over WAN. Coalesce.
- **`done=True` only sometimes** — clients will hang. Emit it always, even on error.
- **Mixing trace ids** — chunks for trace A land in trace B's stream because the producer reused `trace_id`. Always derive `trace_id` from the inbound request.
- **Sink calls await long external I/O** — backpressure across the whole flow. Buffer in the sink, ack quickly.
- **Disabling backpressure** for chunk edges. Memory grows linearly with producer speed; one slow client takes down the worker.
