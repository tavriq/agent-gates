#!/usr/bin/env python3
"""PreToolUse: агент не читает и не правит .env и ключи — даже если попросить."""
import json
import os
import re
import sys

# .env, .env.local, .envrc, config/.env — да; .env.example — нет. Регистр не важен: на macOS .ENV — тот же файл
ENV = re.compile(r"(?<![\w.-])\.env(?:rc|\.(?!example\b)[\w-]+)?(?![\w.-])", re.I)
KEY = re.compile(r"[\w-]\.(?:pem|key)(?![\w.-])|\bid_(?:rsa|dsa|ecdsa|ed25519)\b(?!\.pub)", re.I)


def is_secret(text: str) -> bool:
    return bool(ENV.search(text) or KEY.search(text))


def main() -> int:
    event = json.load(sys.stdin)
    tool = event.get("tool_name", "")
    args = event.get("tool_input", {})
    if tool in ("Bash", "Monitor"):
        targets = [args.get("command", "")]
    else:
        # у Grep pattern — искомый текст, не путь, а glob — фильтр файлов; у Glob pattern — путь
        keys = {"Glob": ("path", "pattern"), "Grep": ("path", "glob")}.get(tool, ("file_path", "path", "notebook_path"))
        targets = [os.path.basename(args.get(k) or "") for k in keys]
    for t in targets:
        if t and is_secret(t):
            print(f"secret-guard: доступ к секретам закрыт ({tool}). Нужен ключ — попроси человека.", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # упавший гейт закрывает: exit 1 Claude Code считает «пропустить»
        print(f"secret-guard: сбой гейта ({e!r}), действие остановлено.", file=sys.stderr)
        sys.exit(2)
