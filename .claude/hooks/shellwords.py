"""Слова команды шелла для гейтов (SPEC.md, G2). Это не полный разбор shell, а ровно столько, сколько нужно гейтам.

Что делаем: снимаем кавычки и экранирование (.en''v, .e\\nv, $'\\x2eenv' → .env), режем команду
по ; & | ( ) < > и переводу строки, заходим внутрь sh -c "…" и $(…), находим вызовы git
и помечаем слова, взятые из переменной или подстановки.
Чего не делаем: не подставляем переменные, не раскрываем * и {a,b}, не читаем алиасы и скрипты.
"""
import re
import shlex
import warnings
from dataclasses import dataclass, field

SEPARATORS = ";&|()<>\n"
DYNAMIC = re.compile(r"[$`]")
NESTED = re.compile(r"[\s;&|()<>`]|\$\(")
# git, /usr/bin/git, $(git, `git, git-commit; но не .git/hooks и не --git-dir
GIT = re.compile(r"(?:^|[=(`/])git(?:-([a-z][a-z-]*))?$")
ANSI_C = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)
# commit -m "$(cat <<'EOF' … EOF)": тело heredoc в кавычках не исполняется — это текст сообщения
CAT_HEREDOC = re.compile(r"\$\(\s*cat\s+<<-?\s*(['\"])(\w+)\1[ \t]*\n(.*)\n[ \t]*\2[ \t]*\n\s*\)", re.S)
MAX_DEPTH = 4

# глобальные опции git со значением отдельным словом
GLOBAL_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix", "--attr-source"}
LOCATION = ("-C", "--git-dir", "--work-tree")
ENV_LOCATION = {"GIT_DIR": "--git-dir", "GIT_WORK_TREE": "--work-tree"}
# опции подкоманд со значением отдельным словом: без них значение спутать с веткой или путём
VALUE_OPTS = {
    "commit": ("mFCct", {"--message", "--file", "--reuse-message", "--reedit-message", "--fixup", "--squash",
                         "--author", "--date", "--template", "--cleanup", "--trailer", "--pathspec-from-file"}),
    "merge": ("mFsX", {"--message", "--file", "--strategy", "--strategy-option", "--into-name"}),
    "tag": ("mFu", {"--message", "--file", "--local-user"}),
    "stash": ("m", {"--message", "--pathspec-from-file"}),
    "notes": ("mFcC", {"--message", "--file", "--reuse-message", "--reedit-message", "--ref"}),
    "cherry-pick": ("mX", {"--mainline", "--strategy", "--strategy-option"}),
    "revert": ("mX", {"--mainline", "--strategy", "--strategy-option"}),
    "rebase": ("sXx", {"--onto", "--strategy", "--strategy-option", "--exec"}),
    "pull": ("sXjo", {"--strategy", "--strategy-option", "--depth", "--deepen", "--shallow-since",
                      "--shallow-exclude", "--jobs", "--upload-pack", "--server-option", "--negotiation-tip"}),
    "fetch": ("jo", {"--depth", "--deepen", "--shallow-since", "--shallow-exclude", "--jobs", "--upload-pack",
                     "--server-option", "--negotiation-tip"}),
    "push": ("o", {"--push-option", "--repo", "--receive-pack", "--exec"}),
    "switch": ("cC", {"--create", "--force-create", "--orphan"}),
    "checkout": ("bB", {"--orphan"}),
    "worktree": ("bB", {"--reason"}),
    "branch": ("u", {"--set-upstream-to", "--format", "--sort", "--contains", "--no-contains", "--merged",
                     "--no-merged", "--points-at"}),
    "update-ref": ("m", set()),
    "reset": ("", {"--pathspec-from-file"}),
    "add": ("", {"--pathspec-from-file"}),
    "rm": ("", {"--pathspec-from-file"}),
}
# где значение опции — текст сообщения: подстановка в нём законна (commit -m "$(cat <<'EOF' …)")
MESSAGE_OPTS = {
    "commit": ("mF", {"--message", "--file"}),
    "merge": ("mF", {"--message", "--file"}),
    "tag": ("mF", {"--message", "--file"}),
    "stash": ("m", {"--message"}),
    "notes": ("mF", {"--message", "--file"}),
}


@dataclass
class Args:
    flags: set = field(default_factory=set)  # -a, --all, -m … — короткие склейки разобраны: -am = -a -m
    values: dict = field(default_factory=dict)  # опция → значение
    pos: list = field(default_factory=list)  # позиционные до "--"
    paths: list = field(default_factory=list)  # всё после "--"
    message: set = field(default_factory=set)  # индексы слов — текста сообщения


@dataclass
class GitCall:
    where: list  # -C, --git-dir, --work-tree как в команде: чтобы спросить git о том же репозитории
    sub: str
    rest: list  # слова после подкоманды
    args: Args
    dynamic: str  # первое слово из переменной или подстановки, "" — таких нет
    rough: bool  # команду не удалось разобрать по кавычкам — слова грубые


