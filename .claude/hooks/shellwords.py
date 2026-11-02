"""Слова команды шелла для гейтов (SPEC.md, G2). Это не полный разбор shell, а ровно столько, сколько нужно гейтам.

Что делаем: снимаем кавычки и экранирование (.en''v, .e\\nv, $'\\x2eenv', $"…" → .env), режем команду
по ; & | ( ) < > и переводу строки, заходим внутрь sh -c "…" и $(…), находим вызовы git, разбираем
их опции так, как git (-am = -a -m, --har = --hard), и помечаем слова из переменной или подстановки.
Чего не делаем: не подставляем переменные, не раскрываем * и {a,b}, не читаем алиасы и скрипты.
"""
import re
import shlex
import subprocess
import warnings
from dataclasses import dataclass, field

SEPARATORS = ";&|()<>\n"
DYNAMIC = re.compile(r"[$`]")
# git, /usr/bin/git, $(git, `git, git-commit; но не .git/hooks и не --git-dir
GIT = re.compile(r"(?:^|[=(`/!])git(?:-([a-z][a-z-]*))?$")  # ! — алиас-команда: -c alias.x='!git …'
ANSI_C = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)
# <<EOF, <<-EOF, <<'EOF', <<"EOF", <<\EOF; но не <<< (here-string)
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(?:'(\w+)'|\"(\w+)\"|\\(\w+)|(\w+))")
# commit -m "$(cat <<'EOF' … EOF)": тело heredoc в кавычках не исполняется — это текст сообщения
CAT_HEREDOC = re.compile(r"\$\(\s*cat\s+<<-?\s*'(\w+)'[ \t]*\n(.*)\n[ \t]*\1[ \t]*\n\s*\)", re.S)
MAX_DEPTH = 4

# глобальные опции git со значением отдельным словом
GLOBAL_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix", "--attr-source"}
LOCATION = ("-C", "--git-dir", "--work-tree")
ENV_LOCATION = {"GIT_DIR": "--git-dir", "GIT_WORK_TREE": "--work-tree"}


def _opts(short_values, long_values, long_flags=""):
    return short_values, set(long_values.split()), set(long_flags.split())


