#!/usr/bin/env python3
"""Offline structural test for GPT-5.6 Responses automatic prompt caching.

It never contacts a model gateway and never interacts with WeChat.  The test uses
root prompt.txt as the seed snapshot, splits its group history into immutable
per-record items, simulates three incremental turns, and verifies that each next
turn preserves the previous automatic-cache prefix byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import httpx
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm.prompt.prompt_builder import _group_context_message_items, group_message_identity
from llm.provider.deepseek_provider import _responses_instructions, _to_responses_input


ROLE_SPLIT = re.compile(r"(?m)^\[(\d+)] role=(system|user|assistant|tool)\n")
HISTORY_RECORD = re.compile(
    r"(?m)^\[id=(?P<id>[^\]]+)]\[(?P<nickname>[^\]]*)]\[(?P<role>[^\]]*)]"
    r"(?P<prefix>\[<prefix>])?(?: batch_index=(?P<batch>\d+))?: "
)


def parse_prompt_snapshot(path: Path):
    raw = path.read_text(encoding="utf-8")
    sections = []
    matches = list(ROLE_SPLIT.finditer(raw))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        sections.append((int(match.group(1)), match.group(2), raw[match.end():end].strip()))
    system = next((content for _, role, content in sections if role == "system"), "")
    users = [content for _, role, content in sections if role == "user"]
    if not system or len(users) < 2:
        raise RuntimeError("prompt.txt must contain one system and at least two user sections")
    return system, users[0], "\n\n".join(users[1:])


def parse_history(history_text: str):
    markers = list(HISTORY_RECORD.finditer(history_text))
    if not markers:
        raise RuntimeError("No group-history records found in prompt.txt")
    records = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(history_text)
        content = history_text[marker.end():end].rstrip()
        item = {
            "message_id": marker.group("id"),
            "nickname": marker.group("nickname"),
            "role": marker.group("role"),
            "content": content,
            "is_bot": marker.group("role") == "assistant",
            "prefix_used": bool(marker.group("prefix")),
        }
        if marker.group("batch") is not None:
            item["batch_index"] = int(marker.group("batch"))
        records.append(item)
    return records


def serialized_prefix(instructions: str, response_input: list[dict], item_count: int):
    """Canonical stable automatic-cache prefix, excluding the dynamic tail."""
    if item_count < 1 or len(response_input) < item_count:
        raise AssertionError("Invalid automatic-cache prefix item count")
    prefix = {"instructions": instructions, "input": response_input[:item_count]}
    encoded = json.dumps(prefix, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16], len(encoded)


def to_turn(system: str, records: list[dict], checkpoint_id: str, runtime: str):
    messages = [{"role": "system", "content": system}]
    messages.extend(_group_context_message_items(records, checkpoint_id))
    messages.append({"role": "user", "content": runtime})
    response_input = _to_responses_input(messages)
    return messages, response_input


def verify_sdk_keeps_cache_key(response_input: list[dict], instructions: str):
    captured = []

    def handler(request: httpx.Request):
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "gpt-5.6-luna",
                "output": [],
                "parallel_tool_calls": True,
                "tool_choice": "auto",
                "tools": [],
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenAI(api_key="test", base_url="https://cache-test.invalid/v1", http_client=http_client)
    try:
        client.responses.create(
            model="gpt-5.6-luna",
            instructions=instructions,
            input=response_input,
            prompt_cache_key="wechat:test:cache-layout",
        )
    finally:
        client.close()
    if len(captured) != 1:
        raise AssertionError("Mock transport did not capture exactly one request")
    body = captured[0]
    has_marker = any(
        isinstance(item.get("content"), list)
        and any(block.get("prompt_cache_breakpoint") for block in item["content"] if isinstance(block, dict))
        for item in body.get("input", [])
        if isinstance(item, dict)
    )
    if has_marker:
        raise AssertionError("Automatic-cache default unexpectedly sent an explicit breakpoint")
    return bool(body.get("prompt_cache_key"))


def main():
    system, history_text, runtime = parse_prompt_snapshot(ROOT / "prompt.txt")
    history = parse_history(history_text)
    checkpoint_id = group_message_identity(history[-1])

    # R1 writes a cache at the current history tail. R2/R3 retain that exact
    # record and append later messages after it, which is the important part.
    r1_messages, r1_input = to_turn(system, history, checkpoint_id, runtime)
    history_r2 = history + [{
        "message_id": "cache-test-r2",
        "nickname": "cache-test-user",
        "role": "user",
        "content": "incremental cache layout test: turn two",
        "is_bot": False,
    }]
    r2_messages, r2_input = to_turn(system, history_r2, checkpoint_id, runtime + "\nturn=2")
    history_r3 = history_r2 + [{
        "message_id": "cache-test-r3",
        "nickname": "cache-test-user",
        "role": "user",
        "content": "incremental cache layout test: turn three",
        "is_bot": False,
    }]
    r3_messages, r3_input = to_turn(system, history_r3, checkpoint_id, runtime + "\nturn=3")

    instructions = _responses_instructions(r1_messages)
    # R1 has immutable history items followed by a dynamic runtime item.  R2
    # and R3 retain those original items exactly, then append new messages.
    stable_item_count = len(r1_input) - 1
    prefixes = [
        serialized_prefix(instructions, response_input, stable_item_count)
        for response_input in (r1_input, r2_input, r3_input)
    ]
    if len({item[0] for item in prefixes}) != 1:
        raise AssertionError("Incremental turns rewrote the automatic-cache prefix")
    if any(
        isinstance(block, dict) and block.get("prompt_cache_breakpoint")
        for response_input in (r1_input, r2_input, r3_input)
        for item in response_input
        for block in (item.get("content") if isinstance(item.get("content"), list) else [])
    ):
        raise AssertionError("Automatic-cache test unexpectedly emitted an explicit breakpoint")
    key_serialized = verify_sdk_keeps_cache_key(r1_input, instructions)

    # An offline cache emulator: first request is a write; the next two find
    # exactly the same immutable prefix and are cache reads plus new writes.
    seen = set()
    outcomes = []
    for digest, _ in prefixes:
        outcomes.append("hit+write" if digest in seen else "write")
        seen.add(digest)

    print("Responses automatic-cache incremental test: PASS")
    print(f"seed_records={len(history)} checkpoint_id_sha256={hashlib.sha256(checkpoint_id.encode()).hexdigest()[:12]}")
    for index, ((digest, byte_count), response_input, outcome) in enumerate(zip(prefixes, (r1_input, r2_input, r3_input), outcomes), 1):
        print(
            f"R{index}: input_items={len(response_input)} cache_prefix_bytes={byte_count} "
            f"cache_prefix_sha256={digest} expected={outcome}"
        )
    print(f"sdk_serialization=PASS prompt_cache_key_present={str(key_serialized).lower()} explicit_breakpoint_sent=false")
    print("network=not-used wechat=not-used")


if __name__ == "__main__":
    main()
