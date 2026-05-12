# `StreamChunk` and `ctx.emit_chunk`

## `StreamChunk` schema

```python
StreamChunk(
    stream_id: str,          # defaults to parent's trace_id
    seq: int,                # monotonically increasing per stream_id
    text: str,               # the chunk content
    done: bool = False,      # True marks the terminator chunk
    meta: dict = {},         # JSON-friendly side data
)
```

Lives in `penguiflow.types`.

### Field semantics

- **`stream_id`** — Correlates chunks belonging to the same logical stream. Default is the parent message's `trace_id`. Override only when one trace has multiple concurrent streams (e.g., "answer" and "thinking"); pick distinct stable names.
- **`seq`** — Auto-assigned by `emit_chunk` if you don't supply it. Strictly increasing per `stream_id`. Resets after a chunk with `done=True`.
- **`text`** — The content. For non-text streams, base64-encode and put the MIME in `meta`.
- **`done`** — `True` exactly once per stream. After this, internal seq tracking resets and the consumer should release the stream.
- **`meta`** — JSON-friendly auxiliary data. Common keys: `mime_type`, `provider`, `tokens`, `latency_ms`.

## `ctx.emit_chunk(...)`

```python
await ctx.emit_chunk(
    parent: Message,            # required
    text: str,
    done: bool = False,
    meta: dict | None = None,
    to: Node | Sequence[Node] | None = None,
    stream_id: str | None = None,    # rare; defaults to parent.trace_id
    seq: int | None = None,          # rare; auto-assigned
)
```

### Why `parent` is required
- Inherits `trace_id` (for cancellation, fetch, fan-out join).
- Inherits `Headers` (tenant, topic, priority).
- Inherits `deadline_s` (chunks expire with their parent message).
- Inherits relevant `meta` (your custom metadata propagates).

Don't construct chunks manually with `ctx.emit(Message(payload=StreamChunk(...)))` — you'll bypass the auto-assign of `seq` and lose envelope inheritance.

### `to` parameter

Same semantics as `ctx.emit(..., to=...)`. Pass a `Node` or list of `Node`s to fan out to specific sinks. If omitted, chunks route to all successors of the current node.

### Common patterns

#### Single text stream
```python
async def compose(msg, ctx):
    for token in stream_tokens(msg.payload.prompt):
        await ctx.emit_chunk(parent=msg, text=token, to=chunk_sink)
    await ctx.emit_chunk(parent=msg, text="", done=True, to=chunk_sink)
```

#### Two concurrent streams: answer + thinking
```python
async def compose(msg, ctx):
    async for event in llm.stream(...):
        if event.kind == "thinking":
            await ctx.emit_chunk(
                parent=msg,
                text=event.text,
                stream_id="thinking",
                to=thinking_sink,
            )
        else:
            await ctx.emit_chunk(
                parent=msg,
                text=event.text,
                stream_id="answer",
                to=answer_sink,
            )
    await ctx.emit_chunk(parent=msg, text="", done=True, stream_id="thinking", to=thinking_sink)
    await ctx.emit_chunk(parent=msg, text="", done=True, stream_id="answer", to=answer_sink)
```

#### Status updates alongside content
```python
async def compose(msg, ctx):
    await ctx.emit_chunk(parent=msg, text="searching…", meta={"kind": "status"}, to=chunk_sink)
    results = await search(msg.payload.query)
    await ctx.emit_chunk(parent=msg, text="synthesizing…", meta={"kind": "status"}, to=chunk_sink)
    async for token in synthesize(results):
        await ctx.emit_chunk(parent=msg, text=token, meta={"kind": "content"}, to=chunk_sink)
    await ctx.emit_chunk(parent=msg, text="", done=True, to=chunk_sink)
```

Consumers filter by `meta["kind"]` to render status separately from content.

## Ordering guarantees

- **Within one `stream_id`**: chunks are FIFO. The runtime preserves order through edges.
- **Across `stream_id`s**: no ordering guarantee. Two streams can interleave arbitrarily.
- **Across traces**: each trace is independent.

For UIs that need strict global order across status + content + tool calls, encode order in `meta` (e.g., a global counter) and reconcile client-side.

## Multi-producer hazards

Two producers emitting to the **same** `stream_id` is a bug. `seq` will be inconsistent across producers and consumers can't reassemble. Either:
- Split into distinct `stream_id`s (one per producer), or
- Funnel through a single producer node that serializes the input.

## `done=True` semantics

A chunk with `done=True`:
- Closes the stream.
- Resets the runtime's `seq` counter for that `stream_id`.
- Tells the consumer "you can now release any stream-level state."

Emit it exactly once. Emitting twice corrupts the consumer's state machine. Forgetting to emit it leaves the consumer waiting forever (typically a UI spinner that never stops).

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| No chunks | Parent is not a `Message` envelope | Switch the flow to envelope style |
| Out-of-order chunks | Multiple producers, same `stream_id` | One producer per stream, or distinct ids |
| Stream never closes | Forgot `done=True` | Always emit a terminator |
| `seq` discontinuity | Manual `seq` clashes with auto-assign | Don't supply `seq` manually |
| Chunk leaks across traces | Reusing `trace_id` | Generate fresh `trace_id` per request |
| Backpressure stalls | Sink slow, chunk edge bounded | Raise `queue_maxsize` on the chunk edge, or make sink faster |

## Observability

Chunk emission shows up as `node_*` events on the **producer** node. The runtime doesn't emit a special "stream event" — chunks are just messages. To track stream-specific metrics, instrument the sink:

```python
async def chunk_sink(msg, _ctx):
    chunk: StreamChunk = msg.payload
    metrics.histogram("stream.chunk_chars", len(chunk.text), tags=[f"stream:{chunk.stream_id}"])
    if chunk.seq == 0:
        metrics.histogram("stream.time_to_first_chunk_ms",
                          (time.monotonic() - producer_start_ms[msg.trace_id]) * 1000)
    if chunk.done:
        metrics.increment("stream.completed", tags=[f"stream:{chunk.stream_id}"])
```

Track:
- Time-to-first-chunk (TTFC) p50/p95/p99.
- Chunks per second per stream.
- Stream completion rate (terminated vs abandoned).
- Chunk size distribution.