# Опции подкоманд: короткие со значением, длинные со значением, длинные без значения.
# Значение отдельным словом нельзя спутать с веткой или путём; по полному списку длинных опций
# раскрываются сокращения: git принимает однозначный префикс, --har — это --hard.
OPTIONS = {
    "commit": _opts("mFCct", "--message --file --reuse-message --reedit-message --fixup --squash --author --date "
                    "--template --cleanup --trailer --pathspec-from-file",
                    "--all --include --only --patch --interactive --amend --dry-run --signoff --no-verify --verify "
                    "--allow-empty --allow-empty-message --no-edit --edit --quiet --verbose --untracked-files --status "
                    "--no-status --gpg-sign --no-gpg-sign --porcelain --short --branch --long --null --reset-author "
                    "--pathspec-file-nul --no-post-rewrite"),
    "merge": _opts("mFsX", "--message --file --strategy --strategy-option --into-name",
                   "--abort --continue --quit --ff --no-ff --ff-only --commit --no-commit --edit --no-edit --squash "
                   "--no-squash --stat --no-stat --log --no-log --signoff --verify --no-verify --autostash "
                   "--allow-unrelated-histories --quiet --verbose --progress --gpg-sign --rerere-autoupdate"),
    "tag": _opts("mFu", "--message --file --local-user", "--annotate --sign --force --delete --list --verify"),
    "stash": _opts("m", "--message --pathspec-from-file", "--patch --staged --keep-index --include-untracked --all --quiet"),
    "notes": _opts("mFcC", "--message --file --reuse-message --reedit-message --ref", "--force --allow-empty"),
    "cherry-pick": _opts("mX", "--mainline --strategy --strategy-option",
                         "--abort --continue --quit --skip --edit --no-edit --no-commit --signoff --ff --allow-empty "
                         "--keep-redundant-commits --empty --gpg-sign"),
    "revert": _opts("mX", "--mainline --strategy --strategy-option",
                    "--abort --continue --quit --skip --edit --no-edit --no-commit --signoff --gpg-sign"),
    "am": _opts("", "--whitespace --directory --exclude --include --patch-format --resolvemsg",
                "--abort --continue --quit --skip --resolved --signoff --keep --3way --interactive --committer-date-is-author-date "
                "--ignore-date --quiet --scissors --no-scissors --show-current-patch"),
    "rebase": _opts("sXxC", "--onto --strategy --strategy-option --exec --empty --whitespace",
                    "--root --keep-base --abort --continue --skip --quit --edit-todo --show-current-patch --apply "
                    "--merge --interactive --autosquash --no-autosquash --autostash --no-autostash --no-ff "
                    "--force-rebase --fork-point --no-fork-point --rebase-merges --no-rebase-merges --update-refs "
                    "--no-update-refs --reschedule-failed-exec --committer-date-is-author-date --ignore-date "
                    "--reset-author-date --signoff --verbose --quiet --stat --no-stat --no-verify --verify --gpg-sign "
                    "--keep-empty --no-keep-empty --reapply-cherry-picks --allow-empty-message --ignore-whitespace"),
    "pull": _opts("sXjo", "--strategy --strategy-option --depth --deepen --shallow-since --shallow-exclude --jobs "
                  "--upload-pack --server-option --negotiation-tip --refmap",
                  "--rebase --no-rebase --ff-only --ff --no-ff --commit --no-commit --edit --no-edit --squash "
                  "--no-squash --stat --no-stat --verify --no-verify --autostash --no-autostash --all --append --force "
                  "--keep --prune --tags --no-tags --quiet --verbose --progress --dry-run --unshallow --update-shallow "
                  "--set-upstream --signoff --allow-unrelated-histories --recurse-submodules --log --no-log"),
    "fetch": _opts("jo", "--depth --deepen --shallow-since --shallow-exclude --jobs --upload-pack --server-option "
                   "--negotiation-tip --refmap",
                   "--all --append --atomic --dry-run --force --keep --multiple --prune --prune-tags --tags --no-tags "
                   "--quiet --verbose --progress --update-head-ok --unshallow --update-shallow --refetch "
                   "--write-fetch-head --no-write-fetch-head --set-upstream --porcelain --recurse-submodules"),
    "push": _opts("o", "--push-option --repo --receive-pack --exec",
                  "--all --branches --mirror --tags --follow-tags --delete --force --force-with-lease --force-if-includes "
                  "--dry-run --porcelain --prune --verbose --quiet --progress --set-upstream --atomic --signed "
                  "--no-signed --thin --no-thin --no-verify --verify --recurse-submodules"),
    "switch": _opts("cC", "--create --force-create --orphan",
                    "--detach --discard-changes --force --merge --conflict --quiet --progress --track --no-track "
                    "--guess --no-guess --recurse-submodules --ignore-other-worktrees"),
    "checkout": _opts("bB", "--orphan --conflict --pathspec-from-file",
                      "--detach --force --merge --quiet --progress --track --no-track --guess --no-guess --ours "
                      "--theirs --patch --overlay --no-overlay --ignore-other-worktrees --recurse-submodules"),
    "worktree": _opts("bB", "--reason --orphan", "--force --detach --checkout --no-checkout --lock --quiet --track "
                      "--no-track --guess-remote --porcelain --verbose"),
    "branch": _opts("u", "--set-upstream-to --format --sort --contains --no-contains --merged --no-merged --points-at",
                    "--force --move --copy --delete --all --remotes --list --show-current --verbose --quiet --track "
                    "--no-track --unset-upstream --edit-description --create-reflog --ignore-case --color --no-color"),
    "update-ref": _opts("m", "", "--stdin --no-deref --create-reflog"),
    "symbolic-ref": _opts("m", "", "--quiet --short --delete --no-recurse --recurse"),
    "subtree": _opts("Pbm", "--prefix --branch --message --onto --annotate", "--squash --rejoin --ignore-joins --quiet --debug"),
    "http-push": _opts("", "", "--all --force --dry-run --verbose"),
    "remote": _opts("tm", "", "--mirror --fetch --tags --no-tags --verbose --push --add --delete --prune --dry-run"),
    "send-pack": _opts("", "--receive-pack --exec --push-option", "--all --mirror --force --dry-run --atomic --stdin "
                       "--thin --no-thin --quiet --verbose --signed --force-with-lease"),
    "config": _opts("f", "--file --blob --type --default --comment", "--add --replace-all --unset --unset-all --get "
                    "--get-all --get-regexp --get-urlmatch --global --system --local --worktree --list --edit --bool "
                    "--int --null --name-only --show-origin --show-scope --includes --no-includes"),
    "reset": _opts("", "--pathspec-from-file", "--hard --soft --mixed --keep --merge --quiet --patch --intent-to-add "
                   "--recurse-submodules --no-refresh --refresh --pathspec-file-nul"),
    "add": _opts("", "--pathspec-from-file --chmod", "--all --update --patch --interactive --edit --dry-run --force "
                 "--verbose --intent-to-add --refresh --ignore-errors --ignore-missing --no-all --renormalize --sparse"),
    "rm": _opts("", "--pathspec-from-file", "--cached --dry-run --force --quiet --ignore-unmatch --sparse"),
}
# встроенные команды git (и git-lfs): всё прочее — алиас или внешняя git-<имя>
BUILTINS = set(OPTIONS) | set("""
    status log diff show add rm mv restore describe blame annotate grep shortlog help version show-ref cat-file
    rev-list rev-parse ls-files ls-tree ls-remote name-rev merge-base for-each-ref check-ignore check-attr clean archive
    count-objects format-patch range-diff whatchanged var hash-object write-tree diff-files diff-index diff-tree apply
    show-branch cherry verify-commit verify-tag merge-tree merge-file interpret-trailers column check-ref-format config
    tag reflog init clone bundle gc fsck prune repack submodule sparse-checkout mergetool difftool request-pull
    read-tree commit-tree update-index maintenance replace credential pack-refs lfs bisect stash filter-branch
    filter-repo fast-import fast-export subtree citool gui svn p4 http-push send-pack notes worktree remote fetch pull
    push rebase reset switch checkout branch update-ref symbolic-ref commit merge cherry-pick revert am
""".split())
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
    flags: set = field(default_factory=set)  # -a, --all, -m … — склейки и сокращения раскрыты: -am = -a -m
    values: dict = field(default_factory=dict)  # опция → значение
    pos: list = field(default_factory=list)  # позиционные до "--"
    paths: list = field(default_factory=list)  # всё после "--"
    message: set = field(default_factory=set)  # индексы слов — текста сообщения

    @property
    def operands(self):
        """Позиционные и после "--": для ветки или ссылки "--" ничего не меняет (git branch -f -- main x)."""
        return self.pos + self.paths