def _ansi_c(body: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return body.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except (UnicodeError, ValueError):
        return body


def normalize(text: str) -> str:
    """Текст команды без кавычек и экранирования: .en''v, .e\\nv, ".en"v, $'\\x2eenv' → .env."""
    text = ANSI_C.sub(lambda m: _ansi_c(m.group(1)), text.replace("\\\n", ""))
    return re.sub(r"[\"'\\]", "", text)


def _word_lists(text: str):
    """→ (слова, rough). Сначала честно по кавычкам; не вышло (кавычка без пары) — грубо: кавычки долой, режем везде."""
    text = ANSI_C.sub(lambda m: shlex.quote(_ansi_c(m.group(1))), text.replace("\\\n", ""))
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=SEPARATORS)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        for part in re.split(r"[;&|()<>\n]+", text):
            words = re.sub(r"[\"'\\]", "", part).split()
            if words:
                yield words, True
        return
    words = []
    for t in tokens + ["\n"]:
        if t and all(ch in SEPARATORS for ch in t):
            if words:
                yield words, False
            words = []
        else:
            words.append(t)


def parse_args(sub: str, rest: list, rough: bool) -> Args:
    short_v, long_v = VALUE_OPTS.get(sub, ("", set()))
    short_m, long_m = MESSAGE_OPTS.get(sub, ("", set()))
    a = Args()

    def message(j):
        a.message.add(j)
        if rough:  # где кончается сообщение, в грубом разборе не видно: всё до конца — текст
            a.message.update(range(j, len(rest)))
            return True
        return False

    j = 0
    while j < len(rest):
        w = rest[j]
        if w == "--":
            a.paths = rest[j + 1:]
            break
        if w.startswith("--"):
            name, eq, val = w.partition("=")
            a.flags.add(name)
            if eq:
                a.values[name] = val
                if name in long_m and message(j):
                    break
            elif name in long_v and j + 1 < len(rest):
                j += 1
                a.values[name] = rest[j]
                if name in long_m and message(j):
                    break
        elif w.startswith("-") and len(w) > 1:
            stop = False
            for k, ch in enumerate(w[1:], 1):
                opt = "-" + ch
                a.flags.add(opt)
                if ch in short_v:
                    if k < len(w) - 1:  # значение приклеено: -mtext
                        a.values[opt] = w[k + 1:]
                        stop = ch in short_m and message(j)
                    elif j + 1 < len(rest):
                        j += 1
                        a.values[opt] = rest[j]
                        stop = ch in short_m and message(j)
                    break
            if stop:
                break
        else:
            a.pos.append(w)
        j += 1
    return a


def _parse_git(words: list, rough: bool):
    for start, w in enumerate(words):
        m = GIT.search(w)
        if m:
            break
    else:
        return None
    where, glob, i, sub = [], [], start + 1, m.group(1)
    for w0 in words[:start]:  # GIT_DIR=… git commit — то же, что git --git-dir=… commit
        name, eq, val = w0.partition("=")
        if eq and name in ENV_LOCATION:
            where.append(f"{ENV_LOCATION[name]}={val}")
            glob.append(w0)
    if not sub:
        while i < len(words) and words[i].startswith("-"):
            opt = words[i]
            glob.append(opt)
            i += 1
            if opt in GLOBAL_VALUE and i < len(words):
                glob.append(words[i])
                if opt in LOCATION:
                    where += [opt, words[i]]
                i += 1
            elif opt.partition("=")[0] in LOCATION:
                where.append(opt)
        if i >= len(words):
            return None
        sub, i = words[i], i + 1
    rest = words[i:]
    if "`" in w:  # `git status` — вызов кончается на закрывающей обратной кавычке
        tokens = [sub] + rest
        for j, t in enumerate(tokens):
            if "`" in t:
                tokens = tokens[:j] + [t.split("`")[0]]
                break
        tokens = [t for t in tokens if t]
        if not tokens:
            return None
        sub, rest = tokens[0], tokens[1:]
    args = parse_args(sub, rest, rough)
    words_checked = glob + [sub] + [r for j, r in enumerate(rest) if j not in args.message]
    dynamic = next((t for t in words_checked if DYNAMIC.search(t)), "")
    return GitCall(where, sub, rest, args, dynamic, rough), i  # i — где в words начинается rest


def _substitutions(word: str):
    """Содержимое $(…) и `…` внутри слова — оно исполняется."""
    for m in re.finditer(r"`([^`]*)`", word):
        yield m.group(1)
    i = word.find("$(")
    while i != -1:
        depth, j = 1, i + 2
        while j < len(word) and depth:
            depth += {"(": 1, ")": -1}.get(word[j], 0)
            j += 1
        yield word[i + 2:j - 1]
        i = word.find("$(", j)


def _is_text(word: str) -> bool:
    m = CAT_HEREDOC.fullmatch(word)
    return bool(m) and m.group(2) not in (line.strip() for line in m.group(3).split("\n"))


def git_calls(text: str, depth: int = 0):
    """Вызовы git в команде, в порядке исполнения: подстановки и sh -c "…" — тоже."""
    for words, rough in _word_lists(text):
        call, offset = _parse_git(words, rough) or (None, 0)
        if not rough and depth < MAX_DEPTH:
            for i, w in enumerate(words):
                if call and i - offset in call.args.message:
                    if not _is_text(w):
                        for sub in _substitutions(w):
                            yield from git_calls(sub, depth + 1)
                elif NESTED.search(w):
                    yield from git_calls(w, depth + 1)
        if call:
            yield call
