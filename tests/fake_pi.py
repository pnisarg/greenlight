#!/usr/bin/env python3
"""A fake `pi` for e2e tests.

Mimics `pi -p --mode json`: emits a single agent_end JSONL event whose final
assistant message is driven by the FAKE_PI_MODE env var:
  * review  -> clean findings (empty array)
  * default -> a short intent/summary string
Read-only review invocations include "--tools" before the prompt; we just look
at the prompt text to decide what to emit.
"""
import json
import sys


def emit(text: str) -> None:
    ev = {
        "type": "agent_end",
        "messages": [{"role": "assistant", "content": [{"type": "text", "text": text}]}],
    }
    print(json.dumps(ev))


def main() -> int:
    argv = sys.argv[1:]
    prompt = argv[-1] if argv else ""
    if "return an empty findings array" in prompt or "review" in prompt.lower() and "findings" in prompt.lower():
        emit('```json\n{"findings": [], "summary": "clean"}\n```')
    else:
        emit("Add a greeting helper; intent captured for tests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