@dataclass
class GitCall:
    where: list  # -C, --git-dir, --work-tree как в команде: чтобы спросить git о том же репозитории
    sub: str
    rest: list  # слова после подкоманды
    args: Args
    dynamic: str  # первое слово из переменной или подстановки, "" — таких нет
    rough: bool  # команду не удалось разобрать по кавычкам — слова грубые
    # настройки этой команды: -c, --config-env, GIT_CONFIG_* — [(ключ, значение)]; значение None — не видно,
    # ключ "*" — подменён весь файл настроек
    config: list = field(default_factory=list)
    env: dict = field(default_factory=dict)  # GIT_…=… перед git, кроме GIT_DIR и GIT_WORK_TREE


def _config_pair(text):
    key, eq, value = text.partition("=")
    return key, value if eq else "true"


# переменные перед git, которые подменяют настройки или хранилище ссылок целиком: что в них — гейту не видно
ENV_OPAQUE = {"GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_PARAMETERS", "GIT_COMMON_DIR",
              "HOME", "XDG_CONFIG_HOME"}


def _env_config(env):
    pairs = []
    for name, value in env.items():
        n = re.fullmatch(r"GIT_CONFIG_KEY_(\d+)", name)
        if n:
            pairs.append((value, env.get(f"GIT_CONFIG_VALUE_{n.group(1)}")))
        elif name in ENV_OPAQUE:
            pairs.append(("*", None))
        elif name == "GIT_NOTES_REF":
            pairs.append(("core.notesref", value))
    return pairs


