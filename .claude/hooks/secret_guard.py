#!/usr/bin/env python3
"""PreToolUse: агент не читает и не правит .env и ключи — даже если попросить."""
import json
import os
import re
import sys

# .env, .env.local, config/.env — да; .env.example — нет
ENV = re.compile(r"(?<![\w.-])\.env(?:\.(?!example\b)[\w-]+)?(?![\w.-])")
KEY = re.compile(r"[\w-]\.(?:pem|key)(?![\w.-])")


def is_secret(text: str) -> bool:
    return bool(ENV.search(text) or KEY.search(text))


def main() -> int:
    event = json.load(sys.stdin)
    tool = event.get("tool_name", "")
    args = event.get("tool_input", {})
    if tool == "Bash":
        targets = [args.get("command", "")]
    else:
        # у Grep pattern — искомый текст, не путь; у Glob — путь
        keys = ("file_path", "path", "pattern") if tool == "Glob" else ("file_path", "path")
        targets = [args.get(k, "") for k in keys]
    for t in targets:
        if t and is_secret(os.path.basename(t) if tool != "Bash" else t):
            print(f"secret-guard: доступ к секретам закрыт ({tool}). Нужен ключ — попроси человека.", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
