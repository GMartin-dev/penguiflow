"""Live verification of temperature handling + rate-limit fallback (Databricks).

Run from the repo root with the project .env present:

    uv run python scripts/live_databricks_test.py

This script is a manual verification harness — it is NOT part of the test suite
and is not committed as a permanent artifact.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time

from dotenv import load_dotenv

load_dotenv()

from penguiflow.llm import ModelFallbackConfig, create_native_adapter  # noqa: E402
from penguiflow.llm.profiles import get_profile, register_profile  # noqa: E402
from penguiflow.llm.protocol import NativeLLMAdapter  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

M1 = os.environ["LLM_MODEL1"]  # databricks/databricks-claude-opus-4-7
M2 = os.environ["LLM_MODEL2"]  # databricks/databricks-gpt-5-5
M3 = os.environ["LLM_MODEL3"]  # databricks/databricks-gpt-5-4-mini

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{PASS if ok else FAIL}] {name}{(' — ' + detail) if detail else ''}")


async def section_1_basic() -> None:
    print("\n=== Section 1: basic completion, temperature omitted by default ===")
    for model in (M1, M2, M3):
        try:
            adapter = NativeLLMAdapter(model, timeout_s=90)
            content, cost = await adapter.complete(
                messages=[{"role": "user", "content": "Reply with exactly: pong"}]
            )
            record(f"{model} basic call", bool(content.strip()), f"reply={content.strip()[:40]!r}")
        except Exception as exc:  # noqa: BLE001
            record(f"{model} basic call", False, repr(exc))


async def section_2_explicit_temp_dropped() -> None:
    print("\n=== Section 2: explicit temperature dropped for unsupported model ===")
    # databricks-claude-opus-4-7 has supports_temperature=False; sending an
    # explicit temperature must be dropped, so the call still succeeds.
    try:
        adapter = NativeLLMAdapter(M1, temperature=0.0, timeout_s=90)
        content, _ = await adapter.complete(
            messages=[{"role": "user", "content": "Reply with exactly: ok"}]
        )
        record(f"{M1} explicit temp=0.0 dropped", bool(content.strip()), f"reply={content.strip()[:40]!r}")
    except Exception as exc:  # noqa: BLE001
        record(f"{M1} explicit temp=0.0 dropped", False, repr(exc))


async def section_3_runtime_recovery() -> None:
    print("\n=== Section 3: runtime 400 recovery (force profile, send temperature) ===")
    model_id = "databricks-claude-opus-4-7"
    original = get_profile(model_id)
    # Force the profile to claim temperature is supported, so the provider
    # actually sends it and Databricks returns a temperature-400.
    register_profile(model_id, dataclasses.replace(original, supports_temperature=True))
    try:
        adapter = NativeLLMAdapter(M1, temperature=0.5, timeout_s=90)
        content, _ = await adapter.complete(
            messages=[{"role": "user", "content": "Reply with exactly: recovered"}]
        )
        recovered = adapter._provider.temperature_unsupported
        record(
            f"{M1} temperature-400 auto-recovery",
            bool(content.strip()) and recovered,
            f"recovered_flag={recovered} reply={content.strip()[:40]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        record(f"{M1} temperature-400 auto-recovery", False, repr(exc))
    finally:
        register_profile(model_id, original)


async def section_4_reasoning() -> None:
    print("\n=== Section 4: reasoning works for GPT-5 models (reasoning_effort engaged) ===")
    # Databricks GPT-5 endpoints accept `reasoning_effort` and reason internally;
    # they do not surface a separate reasoning-token stream. The meaningful check
    # is: the call succeeds with reasoning_effort set and the answer is correct.
    for model in (M2, M3):
        try:
            adapter = create_native_adapter(model, reasoning_effort="medium", timeout_s=120)
            content, _ = await adapter.complete(
                messages=[
                    {"role": "user", "content": "What is 23 * 17? Reason it out, then state the number."}
                ],
            )
            ok = "391" in content
            record(f"{model} reasoning (reasoning_effort=medium)", ok, f"answer_has_391={ok}")
        except Exception as exc:  # noqa: BLE001
            record(f"{model} reasoning (reasoning_effort=medium)", False, repr(exc))


async def section_5_fallback() -> None:
    print("\n=== Section 5: rate-limit fallback (bomb model 1 in parallel) ===")
    config = ModelFallbackConfig(models=[M1, M2, M3], cooldown_s=45.0, max_wait_s=25.0)
    client = create_native_adapter(M1, fallback=config, timeout_s=150)

    n = 100
    prompt = [
        {
            "role": "user",
            "content": (
                "Write an extremely long, detailed story — at least 2500 words — "
                "about a penguin explorer crossing Antarctica. Include rich "
                "description, dialogue, and multiple chapters. Do not stop early."
            ),
        }
    ]

    async def one(i: int) -> tuple[str, object]:
        try:
            content, _ = await client.complete(messages=prompt)
            return ("ok", len(content))
        except Exception as exc:  # noqa: BLE001
            return ("err", repr(exc))

    started = time.monotonic()
    outcomes = await asyncio.gather(*[one(i) for i in range(n)])
    elapsed = time.monotonic() - started

    oks = [o for o in outcomes if o[0] == "ok"]
    errs = [o for o in outcomes if o[0] == "err"]
    cooled = dict(client._store._until)  # type: ignore[attr-defined]
    print(f"  {len(oks)}/{n} succeeded, {len(errs)} failed in {elapsed:.1f}s")
    if errs:
        print(f"  sample error: {errs[0][1]}")
    print(f"  cooldown entries: {list(cooled.keys())}")

    triggered = len(cooled) > 0
    record(
        "fallback: all requests completed",
        len(oks) == n,
        f"{len(oks)}/{n} ok",
    )
    record(
        "fallback: rate limit triggered a model cooldown",
        triggered,
        f"cooled={list(cooled.keys())}" if triggered else "no 429 observed — increase load",
    )

    # After bombing, model 1 is likely cooled down — a fresh call should route
    # to a GPT-5 fallback and still produce a correct (reasoned) answer.
    if triggered:
        try:
            content, _ = await client.complete(
                messages=[{"role": "user", "content": "What is 8 * 12? Reason it out, then state the number."}],
            )
            record(
                "fallback: routed model still answers correctly",
                "96" in content,
                f"answer_has_96={'96' in content}",
            )
        except Exception as exc:  # noqa: BLE001
            record("fallback: routed model still answers correctly", False, repr(exc))


async def main() -> None:
    print(f"Models:\n  M1={M1}\n  M2={M2}\n  M3={M3}")
    await section_1_basic()
    await section_2_explicit_temp_dropped()
    await section_3_runtime_recovery()
    await section_4_reasoning()
    await section_5_fallback()

    print("\n=== Summary ===")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print(f"  [{PASS if ok else FAIL}] {name}")
    print(f"\n{passed}/{len(results)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
