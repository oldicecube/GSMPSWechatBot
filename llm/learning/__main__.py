from __future__ import annotations

import argparse
import json
import os

from .learner import StyleLearner


def _arguments():
    parser = argparse.ArgumentParser(description="Build a local group-chat style profile from JSONL")
    parser.add_argument("--input", required=True, help="WeFlow/chat export JSONL")
    parser.add_argument("--group", help="Override the group id/name")
    parser.add_argument("--replace", action="store_true", help="Clear the selected profile first")
    return parser.parse_args()


def main():
    args = _arguments()
    learner = StyleLearner({"learning": {"enabled": False}})
    group_id = args.group
    members = {}
    observations = []
    if args.replace and group_id:
        learner.store.reset_group(group_id)
    with open(args.input, "r", encoding="utf-8") as source:
        for line in source:
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(item, dict):
                continue
            if item.get("_type") == "header":
                meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
                if not group_id:
                    group_id = str(meta.get("name") or meta.get("groupId") or "unknown")
                continue
            if item.get("_type") == "member":
                members[str(item.get("platformId") or "")] = str(
                    item.get("groupNickname") or item.get("accountName") or "unknown"
                )
                continue
            if item.get("_type") != "message":
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            sender = str(item.get("sender") or "unknown")
            observation = {
                "group_id": group_id or "unknown",
                "speaker": sender,
                "speaker_name": members.get(sender, item.get("accountName") or "unknown"),
                "content": content,
                "timestamp": item.get("timestamp"),
                "fingerprint": str(item.get("platformMessageId") or ""),
                "reply_to": item.get("replyToMessageId"),
                "is_media": item.get("type") != 0,
                "is_bot": "服务器状态@我" in str(item.get("groupNickname") or members.get(sender) or "")
                or "gsmps bot" in str(item.get("groupNickname") or members.get(sender) or "").casefold(),
            }
            observations.append(observation)
    count = learner.ingest_many(observations)
    print(f"Imported {count} messages into {learner.store.path} for group {group_id or 'unknown'}")
    learner.enabled = True
    summary = learner.get_prompt_context(group_id or "unknown")
    try:
        print(summary)
    except UnicodeEncodeError:
        print(summary.encode("ascii", errors="backslashreplace").decode("ascii"))


if __name__ == "__main__":
    main()
