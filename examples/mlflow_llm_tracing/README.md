# MLflow LLM Tracing

Runnable ReactPlanner example for the Phase 4 tracing and pricing path.

By default it uses a scripted LLM client so the example is deterministic and
does not require credentials:

```bash
PENGUIFLOW_LLM_TRACING=log uv run python examples/mlflow_llm_tracing/flow.py
```

To write MLflow tracing spans locally:

```bash
PENGUIFLOW_LLM_TRACING=mlflow uv run --with mlflow python examples/mlflow_llm_tracing/flow.py
```

For a live Databricks run, put `DATABRICKS_API_BASE` and
`DATABRICKS_API_KEY` in the repository `.env` and run:

```bash
PENGUIFLOW_LLM_TRACING=mlflow PENGUIFLOW_PHASE4_LIVE=1 uv run --with mlflow python examples/mlflow_llm_tracing/flow.py
```

The output includes the model's base price from `genai-prices`, the run cost,
and a private-rate override check proving `register_pricing()` still wins.

## Integration Test

```bash
uv run pytest tests/test_example_mlflow_llm_tracing.py
```