def _ansi_c(body: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return body.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except (UnicodeError, ValueError):
        return body


def _prepare(text: str, quote) -> str:
    """\\ + перевод строки — склейка строк; $'…' — строка с escape-последовательностями; $"…" — то же, что "…"."""
    text = text.replace("\\\n", "")
    text = ANSI_C.sub(lambda m: quote(_ansi_c(m.group(1))), text)
    return text.replace('$"', '"')


def normalize(text: str) -> str:
    """Текст команды без кавычек и экранирования: .en''v, .e\\nv, ".en"v, $'\\x2eenv', .e$"n"v → .env."""
    return re.sub(r"[\"'\\]", "", _prepare(text, lambda s: s))


def _heredocs(text: str) -> str:
    """Тело heredoc — не код шелла: кавычки в нём без пары ломают разбор, поэтому их снимаем.
    <<"EOF" и <<\\EOF приводим к <<'EOF'. Конца тела не нашли — текст как есть (дальше грубый разбор)."""
    out, pending = [], []
    for line in text.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
                out.append(line)
            else:
                out.append(re.sub(r"[\"'\\]", "", line))
            continue

        def mark(m):
            word = m.group(2) or m.group(3) or m.group(4) or m.group(5)
            pending.append(word)
            return m.group(0) if m.group(5) else f"<<{m.group(1)}'{word}'"

        out.append(HEREDOC.sub(mark, line))
    return text if pending else "\n".join(out)


def _word_lists(text: str):
    """→ (слова, rough). Сначала честно по кавычкам; не вышло (кавычка без пары) — грубо: кавычки долой, режем везде."""
    text = _heredocs(_prepare(text, shlex.quote))
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=SEPARATORS)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        # shlex по умолчанию режет «комментарий» с # и посреди слова: a#b, $#, URL#якорь прятали остаток строки.
        # В bash # — комментарий только в начале слова; гейт комментариев не вырезает вовсе: лишнее слово — не пропуск
        lex.commenters = ""
        tokens = []
        for t in lex:  # группы знаков (&&, )|) — по одному: дальше важен каждый
            tokens.extend(t) if t and all(ch in SEPARATORS for ch in t) else tokens.append(t)
    except ValueError:
        for part in re.split(r"[;&|()<>\n]+", text):
            words = re.sub(r"[\"'\\]", "", part).split()
            if words:
                yield words, True
        return
    words, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t.endswith("$") and i + 1 < len(tokens) and tokens[i + 1] == "(":
            # $(…) без кавычек — одно слово: иначе скобки разрежут git -C $(pwd) commit на куски
            depth, j = 0, i + 1
            while j < len(tokens):
                depth += {"(": 1, ")": -1}.get(tokens[j], 0)
                j += 1
                if depth == 0:
                    break
            words.append(t + "(" + " ".join(tokens[i + 2:j - 1]) + ")")
            i = j
            continue
        if len(t) == 1 and t in SEPARATORS:
            if words:
                yield words, False
            words = []
        else:
            words.append(t)
        i += 1
    if words:
        yield words, False


def _long(name, known):
    """Длинная опция как её поймёт git: точное имя или однозначный префикс; неоднозначный — все варианты."""
    if name in known:
        return [name]
    found = sorted(o for o in known if o.startswith(name))
    return found or [name]


