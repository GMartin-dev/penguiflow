"""Live verification of structured final answers (ReactPlanner final_response_model).

Run from the repo root with the project .env present:

    uv run python scripts/live_structured_final_test.py

This is a manual verification harness — NOT part of the test suite. It exercises
the structured-final feature end-to-end against a real LLM endpoint so we can
confirm the prompt wiring, validation, repair, and degradation paths behave with
a live model (not just stubbed clients).
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

from penguiflow.catalog import build_catalog  # noqa: E402
from penguiflow.node import Node  # noqa: E402
from penguiflow.planner import ReactPlanner  # noqa: E402
from penguiflow.planner.models import PlannerEvent  # noqa: E402
from penguiflow.registry import ModelRegistry  # noqa: E402

MODEL = os.environ.get("LLM_MODEL_LIVE", "databricks/databricks-claude-sonnet-4-5")

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{PASS if ok else FAIL}] {name}{(' — ' + detail) if detail else ''}")


# --- A tiny tool so the planner has something to call -----------------------


class Query(BaseModel):
    text: str


class MathResult(BaseModel):
    product: int


async def multiply(payload: Query, ctx: object) -> MathResult:
    """Multiply the two integers found in a 'a*b' style query."""
    del ctx
    a, b = (int(p.strip()) for p in payload.text.replace("?", "").split("*"))
    return MathResult(product=a * b)


def build_planner(**kwargs: object) -> ReactPlanner:
    registry = ModelRegistry()
    registry.register("multiply", Query, MathResult)
    nodes = [Node(multiply, name="multiply")]
    catalog = build_catalog(nodes, registry)
    return ReactPlanner(llm=MODEL, catalog=catalog, max_iters=6, **kwargs)


# --- The structured final answer schema -------------------------------------


class Verdict(BaseModel):
    """Structured final answer."""

    answer: int = Field(description="The numeric result of the computation.")
    method: str = Field(description="One short phrase naming how it was solved.")
    confident: bool = Field(default=True)


async def t1_happy_path() -> None:
    print("\n=== Section 1: happy path — model emits a valid structured payload ===")
    events: list[PlannerEvent] = []
    planner = build_planner(final_response_model=Verdict, event_callback=events.append)
    try:
        result = await planner.run("Use the multiply tool: what is 17*23?")
    except Exception as exc:  # noqa: BLE001
        record("happy path run", False, repr(exc))
        return

    structured = result.payload.get("structured")
    record("run finished answer_complete", result.reason == "answer_complete", f"reason={result.reason}")
    record("raw_answer present", bool(result.payload.get("raw_answer")), f"raw={str(result.payload.get('raw_answer'))[:60]!r}")
    record(
        "structured payload validated",
        isinstance(structured, dict) and structured.get("answer") == 391,
        f"structured={structured}",
    )
    record(
        "structured has all schema keys",
        isinstance(structured, dict) and {"answer", "method", "confident"} <= set(structured),
        f"keys={sorted(structured) if isinstance(structured, dict) else None}",
    )
    validated = [e for e in events if e.event_type == "final_response_structured_validated"]
    record("validated event emitted", len(validated) >= 1, f"count={len(validated)}")


async def t2_default_off() -> None:
    print("\n=== Section 2: default-off — no final_response_model means structured is None ===")
    planner = build_planner()
    try:
        result = await planner.run("Use the multiply tool: what is 6*7?")
    except Exception as exc:  # noqa: BLE001
        record("default-off run", False, repr(exc))
        return
    record("structured is None when unset", result.payload.get("structured") is None,
           f"structured={result.payload.get('structured')}")
    record("raw_answer still present", bool(result.payload.get("raw_answer")))


async def t3_strict_schema_repair() -> None:
    print("\n=== Section 3: strict schema — exercises validation/repair on a live model ===")

    class StrictVerdict(BaseModel):
        answer: int
        # Constrained so a loose model answer is likely to need a repair turn.
        method: str = Field(min_length=3, max_length=40)
        steps: list[str] = Field(description="Ordered reasoning steps, 1-4 of them.")

    events: list[PlannerEvent] = []
    planner = build_planner(
        final_response_model=StrictVerdict,
        final_response_retries=2,
        event_callback=events.append,
    )
    try:
        result = await planner.run("Use the multiply tool: compute 12*12 and explain.")
    except Exception as exc:  # noqa: BLE001
        record("strict schema run", False, repr(exc))
        return

    structured = result.payload.get("structured")
    repaired = [e for e in events if e.event_type == "final_response_structured_repair_attempt"]
    degraded = [e for e in events if e.event_type == "final_response_structured_degraded"]
    # Either it validated outright or after repair; degradation is acceptable but noted.
    ok = (isinstance(structured, dict) and structured.get("answer") == 144) or bool(degraded)
    record(
        "strict schema produced validated-or-degraded result (never raw garbage)",
        ok,
        f"structured={structured} repairs={len(repaired)} degraded={len(degraded)}",
    )
    if degraded:
        record("degradation surfaces a warning", any("validation" in w for w in result.payload.get("warnings", [])),
               f"warnings={result.payload.get('warnings')}")


async def t4_native_finish_tool() -> None:
    print("\n=== Section 4: native tool-calling mode — structured via finish tool, no repair ===")
    registry = ModelRegistry()
    registry.register("multiply", Query, MathResult)
    nodes = [Node(multiply, name="multiply")]
    catalog = build_catalog(nodes, registry)

    events: list[PlannerEvent] = []
    planner = ReactPlanner(
        llm=MODEL,
        catalog=catalog,
        max_iters=6,
        use_native_llm=True,         # native adapter exposes complete_with_tools
        tool_call_mode="native",     # provider-native function calling
        final_response_model=Verdict,
        event_callback=events.append,
    )
    try:
        result = await planner.run("Use the multiply tool: what is 17*23?")
    except Exception as exc:  # noqa: BLE001
        record("native finish run", False, repr(exc))
        return

    structured = result.payload.get("structured")
    repairs = [e for e in events if e.event_type == "final_response_structured_repair_attempt"]
    validated = [e for e in events if e.event_type == "final_response_structured_validated"]
    record(
        "native structured payload validated",
        isinstance(structured, dict) and structured.get("answer") == 391,
        f"structured={structured}",
    )
    record(
        "native finish needed NO repair turn (validated on first pass)",
        bool(validated) and not validated[0].extra.get("repaired", False) and not repairs,
        f"repaired={validated[0].extra.get('repaired') if validated else 'n/a'} repairs={len(repairs)}",
    )


async def main() -> None:
    print(f"Live structured-final verification against: {MODEL}")
    await t1_happy_path()
    await t2_default_off()
    await t3_strict_schema_repair()
    await t4_native_finish_tool()

    print("\n=== Summary ===")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print(f"  [{PASS if ok else FAIL}] {name}")
    print(f"\n{passed}/{len(results)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