def parse_args(sub: str, rest: list, rough: bool) -> Args:
    short_v, long_v, long_f = OPTIONS.get(sub, ("", set(), set()))
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
            names = _long(name, long_v | long_f)
            a.flags.update(names)
            if eq:
                for n in names:
                    a.values[n] = val
                if set(names) & long_m and message(j):
                    break
            elif len(names) == 1 and names[0] in long_v and j + 1 < len(rest):
                j += 1
                a.values[names[0]] = rest[j]
                if names[0] in long_m and message(j):
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
    where, glob, env, i, sub = [], [], {}, start + 1, m.group(1)
    for w0 in words[:start]:  # GIT_DIR=… git commit — то же, что git --git-dir=… commit
        name, eq, val = w0.partition("=")
        if eq and (name.startswith("GIT_") or name in ENV_OPAQUE):
            glob.append(w0)
            if name in ENV_LOCATION:
                where.append(f"{ENV_LOCATION[name]}={val}")
            else:
                env[name] = val
    config = _env_config(env)
    if not sub:
        while i < len(words) and words[i].startswith("-"):
            opt = words[i]
            glob.append(opt)
            i += 1
            name, eq, val = opt.partition("=")
            if opt in GLOBAL_VALUE and i < len(words):
                glob.append(words[i])
                if opt in LOCATION:
                    where += [opt, words[i]]
                if opt == "-c":
                    config.append(_config_pair(words[i]))
                if opt == "--config-env":  # значение — в переменной окружения, гейту не видно
                    config.append((words[i].partition("=")[0], None))
                i += 1
            elif name in LOCATION:
                where.append(opt)
            elif name == "--config-env" and eq:
                config.append((val.partition("=")[0], None))
        if i >= len(words):  # подкоманды нет; но если путь из подстановки — это всё равно стоп
            dynamic = next((t for t in glob if DYNAMIC.search(t)), "")
            return (GitCall(where, "", [], Args(), dynamic, rough, config, env), i) if dynamic else None
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
    return GitCall(where, sub, rest, args, dynamic, rough, config, env), i  # i — где в words начинается rest


def _substitutions(word: str):
    """(начало, конец, содержимое) каждой $(…) и `…` в слове — их содержимое исполняется."""
    for m in re.finditer(r"`([^`]*)`", word):
        yield m.start(), m.end(), m.group(1)
    i = word.find("$(")
    while i != -1:
        depth, j = 1, i + 2
        while j < len(word) and depth:
            depth += {"(": 1, ")": -1}.get(word[j], 0)
            j += 1
        yield i, j, word[i + 2:j - 1]
        i = word.find("$(", j)


def _is_text(word: str) -> bool:
    m = CAT_HEREDOC.fullmatch(word)
    return bool(m) and m.group(1) not in (line.strip() for line in m.group(2).split("\n"))


def _alias(call, cwd):
    """Значение алиаса: из -c этой команды, иначе из настроек репозитория; не алиас — None."""
    value = next((v for k, v in call.config if k.lower() == f"alias.{call.sub.lower()}"), None)
    if value is None:
        opts = [x for k, v in call.config if v is not None for x in ("-c", f"{k}={v}")]
        r = subprocess.run(["git", *call.where, *opts, "config", "--get", f"alias.{call.sub}"], cwd=cwd,
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        value = r.stdout.strip() if r.returncode == 0 else None
    return value or None


def expand_aliases(calls, cwd, depth=0):
    """Алиасы заменены тем, во что они раскрываются: git ci → git commit, «!…» — команда шелла."""
    out = []
    for call in calls:
        value = _alias(call, cwd) if call.sub and call.sub not in BUILTINS and not call.dynamic and depth < 5 else None
        if value is None:
            out.append(call)
            continue
        args = " ".join(shlex.quote(w) for w in call.rest)
        inner = list(git_calls(f"{value[1:]} {args}" if value.startswith("!") else f"git {value} {args}"))
        for c in inner:  # -c и -C самой команды действуют и на то, во что раскрылся алиас
            c.where = call.where + c.where
            c.config = call.config + c.config
        out += expand_aliases(inner, cwd, depth + 1)
    return out


def git_calls(text: str, depth: int = 0):
    """Вызовы git в команде, в порядке исполнения: подстановки и sh -c "…" — тоже."""
    for words, rough in _word_lists(text):
        call, offset = _parse_git(words, rough) or (None, 0)
        if not rough and depth < MAX_DEPTH:
            for i, w in enumerate(words):
                if call and i - offset in call.args.message and _is_text(w):
                    continue  # сообщение коммита из heredoc в кавычках — только текст
                subs = list(_substitutions(w))
                outside = w
                for s, e, _ in reversed(subs):
                    outside = outside[:s] + outside[e:]
                if not (call and i - offset in call.args.message) and re.search(r"[\s;&|()<>`]", outside):
                    yield from git_calls(w, depth + 1)  # sh -c "git …", eval "git …" — подстановки найдутся там же
                else:  # слово без команд вокруг (или текст сообщения): исполняются только подстановки
                    for _, _, inner in subs:
                        yield from git_calls(inner, depth + 1)
        if call:
            yield call
