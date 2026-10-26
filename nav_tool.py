"""Nav Tool: веб-интерфейс для правки nav в mkdocs.yml.

Запуск: python nav_tool.py  (откроется браузер на http://127.0.0.1:8765)
Только стандартная библиотека. mkdocs.yml правится построчно, чтобы
сохранить комментарии, отступы, CRLF и теги вида !!python/name.
"""
import difflib
import hashlib
import json
import os
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
BACKUPS = HERE / "backups"
UI = HERE / "ui.html"
PORT = 8765
UNDO_DEPTH = 30

TOP_KEY_RE = re.compile(r"^[^\s#-]")
NO_SORT_TITLES = {"главная", "о продукте"}
SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "site"}
LOCK = threading.Lock()


# ---------- конфиг ----------

def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def yml_path(repo):
    for name in ("mkdocs.yml", "mkdocs.yaml"):
        p = Path(repo) / name
        if p.is_file():
            return p
    return None


def site_name(repo):
    p = yml_path(repo)
    if not p:
        return ""
    try:
        for l in p.read_text(encoding="utf-8-sig", errors="replace").splitlines()[:40]:
            m = re.match(r"^site_name\s*:\s*(.+?)\s*$", l)
            if m:
                return unquote(m.group(1))
    except Exception:
        pass
    return ""


_found = None


def find_repos():
    global _found
    if _found is None:
        found = []
        home = Path.home()
        for root in (home / "Desktop", home / "OneDrive" / "Desktop", home / "Documents"):
            if not root.is_dir():
                continue
            for pattern in ("*/mkdocs.y*ml", "*/*/mkdocs.y*ml", "*/*/*/mkdocs.y*ml"):
                try:
                    for p in root.glob(pattern):
                        d = str(p.parent)
                        if d not in found:
                            found.append(d)
                except OSError:
                    pass
        _found = found
    return _found


def repo_card(path):
    return {"path": path, "name": site_name(path) or Path(path).name,
            "folder": Path(path).name, "branch": git(path, "branch", "--show-current")}


# ---------- работа с mkdocs.yml ----------

def read_yml(repo):
    path = yml_path(repo)
    if not path:
        raise ValueError("В папке нет mkdocs.yml: " + repo)
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    return lines, nl, bom, hashlib.sha1(raw).hexdigest()


YAML_CHECK = r"""
import sys, json, yaml
text = sys.stdin.buffer.read().decode("utf-8-sig")
def out(d):
    print(json.dumps(d))
    sys.exit(0)
try:
    root = yaml.compose(text, Loader=yaml.SafeLoader)
except yaml.MarkedYAMLError as e:
    m = e.problem_mark or e.context_mark
    out({"kind": "syntax", "line": m.line + 1 if m else None, "col": m.column + 1 if m else None,
         "problem": e.problem or "", "ctx_line": e.context_mark.line + 1 if e.context_mark else None})
except Exception as e:
    out({"kind": "syntax", "line": None, "problem": str(e)})
nav = None
if isinstance(root, yaml.MappingNode):
    for k, v in root.value:
        if isinstance(k, yaml.ScalarNode) and k.value == "nav":
            nav = v
def pos(n):
    return n.start_mark.line + 1, n.start_mark.column + 1
def chk(seq):
    if not isinstance(seq, yaml.SequenceNode):
        ln, col = pos(seq)
        out({"kind": "nav", "problem": "not_list", "line": ln, "col": col})
    for it in seq.value:
        if isinstance(it, yaml.ScalarNode):
            continue
        if not isinstance(it, yaml.MappingNode):
            ln, col = pos(it)
            out({"kind": "nav", "problem": "bad_item", "line": ln, "col": col})
        if len(it.value) != 1:
            k2 = it.value[1][0]
            ln, col = pos(k2)
            out({"kind": "nav", "problem": "dict_size", "line": ln, "col": col, "ctx_line": pos(it)[0],
                 "keys": [k.value for k, _ in it.value]})
        k, v = it.value[0]
        if isinstance(v, yaml.ScalarNode):
            if v.value == "" or v.tag.endswith(":null"):
                ln, col = pos(k)
                out({"kind": "nav", "problem": "empty_section", "line": ln, "col": col, "keys": [k.value]})
            continue
        chk(v)
if nav is not None:
    chk(nav)
print("{}")
"""


def yaml_error(text):
    """Проверка YAML и структуры nav через PyYAML из окружения mkdocs. None = ок или проверить нечем."""
    py = ENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.is_file():
        return None
    try:
        r = subprocess.run([str(py), "-c", YAML_CHECK], input=text.encode("utf-8"), capture_output=True,
                           timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        res = json.loads(r.stdout.decode("utf-8", "replace").strip() or "{}")
    except Exception:
        return None
    return res or None


def describe_yaml_error(err):
    p = err.get("problem", "")
    keys = err.get("keys") or []
    if p == "dict_size" and len(keys) > 1:
        return (f"у строки «{keys[1]}:» потерялся «- » (или съехал отступ), поэтому mkdocs склеивает "
                f"«{keys[0]}» и «{keys[1]}» в один пункт меню")
    if p == "empty_section" and keys:
        return f"раздел «{keys[0]}» пустой, в нем нет ни одной страницы (mkdocs такое не собирает)"
    if p == "not_list":
        return "здесь ожидается список пунктов (строки с «- »)"
    if p == "bad_item":
        return "непонятный пункт меню"
    tr = {"could not find expected ':'": "не хватает двоеточия после названия",
          "mapping values are not allowed here": "лишнее двоеточие (название с «: » нужно взять в кавычки)",
          "found character '\\t' that cannot start any token": "табуляция вместо пробелов",
          "expected <block end>, but found '-'": "неправильный отступ у пункта",
          "expected <block end>, but found '<block mapping start>'": "неправильный отступ",
          "found undefined alias": "символ * в начале названия (нужны кавычки)"}
    return tr.get(p, p)


def yaml_fix(lines, err):
    """Однозначные автоисправления. Возвращает (новые строки, описание) или None."""
    if err.get("problem") == "dict_size" and err.get("line") and err.get("col"):
        i, c = err["line"] - 1, err["col"] - 1
        l = lines[i]
        variants = [l[:c] + "- " + l[c:]]
        if c >= 2 and l[c - 2:c] == "  ":
            variants.append(l[:c - 2] + "- " + l[c:])
        for v in variants:
            fixed = list(lines)
            fixed[i] = v
            if not yaml_error("\n".join(fixed)):
                return fixed, f"добавить «- » в строку {err['line']}", v
    return None


def write_yml(repo, lines, nl, bom):
    text = nl.join(lines)
    err = yaml_error(text)
    if err:
        old_lines, _, _, _ = read_yml(repo)
        if not yaml_error("\n".join(old_lines)):
            raise ValueError(f"Эта правка сломала бы mkdocs.yml (строка {err.get('line')}: "
                             f"{describe_yaml_error(err)}), поэтому не записываю")
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    yml_path(repo).write_bytes(data)


def docs_dir_of(lines):
    for l in lines:
        m = re.match(r"^docs_dir\s*:\s*['\"]?([^'\"#]+?)['\"]?\s*(#.*)?$", l)
        if m:
            d = m.group(1).strip()
            # docs_dir вне проекта (../, C:/, /abs) открыл бы через API любые файлы на диске
            if d.startswith(("/", "\\")) or ".." in re.split(r"[\\/]", d) or re.match(r"^[a-zA-Z]:", d):
                raise ValueError(f"docs_dir: {d} указывает за пределы проекта, программа так не работает")
            return d
    return "docs"


def unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        q = s[0]
        s = s[1:-1]
        if q == "'":
            s = s.replace("''", "'")
    return s


def split_nav_item(l):
    """Строка nav «- Название: путь» -> (отступ, название или None, значение).
    Название в кавычках может содержать двоеточие («'LES-1: текст': путь»), его нельзя резать по первому «:»."""
    m = re.match(r"^(\s*)-\s+(.*?)\s*$", l)
    if not m or not m.group(2):
        return None
    indent, rest = len(m.group(1)), m.group(2)
    if rest[0] in "'\"":
        q, i, buf = rest[0], 1, []
        while i < len(rest):
            c = rest[i]
            if q == "'" and c == "'" and rest[i + 1:i + 2] == "'":
                buf.append("'")
                i += 2
                continue
            if q == '"' and c == "\\" and i + 1 < len(rest):
                buf.append(rest[i + 1])
                i += 2
                continue
            if c == q:
                break
            buf.append(c)
            i += 1
        else:
            return indent, None, rest
        after = rest[i + 1:].lstrip()
        if after.startswith(":") and (len(after) == 1 or after[1] in " \t"):
            return indent, "".join(buf), after[1:].strip()
        return indent, None, rest
    m2 = re.match(r"^(.*?)\s*:(?:\s+(.*?))?\s*$", rest)
    if m2 and (m2.group(2) is not None or rest.endswith(":")):
        return indent, m2.group(1), (m2.group(2) or "").strip()
    return indent, None, rest


def parse_nav(lines):
    start = next((i for i, l in enumerate(lines) if re.match(r"^nav\s*:\s*$", l)), None)
    if start is None:
        raise ValueError("В mkdocs.yml не найден блок nav:")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if TOP_KEY_RE.match(lines[i]):
            end = i
            break
    root = {"id": 0, "parent": None, "title": "Корень меню", "path": None, "raw": "",
            "line": start, "indent": -1, "last": start, "children": []}
    nodes = [root]
    stack = [root]
    for i in range(start + 1, end):
        l = lines[i]
        if not l.strip() or l.lstrip().startswith("#"):
            continue
        item = split_nav_item(l)
        if item is None:
            for s in stack:
                s["last"] = i
            continue
        indent, title, raw = item
        while len(stack) > 1 and stack[-1]["indent"] >= indent:
            stack.pop()
        parent = stack[-1]
        value = unquote(raw.split(" #")[0]) if raw else None
        node = {"id": len(nodes), "parent": parent["id"], "title": title if title is not None else value,
                "path": value, "raw": raw, "bare": title is None,
                "line": i, "indent": indent, "last": i, "children": []}
        nodes.append(node)
        parent["children"].append(node["id"])
        for s in stack:
            s["last"] = i
        if not raw:
            stack.append(node)
    return nodes, start, end


def descendant_pages(nodes, nid):
    out = []
    for c in nodes[nid]["children"]:
        n = nodes[c]
        if n["path"]:
            out.append(n["path"])
        else:
            out.extend(descendant_pages(nodes, c))
    return out


def is_local_md(p):
    return p and p.lower().endswith(".md") and "://" not in p


def dir_hint(nodes, nid):
    cur = nid
    while cur is not None:
        pages = [p for p in descendant_pages(nodes, cur) if is_local_md(p)]
        if pages:
            # папка раздела: самая верхняя папка, где лежат только страницы этого раздела
            # (у «Требований» это analysis, а не analysis/APIM2M/Faza1, где их просто больше всего);
            # если своей папки нет, общая папка всех его страниц
            dirs = [p.rsplit("/", 1)[0].split("/") if "/" in p else [] for p in pages]
            common = []
            for parts in zip(*dirs):
                if any(x != parts[0] for x in parts):
                    break
                common.append(parts[0])
            mine = set(pages)
            foreign = [n["path"] for n in nodes if is_local_md(n["path"]) and n["path"] not in mine]
            for depth in range(1, len(common) + 1):
                f = "/".join(common[:depth]) + "/"
                if not any(p.startswith(f) for p in foreign):
                    return f[:-1]
            return "/".join(common)
        cur = nodes[cur]["parent"]
    return ""


def yaml_title(t):
    t = t.strip()
    special = t[:1] in "[]{}&*!|>'\"%@`#,?:-" or ": " in t or " #" in t or t.endswith(":")
    if special:
        return "'" + t.replace("'", "''") + "'"
    return t


def natkey(s):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", (s or "").lower())]


def prefix_of(s):
    m = re.match(r"^\D+(?=\d)", s or "")
    return m.group(0).lower() if m else ""


def child_indent(nodes, n):
    if n["children"]:
        return nodes[n["children"][0]]["indent"]
    return n["indent"] + 2 if n["indent"] >= 0 else 2


def step_of(nodes, n):
    if n["indent"] >= 0 and n["children"]:
        st = nodes[n["children"][0]]["indent"] - n["indent"]
        if st > 0:
            return st
    return 2


def breadcrumb(nodes, nid):
    parts = []
    cur = nid
    while cur:
        parts.append(nodes[cur]["title"])
        cur = nodes[cur]["parent"]
    return " > ".join(reversed(parts)) or "Корень меню"


def plan_inserts(lines, nodes, targets, title, path, mode, subsection):
    inserts = []
    notes = []
    for t in targets:
        # цель: id раздела или {"id", "sub"}; sub - строка или цепочка новых разделов (пустые разделы
        # из интерфейса, которых еще нет в nav, могут быть вложены друг в друга)
        tid, own_sub = (int(t["id"]), t.get("sub")) if isinstance(t, dict) else (int(t), None)
        n = nodes[tid]
        crumb = breadcrumb(nodes, tid)
        if isinstance(own_sub, list):
            subs = [s.strip() for s in own_sub if s and s.strip()]
        else:
            s0 = (own_sub or subsection or "").strip()
            subs = [s0] if s0 else []
        # часть цепочки уже может быть в nav: спускаемся по существующим разделам
        while subs:
            existing = next((nodes[c] for c in n["children"]
                             if nodes[c]["path"] is None and nodes[c]["title"] == subs[0]), None)
            if not existing:
                break
            n = existing
            crumb = crumb + " > " + subs.pop(0)
        ci = child_indent(nodes, n)
        if not subs and any(nodes[c]["path"] == path for c in n["children"]):
            notes.append(("skip", f"{crumb}: страница там уже есть, пропущено"))
            continue
        entry = f"- {yaml_title(title)}: {path}"
        if subs:
            st = step_of(nodes, n)
            new, ind = [], ci
            for s in subs:
                new.append(" " * ind + f"- {yaml_title(s)}:")
                ind += st
            new.append(" " * ind + entry)
        else:
            new = [" " * ci + entry]
        sub = " > ".join(subs)
        idx = n["last"] + 1
        if mode == "alpha":
            name = subs[0] if subs else title
            key = natkey(name)
            sibs = [nodes[c] for c in n["children"] if (nodes[c]["title"] or "").lower() not in NO_SORT_TITLES]
            # LES-12000 сравниваем только с LES-*, иначе латиница уезжает выше кириллицы
            pref = prefix_of(name)
            same = [s for s in sibs if pref and prefix_of(s["title"]) == pref]
            pool = same if pref else sibs
            nxt = next((s for s in pool if natkey(s["title"]) > key), None)
            if nxt:
                idx = nxt["line"]
            elif same:
                idx = same[-1]["last"] + 1
        inserts.append((idx, new))
        notes.append(("add", crumb + (f" > {sub} (новый подраздел)" if sub else "")))
    new_lines = list(lines)
    for idx, new in sorted(inserts, key=lambda x: x[0], reverse=True):
        new_lines[idx:idx] = new
    return new_lines, notes, len(inserts)


# ---------- файлы docs ----------

def md_title(text, fallback):
    t = text.lstrip("﻿").replace("\r\n", "\n")
    if t.startswith("---"):
        m = re.match(r"^---\s*\n(.*?)\n---", t, re.S)
        if m:
            tm = re.search(r"^title\s*:\s*(.+)$", m.group(1), re.M)
            if tm:
                return unquote(tm.group(1))
    hm = re.search(r"^#\s+(.+?)\s*#*\s*$", t, re.M)
    return hm.group(1).strip() if hm else fallback


def scan_docs(docs):
    files, dirs = [], []
    for cur, dnames, fnames in os.walk(docs):
        dnames[:] = sorted(d for d in dnames if not d.startswith(".") and d not in SKIP_DIRS)
        rel_dir = Path(cur).relative_to(docs).as_posix()
        if rel_dir != ".":
            dirs.append(rel_dir)
        for f in sorted(fnames):
            if not f.lower().endswith(".md"):
                continue
            p = Path(cur) / f
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(4000)
            except Exception:
                head = ""
            files.append({"path": p.relative_to(docs).as_posix(), "title": md_title(head, p.stem)})
    return files, dirs


def safe_rel(s):
    s = (s or "").replace("\\", "/").strip().strip("/")
    parts = [p for p in s.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or re.match(r"^[a-zA-Z]:", s):
        raise ValueError("Недопустимый путь: " + s)
    return "/".join(parts)


def git_run(repo, *args, timeout=20):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        r = subprocess.run(["git", "-c", "core.quotepath=false", "-C", repo, *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # ведущие пробелы не трогаем: в status --short они значимы
        out = r.stdout.rstrip() + ("\n" + r.stderr.rstrip() if r.stderr.strip() else "")
        return r.returncode, out.strip("\n")
    except subprocess.TimeoutExpired:
        return 1, "git не ответил вовремя (возможно, ждет логин/пароль в отдельном окне)"
    except FileNotFoundError:
        return 1, "git не найден в PATH"


def git(repo, *args):
    code, out = git_run(repo, *args)
    return out.strip() if code == 0 else ""


def git_state(repo):
    if git(repo, "rev-parse", "--is-inside-work-tree") != "true":
        return {"is_repo": False}
    _, status = git_run(repo, "status", "--short")
    upstream = git(repo, "rev-parse", "--abbrev-ref", "@{u}")
    ahead = git(repo, "rev-list", "--count", "@{u}..HEAD") if upstream else ""
    behind = git(repo, "rev-list", "--count", "HEAD..@{u}") if upstream else ""
    if upstream:
        ahead, behind = clean_counts(repo, ahead, behind)
    branch = git(repo, "branch", "--show-current")
    base = base_ref(repo)
    base_behind = None
    if base and branch and base.split("/", 1)[1] != branch:
        n = git(repo, "rev-list", "--count", f"HEAD..{base}")
        base_behind = int(n) if n.isdigit() else None
    cfg = load_config()
    return {"is_repo": True, "branch": branch,
            "upstream": upstream, "ahead": int(ahead) if ahead.isdigit() else None,
            "behind": int(behind) if behind.isdigit() else None,
            "base": base, "base_behind": base_behind,
            "merged": [r.split("/", 1)[1] for r in merged_refs(repo)],
            "fetched": cfg.get("fetched", {}).get(repo),
            "status": [l for l in status.splitlines() if l.strip()],
            "log": git(repo, "log", "-3", "--pretty=format:%h %s").splitlines(),
            "stashes": [dict(zip(("ref", "when", "msg"), l.split("\t", 2)))
                        for l in git(repo, "stash", "list", "--format=%gd%x09%cr%x09%s").splitlines() if l.count("\t") >= 2],
            "origin": git(repo, "remote", "get-url", "origin")}


def base_ref(repo):
    """Основная ветка: upstream/master (если работа в форке), иначе origin/master или main."""
    for remote in ("upstream", "origin"):
        for b in ("master", "main"):
            if git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{b}"):
                return f"{remote}/{b}"
    return None


def dirty_files(repo):
    _, out = git_run(repo, "status", "--porcelain")
    files = set()
    for l in out.splitlines():
        if len(l) > 3 and not l.startswith("??"):
            for part in l[3:].split(" -> "):
                files.add(part.strip().strip('"'))
    return files


def overlap_guard(repo, target, what):
    """Не даем обновиться, если локальные незакоммиченные правки и входящие коммиты трогают одни файлы."""
    incoming = set(git(repo, "diff", "--name-only", "HEAD..." + target).splitlines())
    clash = sorted(dirty_files(repo) & incoming)
    if clash:
        raise ValueError(f"Нельзя {what}: в этих файлах есть и твои незакоммиченные правки, и изменения с сервера:\n"
                         + "\n".join(clash) + "\nСначала закоммить их (или отмени), потом повтори.")


def api_sync(repo):
    cfg = load_config()
    code, out = git_run(repo, "fetch", "--all", "--prune", timeout=180)
    if code:
        raise ValueError("fetch не прошел:\n" + out)
    cfg.setdefault("fetched", {})[repo] = time.strftime("%d.%m %H:%M")
    save_config(cfg)
    log = [out] if out else []
    upstream = git(repo, "rev-parse", "--abbrev-ref", "@{u}")
    behind = git(repo, "rev-list", "--count", "HEAD..@{u}") if upstream else "0"
    ahead = git(repo, "rev-list", "--count", "@{u}..HEAD") if upstream else "0"
    if upstream:
        ahead, behind = clean_counts(repo, ahead, behind)
    if not upstream:
        msg = "Скачал обновления. Текущая ветка еще не связана с сервером, обновлять ее нечем"
    elif behind == "0":
        msg = "Скачал обновления. Ветка уже свежая"
    else:
        if ahead != "0":
            raise ValueError(f"Ветка разошлась с {upstream}: у тебя {ahead} своих коммитов, на сервере {behind} новых. "
                             "Такое лучше решать вручную (git pull --rebase).")
        overlap_guard(repo, "@{u}", "обновиться")
        code, out = git_run(repo, "pull", "--ff-only", "--autostash", timeout=180)
        log.append(out)
        if code:
            raise ValueError("pull не прошел:\n" + out)
        msg = f"Обновлено: пришло коммитов {behind}"
    # ветки, которые уже вливали в эту: подтягиваем их новые коммиты
    for ref in merged_refs(repo):
        n = git(repo, "rev-list", "--count", f"HEAD..{ref}")
        if not n.isdigit() or n == "0":
            continue
        short = ref.split("/", 1)[1]
        try:
            r = merge_with_union(repo, ref, f"из {short} влито коммитов: {n}", True)
            msg += ". " + r["message"][0].upper() + r["message"][1:]
            log.append(r.get("output", ""))
        except ValueError as e:
            msg += f". {short} не влил: {str(e).splitlines()[0]}"
            log.append(str(e))
    return {"message": msg, "output": "\n".join(x for x in log if x)}


MERGED_RE = re.compile(r"^Merge (?:remote-tracking )?branch '([^']+)'(?: into \S+)?$")


def clean_counts(repo, ahead, behind):
    """Ветка отправлена без доп. веток: на сервере свой коммит, сравниваем с тем, что было при отправке."""
    rec = published(repo, git(repo, "branch", "--show-current"))
    if not rec or git(repo, "rev-parse", "@{u}") != rec["sha"]:
        return ahead, behind
    head = git(repo, "rev-parse", "HEAD")
    if head == rec["head"]:
        return "0", "0"
    n = git(repo, "rev-list", "--count", "--first-parent", f"{rec['head']}..HEAD")
    return (n if n.isdigit() else "1"), "0"


def merged_tips(repo):
    """Доп. ветки, влитые в текущую: {ref: коммит этой ветки на момент последнего вливания}.
    Ветки, которые уже целиком в master, не считаются."""
    cur = git(repo, "branch", "--show-current")
    if not cur or cur in ("master", "main"):
        return {}
    base = base_ref(repo)
    upstream = git(repo, "rev-parse", "--abbrev-ref", "@{u}")
    rng = [f"{base}..HEAD"] if base else ["-200", "HEAD"]
    out = {}
    for l in git(repo, "log", "--first-parent", "--merges", "--format=%P%x09%s", *rng).splitlines():
        parents, _, subj = l.partition("\t")
        m = MERGED_RE.match(subj.strip())
        ps = parents.split()
        ref = m.group(1) if m else ""
        if not ref or ref in out or ref in (base, upstream) or len(ps) < 2:
            continue
        if not git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/{ref}"):
            continue
        if base and git_run(repo, "merge-base", "--is-ancestor", f"refs/remotes/{ref}", base)[0] == 0:
            continue
        out[ref] = ps[1]
    return out


def merged_refs(repo):
    """Ветки с сервера, которые уже вливали в текущую (по сообщениям merge-коммитов)."""
    return list(merged_tips(repo))


def blob_text(repo, rev, path):
    r = subprocess.run(["git", "-C", repo, "show", f"{rev}:{path}"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout.decode("utf-8-sig", "replace") if r.returncode == 0 else None


def find_block(lines, blk, before=None):
    """Индекс, с которого в lines идет блок blk; при нескольких вхождениях - то, перед которым строка before."""
    hits = [i for i in range(len(lines) - len(blk) + 1) if lines[i:i + len(blk)] == blk]
    for i in hits:
        if before is not None and i > 0 and lines[i - 1] == before:
            return i
    return hits[0] if hits else None


def carry_patch(cur, old, new):
    """Перенести построчную правку old -> new на текст cur (блоки ищутся по содержимому)."""
    res = list(cur)
    ops = difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    missed = 0
    for tag, i1, i2, j1, j2 in reversed(ops):
        if tag == "equal":
            continue
        blk, rep = old[i1:i2], new[j1:j2]
        before = old[i1 - 1] if i1 else None
        if blk:
            pos = find_block(res, blk, before)
            if pos is None and not rep:
                # блок разорван своими строками: убираем строки по одной, заголовки - только опустевшие
                ind = lambda s: len(s) - len(s.lstrip(" -"))
                heads = [h for h in blk if h.rstrip().endswith(":")]
                for h in blk:
                    if h in heads or not h.strip():
                        continue
                    hits = [i for i, l in enumerate(res) if l == h]
                    if len(hits) != 1:
                        missed += 1
                        continue
                    del res[hits[0]]
                for h in reversed(heads):
                    hits = [i for i, l in enumerate(res) if l == h]
                    if len(hits) != 1:
                        continue
                    i = hits[0]
                    if not (i + 1 < len(res) and res[i + 1].strip() and ind(res[i + 1]) > ind(h)):
                        del res[i]
                continue
            if pos is None:
                missed += 1
                continue
            if not rep:
                # заголовок раздела остается, если в нем есть строки, которых нет в убираемом блоке (свои страницы)
                ind = lambda s: len(s) - len(s.lstrip(" -"))
                nxt = res[pos + len(blk)] if pos + len(blk) < len(res) else None
                rep = [h for k, h in enumerate(blk) if h.rstrip().endswith(":") and nxt is not None and nxt.strip()
                       and ind(nxt) > ind(h) and all(ind(x) > ind(h) for x in blk[k + 1:] if x.strip())]
            res[pos:pos + len(blk)] = rep
        else:
            # вставка: после строки, которая стояла перед этим местом
            if before is None:
                res[0:0] = rep
                continue
            after = old[i1] if i1 < len(old) else None
            cand = [i for i, l in enumerate(res) if l == before]
            pos = next((i for i in cand if after is None or (i + 1 < len(res) and res[i + 1] == after)), cand[0] if cand else None)
            if pos is None:
                missed += 1
                continue
            res[pos + 1:pos + 1] = rep
    return res, missed


def write_tree_with(repo, tree, fix):
    """Новый tree: tree с заменой файлов (path -> blob или None для удаления)."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(d, "index"))
        kw = dict(capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.run(["git", "-C", repo, "read-tree", tree], check=True, **kw)
        for path, blob in fix.items():
            if blob is None:
                subprocess.run(["git", "-C", repo, "update-index", "--force-remove", "--", path], check=True, **kw)
            else:
                subprocess.run(["git", "-C", repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"], check=True, **kw)
        return subprocess.run(["git", "-C", repo, "write-tree"], check=True, **kw).stdout.strip()


def hash_text(repo, text):
    r = subprocess.run(["git", "-C", repo, "hash-object", "-w", "--stdin"], input=text.encode("utf-8"), capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout.decode().strip()


def clean_tree(repo, yname):
    """Состояние HEAD без изменений доп. веток: (tree, fork, тронутые чужие файлы, доп. ветки)."""
    tips = merged_tips(repo)
    base = base_ref(repo)
    fork = git(repo, "merge-base", "HEAD", base)
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    raw = blob_text(repo, "HEAD", yname) or ""
    nl = "\r\n" if "\r\n" in raw else "\n"
    yml = raw.replace("\r\n", "\n")
    touched = []
    for ref, tip in tips.items():
        mb = git(repo, "merge-base", tip, fork)
        code, out = git_run(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages",
                            f"--merge-base={tip}", tree, mb, timeout=60)
        rows = [l for l in out.splitlines() if l.strip()]
        if code not in (0, 1) or not rows or not re.fullmatch(r"[0-9a-f]{40,64}", rows[0]):
            raise ValueError("Не смог отделить изменения ветки " + ref + ":\n" + out)
        fix = {}
        for c in rows[1:]:
            if c == yname:
                continue
            # файл из доп. ветки, который ты тоже правил: остается в твоей версии
            blob = git(repo, "rev-parse", "--verify", "--quiet", f"{tree}:{c}")
            fix[c] = blob or None
            touched.append(c)
        y_old = (blob_text(repo, tip, yname) or "").replace("\r\n", "\n")
        y_new = (blob_text(repo, mb, yname) or "").replace("\r\n", "\n")
        if y_old != y_new:
            lines, missed = carry_patch(yml.split("\n"), y_old.split("\n"), y_new.split("\n"))
            yml = "\n".join(lines)
        fix[yname] = hash_text(repo, yml.replace("\n", nl))
        tree = write_tree_with(repo, rows[0], fix)
    # проверка: mkdocs.yml цел и не ссылается на страницы, которых в отправке нет
    lines = yml.split("\n")
    if yaml_error(yml):
        raise ValueError(f"После отделения доп. веток {yname} получился с ошибкой, отправка отменена")
    nodes, _, _ = parse_nav(lines)
    docs = docs_dir_of(lines).strip("/")
    have = set(git(repo, "ls-tree", "-r", "--name-only", tree, "--", docs + "/").splitlines())
    lost = sorted({n["path"] for n in nodes if n.get("path") and not re.match(r"^[a-z]+://", n["path"])
                   and f"{docs}/{n['path']}" not in have})
    if lost:
        raise ValueError("Не смог чисто отделить доп. ветки: в меню остались страницы, которых не будет в отправке:\n"
                         + "\n".join(lost[:15]) + "\nОтправка отменена, ничего не изменилось.")
    return tree, fork, sorted(set(touched)), list(tips)


def published(repo, branch):
    return load_config().get("published", {}).get(f"{repo}|{branch}")


def api_push(repo, dry=False):
    """Push. Если в ветку влиты доп. ветки, на сервер уходят только свои изменения:
    отдельный коммит с тем же содержимым, но без изменений доп. веток. Локальная ветка не меняется."""
    branch = git(repo, "branch", "--show-current")
    tips = merged_tips(repo) if branch else {}
    if not tips:
        if dry:
            return {"clean": False}
        code, out = git_run(repo, "push", "-u", "origin", "HEAD", timeout=180)
        if code:
            raise ValueError(out or "git завершился с ошибкой")
        return {"message": "Запушено", "output": out}
    yname = yml_path(repo).name
    tree, fork, touched, refs = clean_tree(repo, yname)
    upstream = git(repo, "rev-parse", "--abbrev-ref", "@{u}")
    remote = git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
    # если доп. ветки уже уходили на сервер, ветку на сервере откатываем до последнего коммита без них
    on_top = remote
    if remote:
        extra = extra_parents(repo)
        on_top = None
        for c in git(repo, "rev-list", "--first-parent", "-300", remote).splitlines():
            if not any(git_run(repo, "merge-base", "--is-ancestor", p, c)[0] == 0 for p in extra):
                on_top = c
                break
    rewrite = bool(remote) and on_top != remote
    parents = [on_top] if on_top else []
    if not on_top or git_run(repo, "merge-base", "--is-ancestor", fork, on_top)[0] != 0:
        parents.append(fork)  # master, влитый в ветку, уходит как обычное вливание master
    files = [l for l in git(repo, "diff", "--name-status", fork, tree).splitlines() if l.strip()]
    same = bool(on_top) and len(parents) == 1 and git(repo, "rev-parse", f"{on_top}^{{tree}}") == tree
    nothing = same and not rewrite
    if dry:
        return {"clean": True, "refs": [r.split("/", 1)[1] for r in refs], "files": files, "touched": touched,
                "nothing": nothing, "rewrite": rewrite}
    rec = published(repo, branch)
    if nothing:
        cfg = load_config()
        cfg.setdefault("published", {})[f"{repo}|{branch}"] = {"head": git(repo, "rev-parse", "HEAD"), "sha": remote}
        save_config(cfg)
        return {"message": "Отправлять нечего: на сервере уже все твои изменения", "output": ""}
    if same:
        sha = on_top
    else:
        since = rec["head"] if rec and git_run(repo, "merge-base", "--is-ancestor", rec["head"], "HEAD")[0] == 0 else (on_top or fork)
        subj = []
        for s in git(repo, "log", "--first-parent", "--no-merges", "--format=%s", f"{since}..HEAD").splitlines():
            if s.strip() and s not in subj:
                subj.append(s)
        msg = subj[0] if subj else f"{branch} правки"
        if len(subj) > 1:
            msg += "\n\n" + "\n".join(reversed(subj))
        code, sha = git_run(repo, "commit-tree", tree, *sum((["-p", p] for p in parents), []), "-m", msg)
        if code or not re.fullmatch(r"[0-9a-f]{40,64}", sha.strip()):
            raise ValueError("Не смог собрать коммит для отправки:\n" + sha)
        sha = sha.strip()
    args = ["push", "origin", f"{sha}:refs/heads/{branch}"]
    if rewrite:
        args.insert(1, f"--force-with-lease=refs/heads/{branch}:{remote}")
    code, out = git_run(repo, *args, timeout=180)
    if code:
        raise ValueError(out or "git завершился с ошибкой")
    git_run(repo, "fetch", "origin", branch, timeout=60)
    if not upstream:
        git_run(repo, "branch", f"--set-upstream-to=origin/{branch}")
    cfg = load_config()
    cfg.setdefault("published", {})[f"{repo}|{branch}"] = {"head": git(repo, "rev-parse", "HEAD"), "sha": sha}
    save_config(cfg)
    short = ", ".join(r.split("/", 1)[1] for r in refs)
    return {"message": f"Запушено только твое, без доп. веток ({short})"
                       + (". Ветка на сервере перезаписана: доп. ветки из нее убраны" if rewrite else ""),
            "output": out, "touched": touched}


def extra_parents(repo):
    """Все коммиты доп. веток, которые когда-либо вливались в текущую (вторые родители merge-коммитов)."""
    tips = merged_tips(repo)
    out = set(tips.values())
    base = base_ref(repo)
    rng = [f"{base}..HEAD"] if base else ["-200", "HEAD"]
    for l in git(repo, "log", "--first-parent", "--merges", "--format=%P%x09%s", *rng).splitlines():
        parents, _, subj = l.partition("\t")
        m = MERGED_RE.match(subj.strip())
        ps = parents.split()
        if m and m.group(1) in tips and len(ps) > 1:
            out.add(ps[1])
    return out


def api_merge_base(repo, union=False):
    base = base_ref(repo)
    if not base:
        raise ValueError("Не нашел основную ветку (origin/master)")
    return merge_with_union(repo, base, f"Свежий {base} влит в ветку", union)


ORIGIN_CACHE = {}


def file_origins(repo):
    """Откуда страница, которой еще нет в master: из ветки коллеги, из своей ветки или еще не закоммичена."""
    if git(repo, "rev-parse", "--is-inside-work-tree") != "true":
        return {}
    _, docs_name = docs_root(repo)
    pre = docs_name + "/"
    base = base_ref(repo)
    cur = git(repo, "branch", "--show-current")
    out = {}
    if base:
        key = (repo, git(repo, "rev-parse", "HEAD"), git(repo, "for-each-ref", "refs/remotes", "--format=%(objectname)"))
        if key not in ORIGIN_CACHE:
            upstream = git(repo, "rev-parse", "--abbrev-ref", "@{u}")
            own = set(git(repo, "rev-list", "--first-parent", f"{base}..HEAD").split())
            res, by_sha = {}, {}
            for l in git(repo, "log", "--format=@%H", "--name-only", "--diff-filter=A", f"{base}..HEAD", "--", docs_name).splitlines():
                if l.startswith("@"):
                    sha = l[1:]
                elif l.strip().startswith(pre) and l.strip()[len(pre):] not in res:
                    rel = l.strip()[len(pre):]
                    if sha in own:
                        res[rel] = {"kind": "mine", "branch": cur}
                        continue
                    if sha not in by_sha:
                        refs = git(repo, "branch", "-r", "--contains", sha, "--format=%(refname:short)").splitlines()
                        refs = [r.strip() for r in refs if r.strip() and "/" in r and not r.strip().endswith("/HEAD")
                                and r.strip() not in (upstream, base)]
                        by_sha[sha] = refs[0].split("/", 1)[1] if refs else ""
                    res[rel] = {"kind": "other", "branch": by_sha[sha]} if by_sha[sha] else {"kind": "mine", "branch": cur}
            ORIGIN_CACHE.clear()
            ORIGIN_CACHE[key] = res
        out.update(ORIGIN_CACHE[key])
    _, st = git_run(repo, "status", "--porcelain", "-uall")
    for l in st.splitlines():
        f = l[3:].strip().strip('"')
        if l.startswith("??") and f.startswith(pre):
            out[f[len(pre):]] = {"kind": "new"}
    return out


def api_merge_candidates(repo):
    """Ветки с сервера, которые можно влить в текущую: свежий список, сколько коммитов придет, кто правил."""
    git_run(repo, "fetch", "--all", "--prune", timeout=120)
    cur = git(repo, "branch", "--show-current")
    upstream = git(repo, "rev-parse", "--abbrev-ref", "@{u}")
    fmt = "%(refname:short)%09%(committerdate:short)%09%(authorname)%09%(contents:subject)"
    out = []
    for l in git(repo, "for-each-ref", "--sort=-committerdate", "--format=" + fmt, "refs/remotes").splitlines():
        parts = l.split("\t", 3)
        if len(parts) < 4:
            continue
        ref, date, author, subject = parts
        if ref.endswith("/HEAD") or "/" not in ref or ref == upstream:
            continue
        n = git(repo, "rev-list", "--count", f"HEAD..{ref}")
        out.append({"ref": ref, "name": ref.split("/", 1)[1], "date": date, "author": author, "subject": subject,
                    "new": int(n) if n.isdigit() else 0})
    return {"current": cur, "branches": out}


def api_merge_preview(repo, ref):
    if not git(repo, "rev-parse", "--verify", "--quiet", ref):
        raise ValueError("Ветка не найдена: " + ref)
    commits = git(repo, "log", "--format=%h  %an  %s", f"HEAD..{ref}").splitlines()
    files = git(repo, "diff", "--name-status", f"HEAD...{ref}").splitlines()
    clash = sorted(dirty_files(repo) & {l.split("\t")[-1] for l in files})
    return {"ref": ref, "commits": commits[:50], "count": len(commits), "files": files[:200], "clash": clash}


def api_merge_branch(repo, ref, union=False):
    """Влить ветку коллеги в свою: защита от пересечения с незакоммиченными правками, при конфликте отмена."""
    cur = git(repo, "branch", "--show-current")
    if cur in ("master", "main"):
        raise ValueError(f"Ты на {cur}. Чужую неслитую ветку вливают в свою ветку задачи: сначала создай или выбери ее")
    if not git(repo, "rev-parse", "--verify", "--quiet", ref):
        raise ValueError("Ветка не найдена: " + ref)
    return merge_with_union(repo, ref, f"Ветка {ref} влита в {cur}", union)


def union_yml_text(repo, name):
    """Конфликт только в mkdocs.yml: объединяем пункты меню обеих сторон (git merge-file --union).
    Возвращает текст, если получился правильный mkdocs.yml, иначе None."""
    import tempfile
    blobs = {}
    for st in ("1", "2", "3"):
        r = subprocess.run(["git", "-C", repo, "show", f":{st}:{name}"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        blobs[st] = r.stdout if r.returncode == 0 else b""
    if not blobs["2"] or not blobs["3"]:
        return None
    with tempfile.TemporaryDirectory() as d:
        paths = {}
        for st, data in blobs.items():
            paths[st] = os.path.join(d, st + ".yml")
            with open(paths[st], "wb") as f:
                f.write(data)
        code, _ = git_run(repo, "merge-file", "--union", paths["2"], paths["1"], paths["3"])
        if code < 0:
            return None
        with open(paths["2"], "rb") as f:
            text = f.read().decode("utf-8-sig", "replace").replace("\r\n", "\n")
    try:
        parse_nav(text.split("\n"))
    except Exception:
        return None
    if yaml_error(text):
        return None
    return text


def merge_with_union(repo, ref, done_msg, union):
    """git merge; при конфликте только в mkdocs.yml предлагаем (или делаем, если union) объединение меню."""
    overlap_guard(repo, ref, "влить ветку")
    name = yml_path(repo).name
    _, nl, bom, _ = read_yml(repo)
    code, out = git_run(repo, "merge", "--no-edit", "--autostash", ref, timeout=120)
    if not code:
        return {"message": done_msg, "output": out}
    conflicts = [c for c in git(repo, "diff", "--name-only", "--diff-filter=U").splitlines() if c]
    text = union_yml_text(repo, name) if conflicts == [name] else None
    if union and text is not None:
        data = text.replace("\n", nl).encode("utf-8")
        yml_path(repo).write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
        git_run(repo, "add", "--", name)
        code2, out2 = git_run(repo, "-c", "core.editor=true", "merge", "--continue", timeout=60)
        if code2:
            git_run(repo, "merge", "--abort")
            raise ValueError("Не получилось завершить слияние, отменил его:\n" + out2)
        return {"message": done_msg + ". Меню объединено: оставлены пункты обоих", "output": out2}
    preview = None
    if text is not None:
        ours = subprocess.run(["git", "-C", repo, "show", f":2:{name}"], capture_output=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        ours = ours.decode("utf-8-sig", "replace").replace("\r\n", "\n").split("\n")
        preview = "\n".join(list(difflib.unified_diff(ours, text.split("\n"), name, name, n=2, lineterm=""))[2:])
    git_run(repo, "merge", "--abort")
    if text is not None:
        return {"conflict": True, "union_ok": True, "diff": preview, "files": conflicts, "ref": ref,
                "message": f"Конфликт только в {name}: можно оставить пункты меню обоих"}
    raise ValueError(f"Слияние с {ref} дало конфликты, отменил его, ветка осталась как была:\n"
                     + ("\n".join(conflicts) or out) + "\nТакое лучше решать вручную или договориться с автором ветки.")


def git_branches(repo):
    cur = git(repo, "branch", "--show-current")
    fmt = "--format=%(refname:short)\t%(committerdate:short)"
    local = [l.split("\t") for l in git(repo, "branch", fmt).splitlines() if l.strip()]
    remote = [l.split("\t") for l in git(repo, "branch", "-r", fmt).splitlines()
              if l.strip() and not l.split("\t")[0].endswith("/HEAD") and "/" in l.split("\t")[0]]
    names = {n for n, *_ in local}
    out = [{"name": n, "date": (d[0] if d else ""), "local": True, "current": n == cur} for n, *d in local]
    seen = set()
    for full, *d in remote:
        short = full.split("/", 1)[1]
        if short in names or short in seen:
            continue
        seen.add(short)
        out.append({"name": short, "remote": full, "date": (d[0] if d else ""), "local": False, "current": False})
    return {"current": cur, "branches": out}


def api_git(req):
    repo = req["repo"]
    op = req["op"]
    if op == "branches":
        return git_branches(repo)
    if op == "switch":
        name = (req.get("name") or "").strip()
        info = git_branches(repo)
        b = next((x for x in info["branches"] if x["name"] == name), None)
        if not b:
            raise ValueError(f"Ветки «{name}» нет. Нажми «Скачать обновления», чтобы подтянуть ветки с сервера")
        if b["current"]:
            return {"message": f"Ты уже на ветке {name}", "output": ""}
        args = ["switch", name] if b["local"] else ["switch", "--track", b["remote"]]
        # что сделать с незакоммиченными правками, если они мешают: отложить, закоммитить или сбросить
        mode, cur, pre, stashed = req.get("mode"), info["current"], "", False
        if mode == "stash":
            before = git(repo, "rev-parse", "--verify", "--quiet", "refs/stash")
            code, out = git_run(repo, "stash", "push", "-u", "-m", f"navtool: правки с ветки {cur}")
            if code:
                raise ValueError("Не получилось отложить правки:\n" + out)
            # на чистом дереве stash push ничего не откладывает: тогда и возвращать нечего
            stashed = git(repo, "rev-parse", "--verify", "--quiet", "refs/stash") != before
            pre = "Правки отложены. " if stashed else ""
        elif mode == "commit":
            message = (req.get("message") or "").strip() or f"Правки перед переключением на {name}"
            lines, _, _, _ = read_yml(repo)
            git_run(repo, "add", "--", yml_path(repo).name, docs_dir_of(lines))
            code, out = git_run(repo, "commit", "-m", message)
            if code:
                raise ValueError("Не получилось закоммитить:\n" + out)
            pre = f"Правки закоммичены в {cur}. "
        elif mode == "discard":
            api_discard({"repo": repo, "paths": "all"})
            pre = "Правки сброшены (вернуть можно кнопкой «Отменить»). "
        code, out = git_run(repo, *args, timeout=60)
        if code:
            if stashed:
                git_run(repo, "stash", "pop")
            if "would be overwritten" in out or "перезапис" in out:
                files = [l.strip() for l in out.splitlines() if l.startswith(("\t", "    "))]
                return {"conflict": True, "files": files, "output": out,
                        "message": "Незакоммиченные правки мешают переключить ветку"}
            raise ValueError("Git не переключил ветку:\n\n" + out)
        return {"message": f"{pre}Переключено на ветку {name}", "output": out}
    if op == "stash_pop":
        ref = req.get("ref", "")
        if not re.match(r"^stash@\{\d+\}$", ref):
            raise ValueError("Неверная ссылка на отложенные правки")
        code, files = git_run(repo, "stash", "show", "--include-untracked", "--name-only", ref)
        clash = sorted(dirty_files(repo) & set(files.splitlines())) if code == 0 else []
        if clash:
            raise ValueError("Сейчас в этих файлах есть свои незакоммиченные правки, вернуть поверх нельзя:\n"
                             + "\n".join(clash) + "\nЗакоммить или сбрось их и повтори.")
        # заранее проверяем, что правки ложатся чисто: иначе git оставит в файлах метки конфликта
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        nw = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        patch = subprocess.run(["git", "-C", repo, "stash", "show", "-p", "--include-untracked", ref],
                               capture_output=True, env=env, creationflags=nw).stdout
        chk = subprocess.run(["git", "-C", repo, "apply", "--check", "-"], input=patch, capture_output=True,
                             env=env, creationflags=nw)
        if chk.returncode:
            msgs = [l for l in git(repo, "stash", "list", "--format=%gd%x09%s").splitlines() if l.startswith(ref + "\t")]
            src = msgs[0].split("\t", 1)[1] if msgs else ""
            raise ValueError("Эти правки не ложатся на текущую ветку: файлы здесь другие. "
                             "Переключись на ветку, с которой их откладывали, и верни там"
                             + (f" ({src.replace('On ', '').split(':')[0]})" if src else "") + ".")
        code, out = git_run(repo, "stash", "pop", ref)
        if code:
            raise ValueError("Git не вернул правки:\n" + out)
        return {"message": "Отложенные правки возвращены", "output": out}
    if op == "stash_drop":
        ref = req.get("ref", "")
        if not re.match(r"^stash@\{\d+\}$", ref):
            raise ValueError("Неверная ссылка на отложенные правки")
        code, out = git_run(repo, "stash", "drop", ref)
        if code:
            raise ValueError(out)
        return {"message": "Отложенные правки удалены", "output": out}
    if op == "sync":
        return api_sync(repo)
    if op == "merge_base":
        return api_merge_base(repo, bool(req.get("union")))
    if op == "merge_candidates":
        return api_merge_candidates(repo)
    if op == "merge_preview":
        return api_merge_preview(repo, req.get("ref", ""))
    if op == "merge_branch":
        return api_merge_branch(repo, req.get("ref", ""), bool(req.get("union")))
    if op == "branch":
        name = (req.get("name") or "").strip()
        # имя проверяет сам git: те же правила, что при git switch -c
        if not name or git_run(repo, "check-ref-format", "--branch", name)[0] != 0:
            raise ValueError("Недопустимое имя ветки: " + name + ". Нельзя пробелы, ~ ^ : ? * [ \\, две точки подряд, "
                             "начинать с - и заканчивать на / или .lock")
        # новая ветка задачи всегда от свежего master, а не от текущей ветки;
        # в форке основа - upstream/master, его тоже надо скачать
        fetched = git_run(repo, "fetch", "origin", timeout=60)[0] == 0
        base = base_ref(repo)
        if base and not base.startswith("origin/"):
            fetched = git_run(repo, "fetch", base.split("/", 1)[0], timeout=60)[0] == 0 and fetched
        if git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}"):
            raise ValueError(f"Ветка {name} уже есть на сервере: выбери ее в списке и нажми «Переключить»")
        if git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            raise ValueError(f"Ветка {name} уже есть на этом компьютере: выбери ее в списке и нажми «Переключить»")
        code, out = git_run(repo, "switch", "-c", name, "--no-track", *([base] if base else []))
        if code and base:
            raise ValueError("Не получилось создать ветку от " + base + ": незакоммиченные правки мешают. "
                             "Закоммить или отложи их и повтори.\n" + out)
        if not base:
            msg = f"Создана ветка {name}"
        elif fetched:
            msg = f"Создана ветка {name} от свежего {base}"
        else:
            msg = f"Создана ветка {name} от {base}. Скачать обновления с сервера не получилось, основа может быть не самой свежей"
    elif op == "commit":
        message = (req.get("message") or "").strip()
        if not message:
            raise ValueError("Напиши сообщение коммита")
        lines, _, _, _ = read_yml(repo)
        yml_name, docs_name = yml_path(repo).name, docs_dir_of(lines).strip("/")
        files = req.get("files")
        if files is None:
            code, out = git_run(repo, "add", "--", yml_name, docs_name)
            if code:
                raise ValueError(out)
            code, out = git_run(repo, "commit", "-m", message)
        else:
            # только отмеченные файлы; остальные правки остаются незакоммиченными
            files = [str(f).replace("\\", "/").strip() for f in files if str(f).strip()]
            bad = [f for f in files if ".." in f.split("/") or not (f == yml_name or f.startswith(docs_name + "/"))]
            if bad:
                raise ValueError("В коммит можно взять только " + yml_name + " и файлы " + docs_name + "/: " + ", ".join(bad))
            if not files:
                raise ValueError("Отметь хотя бы один файл для коммита")
            code, out = git_run(repo, "add", "-A", "--", *files)
            if code:
                raise ValueError(out)
            code, out = git_run(repo, "commit", "-m", message, "--", *files)
        msg = "Коммит создан"
    elif op == "push_preview":
        return api_push(repo, dry=True)
    elif op == "push":
        r = api_push(repo)
        mr = re.search(r"https?://\S+merge_requests/new\S*", r.get("output", ""))
        return {**r, "mr": mr.group(0) if mr else None}
    else:
        raise ValueError("Неизвестная git-операция")
    if code:
        raise ValueError(out or "git завершился с ошибкой")
    mr = re.search(r"https?://\S+merge_requests/new\S*", out)
    return {"message": msg, "output": out, "mr": mr.group(0) if mr else None}


# ---------- откат ----------

def push_undo(repo, label, file=None, file_existed=False):
    BACKUPS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10**6:06d}"
    yb = BACKUPS / f"mkdocs-{stamp}.yml"
    shutil.copy2(yml_path(repo), yb)
    entry = {"repo": repo, "label": label, "yml": str(yb), "file": None, "file_backup": None,
             "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD")}
    if file:
        entry["file"] = str(file)
        if file_existed:
            fb = BACKUPS / f"file-{stamp}-{Path(file).name}"
            shutil.copy2(file, fb)
            entry["file_backup"] = str(fb)
    cfg = load_config()
    stack = cfg.get("undo") if isinstance(cfg.get("undo"), list) else []
    stack.append(entry)
    for old in stack[:-UNDO_DEPTH]:
        for k in ("yml", "file_backup"):
            if old.get(k):
                Path(old[k]).unlink(missing_ok=True)
        if old.get("trash"):
            shutil.rmtree(old["trash"], ignore_errors=True)
    cfg["undo"] = stack[-UNDO_DEPTH:]
    save_config(cfg)


def undo_list(repo):
    stack = load_config().get("undo")
    if not isinstance(stack, list):
        return []
    return [e["label"] for e in stack if e["repo"] == repo]


def copy_any(src, dst):
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if Path(src).is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)


def remove_any(p):
    p = Path(p)
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()


def api_discard(req):
    """Отменить незакоммиченные изменения. Текущее состояние файлов уходит в корзину инструмента, откат возможен."""
    repo = req["repo"]
    _, out = git_run(repo, "status", "--porcelain")
    entries = []
    for l in out.splitlines():
        if len(l) < 4:
            continue
        code, rest = l[:2], l[3:]
        old, _, new = rest.partition(" -> ")
        entries.append((code, old.strip('"'), new.strip('"') if new else None))
    want = req.get("paths")
    if want != "all":
        want = set(want or [])
        entries = [e for e in entries if e[1] in want or e[2] in want]
    if not entries:
        raise ValueError("Нечего отменять")
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10**6:06d}"
    trash = BACKUPS / f"discard-{stamp}"
    items = []

    def save(rel):
        p = Path(repo) / rel
        if p.exists():
            copy_any(p, trash / rel)
            items.append({"path": str(p), "backup": str(trash / rel)})
        else:
            items.append({"path": str(p), "backup": None})

    for code, a, b in entries:
        for rel in (a, b):
            if rel:
                save(rel)
    for code, a, b in entries:
        if code == "??":
            remove_any(Path(repo) / a)
            continue
        paths = [x for x in (a, b) if x]
        git_run(repo, "reset", "-q", "--", *paths)
        for rel in paths:
            in_head = git_run(repo, "cat-file", "-e", f"HEAD:{rel}")[0] == 0
            if in_head:
                c, o = git_run(repo, "restore", "--source=HEAD", "--worktree", "--", rel)
                if c:
                    raise ValueError(o)
            else:
                remove_any(Path(repo) / rel)
    names = ", ".join(sorted({x for e in entries for x in e[1:] if x}))
    cfg = load_config()
    stack = cfg.get("undo") if isinstance(cfg.get("undo"), list) else []
    stack.append({"repo": repo, "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "label": f"сброс изменений: {names}"[:200], "kind": "files", "items": items,
                  "trash": str(trash)})
    cfg["undo"] = stack[-UNDO_DEPTH:]
    save_config(cfg)
    return {"message": f"Изменения отменены ({len(entries)}). Вернуть можно кнопкой «Отменить»"}


def api_undo(repo):
    cfg = load_config()
    stack = cfg.get("undo") if isinstance(cfg.get("undo"), list) else []
    idx = next((i for i in range(len(stack) - 1, -1, -1) if stack[i]["repo"] == repo), None)
    if idx is None:
        raise ValueError("Нечего откатывать")
    e = stack[idx]
    # снимок mkdocs.yml снят на другой ветке: копия затерла бы чужое меню
    if e.get("branch") and git(repo, "rev-parse", "--abbrev-ref", "HEAD") not in ("", e["branch"]):
        raise ValueError(f"«{e['label']}» сделано на ветке {e['branch']}. Переключись на нее, чтобы отменить")
    stack.pop(idx)
    need = [it["backup"] for it in e.get("items", []) + e.get("files", []) if it.get("backup")]
    need += [e[k] for k in ("yml", "file_backup") if e.get(k)]
    if e.get("kind") == "move":
        need.append(e["from"])
    lost = [p for p in need if not Path(p).exists()]
    if lost:
        # копия пропала: ничего не трогаем, запись убираем, чтобы не мешала следующим откатам
        cfg["undo"] = stack
        save_config(cfg)
        raise ValueError(f"Не могу отменить «{e['label']}»: копия в папке backups не найдена. "
                         "Запись убрана, следующие откаты работают как обычно.")
    if e.get("kind") == "move":
        # возвращаем на место, затем исходные тексты правленых файлов и mkdocs.yml
        if Path(e["to"]).exists():
            cfg["undo"] = stack
            save_config(cfg)
            raise ValueError("Не могу отменить перенос: на старом месте уже что-то лежит")
        shutil.move(e["from"], e["to"])
        for it in e.get("files", []):
            shutil.copy2(it["backup"], it["orig"])
        shutil.copy2(e["yml"], yml_path(repo))
        shutil.rmtree(e.get("trash", ""), ignore_errors=True)
        cfg["undo"] = stack
        save_config(cfg)
        return {"message": "Отменено: " + e["label"]}
    if e.get("kind") == "files":
        for it in e["items"]:
            remove_any(it["path"])
            if it["backup"]:
                copy_any(it["backup"], it["path"])
        if e.get("yml"):
            shutil.copy2(e["yml"], yml_path(repo))
        shutil.rmtree(e.get("trash", ""), ignore_errors=True)
        cfg["undo"] = stack
        save_config(cfg)
        return {"message": "Отменено: " + e["label"]}
    shutil.copy2(e["yml"], yml_path(repo))
    Path(e["yml"]).unlink(missing_ok=True)
    if e.get("file"):
        if e.get("file_backup"):
            shutil.copy2(e["file_backup"], e["file"])
            Path(e["file_backup"]).unlink(missing_ok=True)
        elif Path(e["file"]).is_file():
            Path(e["file"]).unlink()
    cfg["undo"] = stack
    save_config(cfg)
    return {"message": "Отменено: " + e["label"]}


# ---------- API ----------

def api_browse(path):
    home = Path.home()
    places = [{"name": n, "path": str(p)} for n, p in
              (("Рабочий стол", home / "Desktop"), ("Документы", home / "Documents"),
               ("Загрузки", home / "Downloads"), ("Домашняя папка", home)) if p.is_dir()]
    drives = [f"{c}:\\" for c in string.ascii_uppercase if c not in "AB" and os.path.exists(f"{c}:\\")]
    p = Path(path) if path else (home / "Desktop" if (home / "Desktop").is_dir() else home)
    if not p.is_dir():
        raise ValueError("Папка не найдена: " + str(p))
    dirs = []
    try:
        entries = sorted(os.scandir(p), key=lambda e: e.name.lower())
    except OSError:
        entries = []
    for e in entries:
        try:
            if e.is_dir() and not e.name.startswith((".", "$")) and e.name not in SKIP_DIRS:
                dirs.append({"name": e.name, "path": e.path, "mkdocs": yml_path(e.path) is not None})
        except OSError:
            pass
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "dirs": dirs, "drives": drives, "places": places,
            "here": yml_path(p) is not None}


def api_home():
    cfg = load_config()
    recent = [r for r in cfg.get("recent", []) if yml_path(r)]
    found = [r for r in find_repos() if r not in recent]
    return {"recent": [repo_card(r) for r in recent], "found": [repo_card(r) for r in found],
            "last": cfg.get("last")}


def api_state(repo):
    lines, _, _, sha = read_yml(repo)
    nodes, _, _ = parse_nav(lines)
    docs_name = docs_dir_of(lines)
    docs = Path(repo) / docs_name
    files, dirs = scan_docs(docs) if docs.is_dir() else ([], [])
    in_nav = {}
    for n in nodes:
        if n["path"]:
            in_nav.setdefault(n["path"], []).append(breadcrumb(nodes, n["parent"]))
    for f in files:
        f["in_nav"] = in_nav.get(f["path"], [])
    missing = [{"id": n["id"], "path": n["path"], "where": breadcrumb(nodes, n["parent"]), "title": n["title"]}
               for n in nodes if is_local_md(n["path"]) and not (docs / n["path"]).is_file()]
    out_nodes = [{"id": n["id"], "parent": n["parent"], "title": n["title"], "path": n["path"],
                  "children": n["children"], "hint": None if n["path"] else dir_hint(nodes, n["id"])}
                 for n in nodes]
    return {"repo": repo, "name": site_name(repo) or Path(repo).name, "yml": yml_path(repo).name,
            "sha": sha, "docs_dir": docs_name, "nodes": out_nodes, "files": files, "dirs": dirs,
            "in_nav": in_nav, "missing": missing, "git": git_state(repo), "undo": undo_list(repo),
            "yml_error": yml_error_info(lines), "origins": file_origins(repo)}


def yml_error_info(lines):
    err = yaml_error("\n".join(lines))
    if not err:
        return None
    ln = err.get("line") or 1
    show = set(range(max(1, ln - 3), min(len(lines), ln + 2) + 1))
    ctx = err.get("ctx_line")
    if ctx and 1 <= ctx <= len(lines):
        show.add(ctx)
    snippet, prev = [], None
    for n in sorted(show):
        if prev is not None and n > prev + 1:
            snippet.append({"n": "...", "s": ""})
        snippet.append({"n": n, "s": lines[n - 1]})
        prev = n
    fix = yaml_fix(lines, err)
    return {"line": ln, "col": err.get("col"), "ctx_line": err.get("ctx_line"),
            "text": describe_yaml_error(err), "raw": err.get("problem", ""),
            "fix": {"label": fix[1], "line": fix[2]} if fix else None,
            "snippet": snippet}


def api_fix_yml(req):
    repo = req["repo"]
    lines, nl, bom, sha = read_yml(repo)
    check_sha(req, sha)
    err = yaml_error("\n".join(lines))
    fix = yaml_fix(lines, err) if err else None
    if not fix:
        raise ValueError("Автоисправления для этой ошибки нет, поправь в Блокноте")
    push_undo(repo, "исправление mkdocs.yml: " + fix[1])
    write_yml(repo, fix[0], nl, bom)
    return {"message": "Исправлено: " + fix[1]}


def api_open_yml(repo):
    p = yml_path(repo)
    if not p:
        raise ValueError("mkdocs.yml не найден")
    if os.name == "nt":
        subprocess.Popen(["notepad.exe", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])
    return {"message": "Открыл mkdocs.yml в Блокноте. Исправь, сохрани и нажми обновить"}


def check_sha(req, sha):
    if req.get("sha") and req["sha"] != sha:
        raise ValueError("mkdocs.yml изменился снаружи, обнови страницу (кнопка обновления сверху)")


def build_add(req, apply=False):
    repo = req["repo"]
    lines, nl, bom, sha = read_yml(repo)
    check_sha(req, sha)
    nodes, _, _ = parse_nav(lines)
    docs_name = docs_dir_of(lines)
    docs = Path(repo) / docs_name
    targets = req.get("targets", [])
    if not targets:
        raise ValueError("Отметь хотя бы один раздел")
    title = (req.get("title") or "").strip()
    if not title:
        raise ValueError("Пустой заголовок страницы")

    file_note = None
    upload = req.get("upload")
    dest, existed = None, False
    if upload:
        fname = safe_rel(upload["filename"]).split("/")[-1].replace(" ", "-")
        if not fname.lower().endswith(".md"):
            raise ValueError("Нужен файл .md")
        rel = "/".join(x for x in (safe_rel(upload.get("dir", "")), fname) if x)
        dest = docs / rel
        content = upload["content"].replace("\r\n", "\n")
        if dest.is_file():
            existed = True
            old = dest.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
            if old == content:
                file_note = ("same", f"{docs_name}/{rel} уже лежит в репо с тем же текстом")
                dest = None
            elif not req.get("overwrite"):
                raise ValueError(f"{docs_name}/{rel} уже есть и отличается. "
                                 "Включи «Перезаписать файл», если так и надо.")
            else:
                file_note = ("overwrite", f"{docs_name}/{rel} будет перезаписан")
        else:
            file_note = ("create", f"Файл будет скопирован в {docs_name}/{rel}")
        path = rel
    else:
        path = safe_rel(req.get("path"))
        if not path:
            raise ValueError("Не выбран файл")
        if not (docs / path).is_file():
            file_note = ("warn", f"{docs_name}/{path} не найден на диске")

    new_lines, notes, count = plan_inserts(lines, nodes, targets, title, path,
                                           req.get("mode", "end"), req.get("subsection", ""))
    diff = list(difflib.unified_diff(lines, new_lines, "mkdocs.yml", "mkdocs.yml", n=3, lineterm=""))
    result = {"diff": "\n".join(diff[2:]), "notes": notes, "file_note": file_note, "path": path,
              "count": count, "writes_file": dest is not None}
    if apply:
        if not count and dest is None:
            raise ValueError("Нечего записывать")
        push_undo(repo, f"добавление {path}", dest, existed)
        if dest is not None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = upload["content"].replace("\r\n", "\n").replace("\n", nl)
            dest.write_bytes(data.encode("utf-8"))
        if count:
            write_yml(repo, new_lines, nl, bom)
    return result


def api_section(req):
    """Новый раздел: папка в docs создается сразу, в nav он попадает с «Главной» или с первой страницей."""
    repo = req["repo"]
    lines, nl, bom, sha = read_yml(repo)
    check_sha(req, sha)
    nodes, _, _ = parse_nav(lines)
    pid = int(req["parent"])
    if not 0 <= pid < len(nodes) or nodes[pid]["path"]:
        raise ValueError("Раздел не найден, обнови страницу")
    title = (req.get("title") or "").strip()
    if not title:
        raise ValueError("Пустое название раздела")
    # chain: пустые разделы-родители из интерфейса, которых еще нет в nav
    chain = [s.strip() for s in (req.get("chain") or []) if s and s.strip()]
    if not chain and any(nodes[c]["path"] is None and nodes[c]["title"] == title for c in nodes[pid]["children"]):
        raise ValueError(f"Раздел «{title}» здесь уже есть")
    docs_name = docs_dir_of(lines)
    rel = safe_rel(req.get("dir", ""))
    folder = Path(repo) / docs_name / rel
    folder.mkdir(parents=True, exist_ok=True)
    if not req.get("with_index"):
        return {"message": f"Раздел «{title}» готов, папка {docs_name}/{rel}. "
                           "В mkdocs.yml он появится с первой страницей"}
    index = folder / "index.md"
    existed = index.is_file()
    path = "/".join(x for x in (rel, "index.md") if x)
    new_lines, _, count = plan_inserts(lines, nodes, [{"id": pid, "sub": chain + [title]}], "Главная", path, "end", "")
    push_undo(repo, f"новый раздел «{title}»", None if existed else index, False)
    if not existed:
        index.write_bytes(f"# {title}{nl}".encode("utf-8"))
    write_yml(repo, new_lines, nl, bom)
    return {"message": f"Раздел «{title}» создан со страницей Главная ({docs_name}/{path})", "path": path}


BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip", ".7z", ".rar", ".gz",
              ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".woff", ".woff2", ".ttf", ".eot", ".otf",
              ".exe", ".dll", ".mp4", ".mp3", ".wav", ".avi", ".mov", ".psd", ".bin", ".jar", ".class"}
YML_KEY = "@mkdocs.yml"


def is_text_file(p):
    """Текстовый ли файл: не из списка бинарных и в начале нет нулевых байтов."""
    if p.suffix.lower() in BINARY_EXT:
        return False
    try:
        with open(p, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    return b"\x00" not in head


def docs_root(repo):
    lines, _, _, _ = read_yml(repo)
    return Path(repo) / docs_dir_of(lines), docs_dir_of(lines)


def editable_path(repo, rel):
    if rel == YML_KEY:
        return yml_path(repo)
    docs, _ = docs_root(repo)
    p = docs / safe_rel(rel)
    if p.is_file() and not is_text_file(p):
        raise ValueError("Это не текстовый файл, в редакторе он не откроется")
    return p


def api_ls(repo, rel):
    """Содержимое одной папки docs/ для дерева файлов (подгружается по мере раскрытия)."""
    docs, docs_name = docs_root(repo)
    rel = safe_rel(rel)
    d = docs / rel
    if not d.is_dir():
        raise ValueError("Папка не найдена: " + rel)
    lines, _, _, _ = read_yml(repo)
    nodes, _, _ = parse_nav(lines)
    in_nav = {n["path"] for n in nodes if n["path"]}
    entries = []
    for e in sorted(os.scandir(d), key=lambda e: (not e.is_dir(), e.name.lower())):
        if e.name.startswith("."):
            continue
        r = "/".join(x for x in (rel, e.name) if x)
        if e.is_dir():
            try:
                cnt = sum(1 for x in os.scandir(e.path) if not x.name.startswith("."))
            except OSError:
                cnt = 0
            entries.append({"name": e.name, "path": r, "dir": True, "count": cnt})
        else:
            p = Path(e.path)
            entries.append({"name": e.name, "path": r, "dir": False, "size": e.stat().st_size,
                            "text": is_text_file(p), "md": p.suffix.lower() in (".md", ".markdown"),
                            "in_nav": r in in_nav})
    return {"path": rel, "docs": docs_name, "entries": entries}


LINK_RE = re.compile(r'(!?\[[^\]]*\]\()([^)\s]+)((?:\s+"[^"]*")?\))')
SRC_RE = re.compile(r'''(\bsrc\s*=\s*["'])([^"']+)(["'])''')


def _is_rel_url(u):
    return bool(u) and not re.match(r"^[a-zA-Z][\w+.-]*:", u) and not u.startswith(("#", "/", "//"))


def page_url_dir(rel):
    """Папка адреса страницы на сайте (use_directory_urls): a/b.md -> a/b, a/index.md -> a."""
    import posixpath
    if rel.lower().endswith(("/index.md", "/readme.md")) or rel.lower() in ("index.md", "readme.md"):
        return posixpath.dirname(rel)
    return rel[: -len(posixpath.splitext(rel)[1])] if rel.lower().endswith((".md", ".markdown")) else posixpath.dirname(rel)


def plan_move(repo, src, dst_dir):
    """Что изменится при переносе src в dst_dir: пути в nav и относительные ссылки в md-файлах."""
    import posixpath
    from urllib.parse import unquote as url_unquote
    docs, docs_name = docs_root(repo)
    src, dst_dir = safe_rel(src), safe_rel(dst_dir)
    if not src:
        raise ValueError("Корень docs переносить нельзя")
    sp = docs / src
    if not sp.exists():
        raise ValueError("Не найдено: " + src)
    if not (docs / dst_dir).is_dir():
        raise ValueError("Папка назначения не найдена")
    name = posixpath.basename(src)
    dst = "/".join(x for x in (dst_dir, name) if x)
    if dst == src or posixpath.dirname(src) == dst_dir:
        raise ValueError("Файл уже лежит в этой папке")
    if sp.is_dir() and (dst_dir == src or dst_dir.startswith(src + "/")):
        raise ValueError("Нельзя перенести папку внутрь самой себя")
    if (docs / dst).exists():
        raise ValueError(f"В папке назначения уже есть «{name}»")

    def newloc(rel):
        if rel == src:
            return dst
        if rel.startswith(src + "/"):
            return dst + rel[len(src):]
        return rel

    lines, _, _, _ = read_yml(repo)
    nodes, _, _ = parse_nav(lines)
    nav = [(n["line"], n["path"], newloc(n["path"])) for n in nodes
           if is_local_md(n["path"]) and newloc(n["path"]) != n["path"]]

    md_files = []
    for cur, dnames, fnames in os.walk(docs):
        dnames[:] = [d for d in dnames if not d.startswith(".")]
        for f in fnames:
            if f.lower().endswith((".md", ".markdown")):
                md_files.append((Path(cur) / f).relative_to(docs).as_posix())
    existing = set(md_files)

    def rel_from(base_dir, target):
        r = posixpath.relpath(target, base_dir or ".")
        return r

    edits, total = {}, 0
    for rf in md_files:
        raw = (docs / rf).read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        fdir_old, fdir_new = posixpath.dirname(rf), posixpath.dirname(newloc(rf))
        udir_old, udir_new = page_url_dir(rf), page_url_dir(newloc(rf))
        cnt = [0]

        def fix(url, url_mode=False):
            path, sep, frag = url.partition("#")
            if not _is_rel_url(path):
                return url
            plain = url_unquote(path)
            last = plain.rstrip("/").rsplit("/", 1)[-1]
            has_ext = "." in last and plain.lower() != ".." and not plain.endswith("/")
            if has_ext and not url_mode:
                # ссылка на файл: считается от папки файла
                t_old = posixpath.normpath(posixpath.join(fdir_old, plain))
                if t_old.startswith(".."):
                    return url
                t_new = newloc(t_old)
                if fdir_old == fdir_new and t_new == t_old:
                    return url
                new = rel_from(fdir_new, t_new)
            else:
                # ссылка на адрес страницы (../lk/fly/, swagger src): считается от адреса страницы
                base_old = udir_old
                t_old = posixpath.normpath(posixpath.join(base_old, plain))
                if t_old.startswith(".."):
                    return url
                if has_ext:
                    t_new = newloc(t_old)
                else:
                    cand = next((c for c in (t_old + ".md", t_old + "/index.md") if c in existing), None)
                    if not cand:
                        return url
                    nl_ = newloc(cand)
                    t_new = page_url_dir(nl_)
                same_target = t_new == (t_old if has_ext else page_url_dir(cand))
                if udir_old == udir_new and same_target:
                    return url
                if has_ext and (docs / t_old).is_file():
                    # файл реально лежит в docs: путь от файла страницы, его плагин swagger находит напрямую
                    new = rel_from(fdir_new, t_new)
                else:
                    new = rel_from(udir_new, t_new)
                if plain.endswith("/") and not new.endswith("/"):
                    new += "/"
            if posixpath.normpath(new) == posixpath.normpath(plain) and (new.endswith("/") == plain.endswith("/")):
                return url
            cnt[0] += 1
            return new + (sep + frag if sep else "")

        new_text = LINK_RE.sub(lambda m: m.group(1) + fix(m.group(2)) + m.group(3), text)
        new_text = SRC_RE.sub(lambda m: m.group(1) + fix(m.group(2), url_mode=True) + m.group(3), new_text)
        if new_text != text:
            edits[rf] = new_text
            total += cnt[0]
    return {"src": src, "dst": dst, "is_dir": sp.is_dir(), "nav": nav, "edits": edits, "links": total,
            "docs": docs_name}


def api_move(req):
    import posixpath
    repo = req["repo"]
    plan = plan_move(repo, req.get("src"), req.get("dst_dir", ""))
    docs, docs_name = docs_root(repo)
    summary = {"src": plan["src"], "dst": plan["dst"], "is_dir": plan["is_dir"],
               "nav": [{"from": a, "to": b} for _, a, b in plan["nav"]],
               "files": sorted(plan["edits"]), "links": plan["links"]}
    if req.get("dry"):
        return summary
    lines, nl, bom, sha = read_yml(repo)
    check_sha(req, sha)
    # копии для отката: mkdocs.yml и исходные тексты правленых файлов
    BACKUPS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10**6:06d}"
    trash = BACKUPS / f"move-{stamp}"
    trash.mkdir(parents=True)
    shutil.copy2(yml_path(repo), trash / "mkdocs.yml")
    src_abs, dst_abs = docs / plan["src"], docs / plan["dst"]

    def newloc(rel):
        if rel == plan["src"]:
            return plan["dst"]
        if rel.startswith(plan["src"] + "/"):
            return plan["dst"] + rel[len(plan["src"]):]
        return rel
    saved = []
    for rf in plan["edits"]:
        b = trash / "files" / rf
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(docs / rf, b)
        saved.append({"orig": str(docs / rf), "backup": str(b)})
    shutil.move(str(src_abs), str(dst_abs))
    for rf, text in plan["edits"].items():
        p = docs / newloc(rf)
        raw = p.read_bytes()
        old = raw.decode("utf-8-sig")
        fnl = "\r\n" if "\r\n" in old else "\n"
        p.write_bytes((b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"")
                      + text.replace("\r\n", "\n").replace("\n", fnl).encode("utf-8"))
    if plan["nav"]:
        for ln, a, b in plan["nav"]:
            i = lines[ln].rfind(a)
            if i >= 0:
                lines[ln] = lines[ln][:i] + b + lines[ln][i + len(a):]
        write_yml(repo, lines, nl, bom)
    cfg = load_config()
    stack = cfg.get("undo") if isinstance(cfg.get("undo"), list) else []
    stack.append({"repo": repo, "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "label": f"перенос {plan['src']} в {posixpath.dirname(plan['dst']) or docs_name}",
                  "kind": "move", "from": str(dst_abs), "to": str(src_abs), "yml": str(trash / "mkdocs.yml"),
                  "files": saved, "trash": str(trash)})
    cfg["undo"] = stack[-UNDO_DEPTH:]
    save_config(cfg)
    parts = []
    if plan["nav"]:
        parts.append(f"меню: {len(plan['nav'])}")
    if plan["links"]:
        parts.append(f"ссылок поправлено: {plan['links']} в {len(plan['edits'])} файл.")
    summary["message"] = f"Перенесено в {docs_name}/{posixpath.dirname(plan['dst']) or ''}".rstrip("/") + (
        " (" + ", ".join(parts) + ")" if parts else "")
    return summary


def check_name(name):
    name = (name or "").strip()
    if not name or name in (".", "..") or re.search(r'[\\/:*?"<>|]', name):
        raise ValueError("Недопустимое имя: " + (name or "(пусто)"))
    return name


def api_newfile(req):
    """Новый файл в docs/. Для md можно сразу добавить в меню: {nav: id раздела, title}."""
    repo = req["repo"]
    docs, docs_name = docs_root(repo)
    rel_dir = safe_rel(req.get("dir", ""))
    name = check_name(req.get("name")).replace(" ", "-")
    rel = "/".join(x for x in (rel_dir, name) if x)
    p = docs / rel
    if p.exists():
        raise ValueError(f"{docs_name}/{rel} уже существует")
    title = (req.get("title") or "").strip()
    is_md = p.suffix.lower() in (".md", ".markdown")
    content = f"# {title or p.stem}\n\n" if is_md else ""
    if isinstance(req.get("content"), str):
        # текст готового файла с компьютера
        if len(req["content"]) > 5 * 1024 * 1024:
            raise ValueError("Файл больше 5 МБ")
        content = req["content"].replace("\r\n", "\n")
    nav = req.get("nav")
    if nav is not None and is_md:
        lines, nl, bom, sha = read_yml(repo)
        check_sha(req, sha)
        nodes, _, _ = parse_nav(lines)
        new_lines, notes, count = plan_inserts(lines, nodes, [nav], title or p.stem, rel, req.get("mode", "end"), "")
        push_undo(repo, f"новая страница {rel}", p, False)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.replace("\n", nl).encode("utf-8"))
        if count:
            write_yml(repo, new_lines, nl, bom)
        return {"message": f"Создана страница {docs_name}/{rel} и добавлена в меню", "path": rel}
    push_undo(repo, f"новый файл {rel}", p, False)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content.encode("utf-8"))
    return {"message": f"Создан файл {docs_name}/{rel}", "path": rel}


def api_mkdir(req):
    repo = req["repo"]
    docs, docs_name = docs_root(repo)
    rel = "/".join(x for x in (safe_rel(req.get("dir", "")), check_name(req.get("name")).replace(" ", "-")) if x)
    p = docs / rel
    if p.exists():
        raise ValueError(f"{docs_name}/{rel} уже существует")
    p.mkdir(parents=True)
    return {"message": f"Создана папка {docs_name}/{rel}", "path": rel}


def remove_nav_paths(lines, rel, is_dir):
    """Убирает из nav пункты с этим файлом (или всем внутри папки) и разделы, которые от этого опустели."""
    lines, removed = list(lines), []
    match = (lambda p: p == rel or p.startswith(rel + "/")) if is_dir else (lambda p: p == rel)
    while True:
        nodes, _, _ = parse_nav(lines)
        n = next((x for x in nodes if x["path"] and match(x["path"])), None)
        if n is None:
            break
        removed.append(breadcrumb(nodes, n["id"]))
        del lines[n["line"]:n["last"] + 1]
    if removed:
        lines = drop_empty_sections(lines)
    return lines, removed


def drop_empty_sections(lines):
    """Убирает разделы nav, в которых не осталось пунктов (mkdocs такие не собирает)."""
    lines = list(lines)
    while True:
        nodes, _, _ = parse_nav(lines)
        n = next((x for x in nodes[1:] if x["path"] is None and not x["raw"] and not x["children"]), None)
        if n is None:
            return lines
        del lines[n["line"]:n["line"] + 1]


def remove_empty_dirs(docs, rels):
    """После удаления файлов убираем опустевшие папки (вверх до docs)."""
    for rel in rels:
        d = (docs / rel).parent
        while d != docs and docs in d.parents:
            try:
                if any(d.iterdir()):
                    break
                d.rmdir()
            except OSError:
                break
            d = d.parent


def api_nav_delete(req):
    """Корзина в меню: убирает пункт (или раздел целиком) и, если files, удаляет файлы его страниц с диска.
    Другие пункты меню с теми же файлами и опустевшие разделы убираются тоже. Откат - одним шагом."""
    repo = req["repo"]
    lines, nl, bom, sha = read_yml(repo)
    if not req.get("dry"):
        check_sha(req, sha)
    nodes, _, _ = parse_nav(lines)
    nid = int(req["id"])
    if not 0 < nid < len(nodes):
        raise ValueError("Пункт не найден, обнови страницу")
    n = nodes[nid]
    docs, docs_name = docs_root(repo)
    pages = [n["path"]] if n["path"] else descendant_pages(nodes, nid)
    pages = sorted({p for p in pages if is_local_md(p)})
    existing = [p for p in pages if (docs / p).is_file()]
    files = bool(req.get("files"))
    new = list(lines)
    del new[n["line"]:n["last"] + 1]
    other = []
    if files:
        for pth in existing:
            new, rem = remove_nav_paths(new, pth, False)
            other += rem
    new = drop_empty_sections(new)
    tracked = [p for p in existing if git_tracked(repo, docs / p)]
    # папки, где после удаления не останется ни одной страницы, уходят целиком (картинки, assets, пустые подпапки)
    import posixpath
    gone = set(existing)
    full_dirs, extra = [], []
    if files and not n["path"]:
        for dd in sorted({posixpath.dirname(p) for p in existing if posixpath.dirname(p)}, key=len):
            if any(dd == f or dd.startswith(f + "/") for f in full_dirs):
                continue
            md_left, others = [], []
            for cur, _, fnames in os.walk(docs / dd):
                for f in fnames:
                    rel_f = (Path(cur) / f).relative_to(docs).as_posix()
                    if rel_f in gone:
                        continue
                    (md_left if f.lower().endswith((".md", ".markdown")) else others).append(rel_f)
            if not md_left:
                full_dirs.append(dd)
                extra += others
    if req.get("dry"):
        return {"title": n["title"], "is_page": bool(n["path"]), "pages": len(pages), "files": existing,
                "tracked": len(tracked), "other_nav": other, "missing": len(pages) - len(existing),
                "dirs": full_dirs, "extra": len(extra), "extra_list": extra[:8]}
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10**6:06d}"
    trash = BACKUPS / f"navdel-{stamp}"
    trash.mkdir(parents=True)
    shutil.copy2(yml_path(repo), trash / "mkdocs.yml")
    items = []
    if files:
        for dd in full_dirs:
            copy_any(docs / dd, trash / "files" / dd)
            items.append({"path": str(docs / dd), "backup": str(trash / "files" / dd)})
        for pth in existing:
            if any(pth.startswith(dd + "/") for dd in full_dirs):
                continue
            copy_any(docs / pth, trash / "files" / pth)
            items.append({"path": str(docs / pth), "backup": str(trash / "files" / pth)})
    write_yml(repo, new, nl, bom)
    if files:
        for it in items:
            remove_any(it["path"])
        remove_empty_dirs(docs, existing)
    what = f"«{n['title']}»" + ("" if n["path"] else f" (раздел, страниц: {len(pages)})")
    cfg = load_config()
    stack = cfg.get("undo") if isinstance(cfg.get("undo"), list) else []
    stack.append({"repo": repo, "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "label": f"удаление {what}" + (" вместе с файлами" if files and items else " из меню"),
                  "kind": "files", "items": items, "yml": str(trash / "mkdocs.yml"), "trash": str(trash)})
    cfg["undo"] = stack[-UNDO_DEPTH:]
    save_config(cfg)
    msg = f"Удалено из меню: {what}"
    if files and items:
        msg += f", страниц удалено: {len(existing)}"
        if full_dirs:
            msg += f", папок целиком: {len(full_dirs)}"
        if tracked:
            msg += f" (в коммите удалятся из репозитория: {len(tracked)})"
    return {"message": msg}


def git_tracked(repo, abs_path):
    """Есть ли файл (или что-то в папке) в git: тогда удаление уйдет в коммит."""
    rel = os.path.relpath(abs_path, repo).replace("\\", "/")
    return bool(git(repo, "ls-files", "--", rel))


def api_rmfile(req):
    """Удаление файла или папки из docs/: копия уходит в корзину инструмента, откат кнопкой «Отменить».
    Если файл есть в git, удаление попадет в коммит. По умолчанию убирает и пункты меню с этим файлом."""
    repo = req["repo"]
    docs, docs_name = docs_root(repo)
    rel = safe_rel(req.get("path"))
    if not rel:
        raise ValueError("Корень docs удалять нельзя")
    p = docs / rel
    if not p.exists():
        raise ValueError("Не найдено: " + rel)
    lines, nl, bom, sha = read_yml(repo)
    new_lines, removed = remove_nav_paths(lines, rel, p.is_dir())
    tracked = git_tracked(repo, p)
    if req.get("dry"):
        return {"tracked": tracked, "nav": removed, "is_dir": p.is_dir()}
    use_nav = req.get("nav", True) and removed
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10**6:06d}"
    trash = BACKUPS / f"rm-{stamp}"
    copy_any(p, trash / rel)
    entry = {"repo": repo, "label": f"удаление {docs_name}/{rel}", "kind": "files",
             "items": [{"path": str(p), "backup": str(trash / rel)}], "trash": str(trash)}
    if use_nav:
        shutil.copy2(yml_path(repo), trash / "mkdocs.yml")
        entry["yml"] = str(trash / "mkdocs.yml")
        write_yml(repo, new_lines, nl, bom)
    remove_any(p)
    cfg = load_config()
    stack = cfg.get("undo") if isinstance(cfg.get("undo"), list) else []
    stack.append(entry)
    cfg["undo"] = stack[-UNDO_DEPTH:]
    save_config(cfg)
    msg = f"Удалено: {docs_name}/{rel}"
    if use_nav:
        msg += f", из меню убрано пунктов: {len(removed)}"
    if tracked:
        msg += ". В коммите файл удалится и из репозитория"
    return {"message": msg}


def api_file_get(repo, rel):
    p = editable_path(repo, rel)
    if not p.is_file():
        raise ValueError("Файл не найден: " + rel)
    raw = p.read_bytes()
    if len(raw) > 3 * 1024 * 1024:
        raise ValueError("Файл больше 3 МБ, его лучше открыть в обычном редакторе")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Файл не в UTF-8, редактор его не откроет")
    return {"path": rel, "content": text.replace("\r\n", "\n"), "sha": hashlib.sha1(raw).hexdigest(),
            "name": "mkdocs.yml" if rel == YML_KEY else rel}


def api_file_save(req):
    repo, rel = req["repo"], req["path"]
    p = editable_path(repo, rel)
    raw = p.read_bytes() if p.is_file() else b""
    if req.get("sha") and raw and hashlib.sha1(raw).hexdigest() != req["sha"]:
        raise ValueError("Файл изменился снаружи, пока был открыт. Скопируй свои правки, закрой и открой файл заново")
    content = req["content"].replace("\r\n", "\n")
    if rel == YML_KEY:
        _, nl, bom, _ = read_yml(repo)
        push_undo(repo, "правка mkdocs.yml в редакторе")
        try:
            write_yml(repo, content.split("\n"), nl, bom)
        except Exception:
            # запись не прошла (сломала бы файл): лишний шаг отката не нужен
            cfg = load_config()
            cfg["undo"] = cfg.get("undo", [])[:-1]
            save_config(cfg)
            raise
    else:
        old = raw.decode("utf-8-sig", "replace")
        nl = "\r\n" if "\r\n" in old else "\n"
        bom = raw.startswith(b"\xef\xbb\xbf")
        push_undo(repo, f"правка {rel} в редакторе", p, p.is_file())
        data = content.replace("\n", nl).encode("utf-8")
        p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
    return {"message": "Сохранено", "sha": hashlib.sha1(p.read_bytes()).hexdigest()}


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"}


def api_asset(req):
    """Картинка из редактора: кладется рядом с md-файлом, возвращается путь для вставки ![...](путь)."""
    import base64
    import posixpath
    repo = req["repo"]
    docs, docs_name = docs_root(repo)
    md = safe_rel(req.get("md"))
    name = check_name(req.get("name")).replace(" ", "-")
    if Path(name).suffix.lower() not in IMAGE_EXT:
        raise ValueError("Нужна картинка: " + ", ".join(sorted(IMAGE_EXT)))
    data = base64.b64decode(req.get("data") or "")
    if not data:
        raise ValueError("Пустой файл")
    if len(data) > 15 * 1024 * 1024:
        raise ValueError("Картинка больше 15 МБ")
    rel = "/".join(x for x in (posixpath.dirname(md), name) if x)
    dest = docs / rel
    existed = dest.is_file()
    if existed and not req.get("overwrite"):
        raise ValueError(f"Файл {name} рядом со страницей уже есть. Дай другое имя или включи «Заменить»")
    push_undo(repo, f"картинка {rel}", dest, existed)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {"message": f"Картинка сохранена: {docs_name}/{rel}", "path": name, "rel": rel}


# ---------- обновление самой программы (папка программы - клон git-репозитория) ----------

RESTART_FLAG = HERE / ".restart"


def tool_is_git():
    return (HERE / ".git").exists()


def api_selfupdate_status(fetch=True):
    if not tool_is_git():
        return {"git": False}
    h = str(HERE)
    if fetch:
        git_run(h, "fetch", "--quiet", timeout=40)
    upstream = git(h, "rev-parse", "--abbrev-ref", "@{u}")
    behind = git(h, "rev-list", "--count", "HEAD..@{u}") if upstream else ""
    return {"git": True, "version": git(h, "log", "-1", "--format=%cd %h", "--date=format:%d.%m.%Y"),
            "upstream": upstream, "behind": int(behind) if behind.isdigit() else 0,
            "changes": git(h, "log", "--format=%cd  %s", "--date=format:%d.%m", "HEAD..@{u}").splitlines()[:30]
            if upstream else []}


def api_selfupdate():
    if not tool_is_git():
        raise ValueError("Программа установлена не через git, обновлять кнопкой нельзя")
    h = str(HERE)
    code, out = git_run(h, "pull", "--ff-only", timeout=120)
    if code:
        raise ValueError("Не получилось обновить программу:\n" + out)
    # перезапуск: run.bat запускает программу снова, если код выхода 3; браузер не открываем повторно
    RESTART_FLAG.write_text("1")

    def restart():
        try:
            PREVIEW.stop_proc()
            RENDERER.stop()
            if APP_MODE:
                subprocess.Popen([sys.executable] + sys.argv, cwd=str(HERE), close_fds=True,
                                 creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
                                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        finally:
            os._exit(0 if APP_MODE else 3)
    threading.Timer(0.7, restart).start()
    return {"message": "Программа обновлена, перезапускаюсь", "output": out}


def api_presets(req=None):
    """Свои шаблоны редактора: хранятся в config.json рядом с программой."""
    cfg = load_config()
    presets = cfg.get("presets") if isinstance(cfg.get("presets"), list) else []

    def entry(it):
        return {"name": str(it.get("name") or "").strip()[:80], "text": it.get("text") or "",
                "group": (str(it.get("group") or "").strip() or "Мои шаблоны")[:60],
                "desc": str(it.get("desc") or "").strip()[:200], "page": bool(it.get("page"))}
    msg = None
    if req and req.get("op") == "import":
        # шаблоны из файла коллеги: одноименные заменяются, остальные добавляются
        items = req.get("templates")
        if not isinstance(items, list) or not items:
            raise ValueError("В файле нет шаблонов")
        by = {p.get("name"): p for p in presets}
        new = upd = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            e = entry(it)
            if not e["name"] or not isinstance(e["text"], str) or not e["text"].strip():
                continue
            if e["name"] in by:
                upd += 1
            else:
                new += 1
            by[e["name"]] = e
        presets = list(by.values())
        msg = f"Шаблоны загружены: новых {new}, обновлено {upd}"
    elif req:
        name = (req.get("name") or "").strip()
        if not name:
            raise ValueError("Пустое название шаблона")
        presets = [p for p in presets if p.get("name") != name]
        if req.get("op") == "add":
            if not (req.get("text") or "").strip():
                raise ValueError("Пустой шаблон")
            presets.append(entry(req))
    if req:
        cfg["presets"] = presets
        save_config(cfg)
    return {"presets": presets, "message": msg}


def api_op(req):
    if req.get("op") == "delete":
        return api_nav_delete(req)
    repo = req["repo"]
    lines, nl, bom, sha = read_yml(repo)
    check_sha(req, sha)
    nodes, _, _ = parse_nav(lines)
    nid = int(req["id"])
    if not 0 < nid < len(nodes):
        raise ValueError("Пункт не найден, обнови страницу")
    n = nodes[nid]
    parent = nodes[n["parent"]]
    sibs = parent["children"]
    pos = sibs.index(nid)
    op = req["op"]
    new = list(lines)
    if op == "rename":
        title = (req.get("title") or "").strip()
        if not title:
            raise ValueError("Пустое название")
        line = " " * n["indent"] + "- " + yaml_title(title) + ":" + (" " + n["raw"] if n["raw"] else "")
        new[n["line"]] = line
        label = f"переименование «{n['title']}» в «{title}»"
    elif op == "delete":
        if len(sibs) == 1 and parent["id"] != 0:
            raise ValueError(f"Это единственный пункт в разделе «{parent['title']}». "
                             "Удали раздел целиком, иначе mkdocs не соберется.")
        del new[n["line"]:n["last"] + 1]
        label = f"удаление «{n['title']}» из меню"
    elif op in ("up", "down"):
        j = pos - 1 if op == "up" else pos + 1
        if not 0 <= j < len(sibs):
            raise ValueError("Двигать дальше некуда")
        a, b = (nodes[sibs[j]], n) if op == "up" else (n, nodes[sibs[j]])
        block_a = lines[a["line"]:a["last"] + 1]
        block_b = lines[b["line"]:b["last"] + 1]
        mid = lines[a["last"] + 1:b["line"]]
        new = lines[:a["line"]] + block_b + mid + block_a + lines[b["last"] + 1:]
        label = f"перемещение «{n['title']}» {'выше' if op == 'up' else 'ниже'}"
    else:
        raise ValueError("Неизвестная операция")
    push_undo(repo, label)
    write_yml(repo, new, nl, bom)
    return {"message": label[:1].upper() + label[1:]}


# ---------- локальный предпросмотр (mkdocs serve) ----------

ENV_DIR = HERE / ".mkdocs-env"
NODE_DIR = HERE / ".node"
NPM_GLOBAL = NODE_DIR / "g"
PID_FILE = HERE / ".mkdocs.pid"
FAST_CFG = HERE / ".mkdocs-fast.yml"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Preview:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"      # idle | installing | starting | running | error
        self.repo = None
        self.url = None
        self.log = []
        self.proc = None
        self.error = None
        self.building = False
        self.fast = False
        self.no_node = False
        self.fast_mtime = None

    def add(self, line):
        line = ANSI_RE.sub("", line).rstrip()
        if "Building documentation" in line:
            self.building = True
        elif "Serving on" in line or "Documentation built" in line or "Aborted" in line:
            self.building = False
        if line:
            self.log.append(line)
            del self.log[:-400]

    def status(self):
        alive = self.proc is not None and self.proc.poll() is None
        if self.state in ("running", "starting") and not alive:
            self.state = "error"
            self.error = self.error or "mkdocs остановился, смотри лог"
        return {"state": self.state, "repo": self.repo, "url": self.url, "error": self.error,
                "log": self.log[-200:], "building": self.building, "fast": self.fast, "no_node": self.no_node,
                "node_local": bool(self.local_node_dirs()), "can_install_node": os.name == "nt", "env_ready": (ENV_DIR / "ready.txt").is_file()}

    def run_logged(self, cmd, cwd=None, env=None):
        self.add("> " + " ".join(str(c) for c in cmd))
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        for line in p.stdout:
            self.add(line)
        return p.wait()

    def env_python(self):
        return ENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def ensure_env(self, repo):
        req = Path(repo) / "requirements.txt"
        want = hashlib.sha1(req.read_bytes()).hexdigest() if req.is_file() else "no-req"
        marker = ENV_DIR / "ready.txt"
        if marker.is_file() and marker.read_text().strip() == want and self.env_python().is_file():
            return
        self.state = "installing"
        if not self.env_python().is_file():
            self.add("Создаю окружение Python для mkdocs (один раз)...")
            if self.run_logged([sys.executable, "-m", "venv", str(ENV_DIR)]):
                raise RuntimeError("Не удалось создать окружение Python")
        self.add("Ставлю пакеты из requirements.txt (первый раз 1-3 минуты)...")
        cmd = [str(self.env_python()), "-m", "pip", "install", "--disable-pip-version-check"]
        cmd += ["-r", str(req)] if req.is_file() else ["mkdocs", "mkdocs-material"]
        if self.run_logged(cmd):
            raise RuntimeError("pip не смог поставить пакеты, смотри лог")
        marker.write_text(want)

    def needs_likec4(self, repo):
        text = yml_path(repo).read_text(encoding="utf-8-sig", errors="replace")
        return bool(re.search(r"^\s*-\s*likec4\b", text, re.M))

    @staticmethod
    def local_node_dirs():
        """Портативный Node и локальный likec4 из папки инструмента (если ставились кнопкой)."""
        dirs = []
        if (NODE_DIR / "node" / "node.exe").is_file() or (NODE_DIR / "node" / "bin" / "node").is_file():
            dirs.append(str(NODE_DIR / "node"))
        if NPM_GLOBAL.is_dir():
            dirs.append(str(NPM_GLOBAL))
        return dirs

    @staticmethod
    def likec4_env(env):
        """Плагин зовет `npx likec4`. npx видит только глобальные пакеты, иначе качает самую свежую версию,
        поэтому наш likec4 ставится как глобальный в свою папку, и npx направляется туда."""
        if (NPM_GLOBAL / "likec4.cmd").is_file() or (NPM_GLOBAL / "bin" / "likec4").is_file():
            env["NPM_CONFIG_PREFIX"] = str(NPM_GLOBAL)
        return env

    def node_env_path(self):
        return os.pathsep.join(self.local_node_dirs() + [os.environ.get("PATH", "")])

    @staticmethod
    def likec4_pkg(repo):
        ci = Path(repo) / ".gitlab-ci.yml"
        m = re.search(r"likec4@([\w.\-]+)", ci.read_text(encoding="utf-8", errors="replace")) if ci.is_file() else None
        return "likec4@" + (m.group(1) if m else "latest")

    def ensure_likec4(self, repo, path):
        """likec4 есть - ок. Нет, но есть npm - ставим локально. Возвращает True, если likec4 доступен."""
        if shutil.which("likec4", path=path):
            return True
        npm = shutil.which("npm", path=path)
        if not npm:
            return False
        pkg = self.likec4_pkg(repo)
        self.state = "installing"
        self.add(f"Ставлю {pkg} в папку инструмента (пара минут)...")
        NPM_GLOBAL.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PATH=os.pathsep.join([str(NPM_GLOBAL), path]), NPM_CONFIG_PREFIX=str(NPM_GLOBAL))
        if self.run_logged([npm, "install", "-g", pkg, "--allow-scripts=esbuild", "--no-audit", "--no-fund"], env=env):
            self.add("ВНИМАНИЕ: likec4 не поставился (смотри ошибку npm выше).")
            return False
        return bool(shutil.which("likec4", path=self.node_env_path()))

    def install_node(self, repo):
        with self.lock:
            if self.state in ("installing", "starting"):
                raise ValueError("Уже идет установка или запуск, подожди")
            self.stop_proc()
            self.repo, self.url, self.error, self.log = repo, None, None, []
            self.state = "installing"
        threading.Thread(target=self._install_node, args=(repo,), daemon=True).start()

    def _install_node(self, repo):
        import platform
        import urllib.request
        import zipfile
        try:
            if os.name != "nt":
                raise RuntimeError("Автоустановка Node есть только для Windows")
            arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
            self.add("Ищу последнюю LTS-версию Node.js на nodejs.org...")
            with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=30) as r:
                index = json.load(r)
            rel = next(x for x in index if x.get("lts") and f"win-{arch}-zip" in x.get("files", []))
            ver = rel["version"]
            name = f"node-{ver}-win-{arch}"
            with urllib.request.urlopen(f"https://nodejs.org/dist/{ver}/SHASUMS256.txt", timeout=30) as r:
                sums = r.read().decode()
            want = next(l.split()[0] for l in sums.splitlines() if l.strip().endswith(name + ".zip"))
            NODE_DIR.mkdir(exist_ok=True)
            zpath = NODE_DIR / (name + ".zip")
            self.add(f"Скачиваю Node.js {ver} ({arch})...")
            h = hashlib.sha256()
            with urllib.request.urlopen(f"https://nodejs.org/dist/{ver}/{name}.zip", timeout=60) as r, open(zpath, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done, last = 0, -1
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    pct = done * 100 // total if total else 0
                    if pct // 20 != last:
                        last = pct // 20
                        self.add(f"  скачано {done >> 20} МБ{f' из {total >> 20}' if total else ''}")
            if h.hexdigest() != want:
                zpath.unlink(missing_ok=True)
                raise RuntimeError("Контрольная сумма архива Node.js не совпала, скачивание отменено")
            self.add("Контрольная сумма совпала, распаковываю...")
            target = NODE_DIR / "node"
            tmp = NODE_DIR / "_unpack"
            shutil.rmtree(tmp, ignore_errors=True)
            with zipfile.ZipFile(zpath) as z:
                z.extractall(tmp)
            shutil.rmtree(target, ignore_errors=True)
            (tmp / name).rename(target)
            shutil.rmtree(tmp, ignore_errors=True)
            zpath.unlink(missing_ok=True)
            self.add(f"Node.js {ver} лежит в папке инструмента (.node), в систему ничего не ставилось")
            if not self.ensure_likec4(repo, self.node_env_path()):
                raise RuntimeError("Node.js поставился, а likec4 нет. Проверь доступ к registry.npmjs.org")
            self.add("Готово. Запускаю сайт с диаграммами...")
            self.state = "idle"
            self.start(repo, False, keep_log=True)
        except Exception as e:
            self.state = "error"
            self.error = f"Не удалось поставить Node.js: {e}"
            self.add("ОШИБКА: " + str(e))

    def start(self, repo, fast=False, keep_log=False):
        with self.lock:
            if self.state in ("installing", "starting"):
                raise ValueError("Уже запускается, подожди")
            self.stop_proc()
            self.repo, self.url, self.error, self.fast = repo, None, None, fast
            if not keep_log:
                self.log = []
            self.no_node = False
            self.state = "installing"
        threading.Thread(target=self._start, args=(repo,), daemon=True).start()

    def write_fast_config(self, repo):
        """Копия mkdocs.yml без плагина likec4 и с абсолютным docs_dir, лежит в папке инструмента."""
        src = yml_path(repo)
        lines = src.read_text(encoding="utf-8-sig").splitlines()
        out, skip_indent = [], None
        for l in lines:
            if skip_indent is not None:
                if l.strip() and (len(l) - len(l.lstrip())) <= skip_indent:
                    skip_indent = None
                else:
                    continue
            m = re.match(r"^(\s*)-\s*likec4\s*:?\s*$", l)
            if m:
                skip_indent = len(m.group(1))
                continue
            if re.match(r"^docs_dir\s*:", l):
                continue
            out.append(l)
        docs = (Path(repo) / docs_dir_of(lines)).resolve().as_posix()
        out.insert(0, f"docs_dir: '{docs}'")
        FAST_CFG.write_text("\n".join(out), encoding="utf-8")
        self.fast_mtime = src.stat().st_mtime

    def watch_fast(self, repo, proc):
        # mkdocs serve следит за временным конфигом, поэтому правки в настоящем mkdocs.yml переносим туда
        while self.proc is proc and proc.poll() is None:
            time.sleep(1.5)
            try:
                if yml_path(repo).stat().st_mtime != self.fast_mtime:
                    self.write_fast_config(repo)
            except Exception:
                pass

    def _start(self, repo):
        try:
            self.ensure_env(repo)
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            env["PATH"] = self.node_env_path()
            ok = True
            if not self.fast and self.needs_likec4(repo):
                ok = self.ensure_likec4(repo, env["PATH"])
                env["PATH"] = self.node_env_path()
                self.likec4_env(env)
            if not ok or (not self.fast and self.needs_likec4(repo) and not shutil.which("npx", path=env["PATH"])):
                # без Node плагин likec4 роняет всю сборку, поэтому собираем без диаграмм
                self.fast = True
                self.no_node = True
                self.add("Node.js/likec4 не найден: запускаю без C4-диаграмм. "
                         "Для диаграмм нажми «Поставить Node.js и likec4».")
            port = free_port(8000)
            self.url = f"http://127.0.0.1:{port}/"
            self.state = "starting"
            cfg = yml_path(repo)
            if self.fast:
                self.write_fast_config(repo)
                cfg = FAST_CFG
                self.add("Быстрый режим: диаграммы likec4 не собираются")
            cmd = [str(self.env_python()), "-m", "mkdocs", "serve", "-a", f"127.0.0.1:{port}", "-f", str(cfg)]
            self.add("> mkdocs serve")
            # без CREATE_NO_WINDOW: mkdocs живет в той же консоли и закрывается вместе с окном run.bat
            self.proc = subprocess.Popen(cmd, cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                         creationflags=NO_WINDOW)
            PID_FILE.write_text(str(self.proc.pid))
            if self.fast:
                threading.Thread(target=self.watch_fast, args=(repo, self.proc), daemon=True).start()
            for line in self.proc.stdout:
                self.add(line)
                if "Serving on" in line:
                    self.state = "running"
            if self.state != "idle":
                self.state = "error"
                self.error = "mkdocs завершился, смотри лог"
        except Exception as e:
            self.state = "error"
            self.error = str(e)
            self.add("ОШИБКА: " + str(e))

    def stop_proc(self):
        p = self.proc
        self.proc = None
        if p and p.poll() is None:
            kill_tree(p.pid)
        PID_FILE.unlink(missing_ok=True)

    def stop(self):
        with self.lock:
            self.state = "idle"
            self.stop_proc()
            self.url = None
        return {"message": "Локальный сайт остановлен"}


def refresh_path():
    """Проводник держит PATH со времени входа в Windows: Node, поставленный позже, не виден.
    Дочитываем PATH из реестра и добавляем стандартные папки Node/npm."""
    if os.name != "nt":
        return
    parts = os.environ.get("PATH", "").split(os.pathsep)
    extra = []
    try:
        import winreg
        for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, "Environment")):
            try:
                with winreg.OpenKey(root, key) as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                    extra += os.path.expandvars(val).split(os.pathsep)
            except OSError:
                pass
    except ImportError:
        pass
    env = os.environ
    for d in (Path(env.get("ProgramFiles", r"C:\Program Files")) / "nodejs",
              Path(env.get("APPDATA", "")) / "npm",
              Path(env.get("LOCALAPPDATA", "")) / "Programs" / "nodejs",
              Path(env.get("NVM_SYMLINK", "")) if env.get("NVM_SYMLINK") else None):
        if d and d.is_dir():
            extra.append(str(d))
    norm = {p.rstrip("\\/").lower() for p in parts if p}
    for p in extra:
        if p and p.rstrip("\\/").lower() not in norm and os.path.isdir(p):
            parts.append(p)
            norm.add(p.rstrip("\\/").lower())
    os.environ["PATH"] = os.pathsep.join(x for x in parts if x)


def kill_tree(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def kill_stale_preview():
    """Если прошлый запуск упал, его mkdocs serve мог остаться висеть."""
    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        return
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                             creationflags=NO_WINDOW).stdout.decode("cp866", "replace") if os.name == "nt" else ""
        if "python" in out.lower():
            kill_tree(pid)
    except Exception:
        pass
    PID_FILE.unlink(missing_ok=True)


RENDER_WORKER = r'''
import sys, json, os, importlib
import yaml, markdown
class L(yaml.SafeLoader):
    pass
def py_name(loader, suffix, node):
    mod, _, attr = suffix.rpartition(".")
    return getattr(importlib.import_module(mod), attr)
L.add_multi_constructor("tag:yaml.org,2002:python/name:", py_name)
L.add_multi_constructor("!", lambda loader, suffix, node: None)
cache = {}
def get_md(cfg):
    mt = os.path.getmtime(cfg)
    if cfg in cache and cache[cfg][0] == mt:
        return cache[cfg][1], cache[cfg][2]
    with open(cfg, encoding="utf-8-sig") as f:
        data = yaml.load(f, Loader=L) or {}
    exts, conf = ["toc", "tables", "fenced_code"], {}
    for e in data.get("markdown_extensions") or []:
        items = [(e, None)] if isinstance(e, str) else list(e.items()) if isinstance(e, dict) else []
        for name, c in items:
            if name not in exts:
                exts.append(name)
            if c:
                conf[name] = c
    skipped = []
    while True:
        try:
            md = markdown.Markdown(extensions=exts, extension_configs={k: v for k, v in conf.items() if k in exts})
            break
        except Exception as ex:
            bad = next((x for x in exts if x in str(ex)), None)
            if not bad:
                raise
            exts.remove(bad)
            skipped.append(bad)
    cache[cfg] = (mt, md, skipped)
    return md, skipped
for line in sys.stdin:
    try:
        req = json.loads(line)
        md, skipped = get_md(req["cfg"])
        md.reset()
        text = req["text"]
        if text.startswith("---"):
            parts = text.split("\n")
            end = next((i for i in range(1, len(parts)) if parts[i].strip() in ("---", "...")), None)
            if end:
                text = "\n".join(parts[end + 1:])
        out = {"html": md.convert(text), "skipped": skipped}
    except Exception as ex:
        out = {"error": str(ex)}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
'''


class Renderer:
    """Предпросмотр тем же Python-Markdown и расширениями, что у mkdocs (процесс живет между запросами)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None

    def _ensure(self):
        if self.proc and self.proc.poll() is None:
            return True
        py = ENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not py.is_file():
            return False
        self.proc = subprocess.Popen([str(py), "-u", "-c", RENDER_WORKER], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                     encoding="ascii", creationflags=NO_WINDOW)
        return True

    def render(self, repo, text):
        with self.lock:
            if not self._ensure():
                return {"error": "no-env"}
            try:
                self.proc.stdin.write(json.dumps({"cfg": str(yml_path(repo)), "text": text}) + "\n")
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
                return json.loads(line) if line else {"error": "renderer died"}
            except Exception as e:
                try:
                    self.proc.kill()
                except Exception:
                    pass
                self.proc = None
                return {"error": str(e)}

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


RENDERER = Renderer()


def free_port(start):
    import socket
    for port in range(start, start + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Нет свободного порта для mkdocs")


PREVIEW = Preview()


LOG_FILE = HERE / "navtool.log"


def log_error(where):
    """Ошибки пишем в navtool.log рядом с программой, чтобы потом понять, что случилось."""
    import traceback
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + where + "\n" + traceback.format_exc() + "\n")
        if LOG_FILE.stat().st_size > 2 * 1024 * 1024:
            LOG_FILE.write_text(LOG_FILE.read_text(encoding="utf-8")[-500000:], encoding="utf-8")
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def safe_send(self, code, body):
        try:
            self.send(code, body)
        except Exception:
            pass  # браузер уже закрыл соединение

    def foreign(self, post=False):
        """Запрос не от своей страницы. Host отсекает DNS rebinding, Origin и JSON - запросы
        с чужих сайтов (форма или no-cors fetch не могут прислать application/json без preflight)."""
        port = self.server.server_address[1]
        own = (f"127.0.0.1:{port}", f"localhost:{port}")
        if self.headers.get("Host", "") not in own:
            return True
        origin = self.headers.get("Origin")
        if origin is not None and origin not in tuple("http://" + h for h in own):
            return True
        return post and not self.headers.get("Content-Type", "").startswith("application/json")

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if self.foreign():
            self.safe_send(403, {"error": "forbidden"})
            return
        try:
            if u.path == "/":
                self.send(200, UI.read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/home":
                self.send(200, api_home())
            elif u.path == "/api/file":
                self.send(200, api_file_get(q.get("repo", ""), q.get("path", "")))
            elif u.path == "/api/presets":
                self.send(200, api_presets())
            elif u.path == "/api/selfupdate":
                self.send(200, api_selfupdate_status(q.get("fetch", "1") == "1"))
            elif u.path == "/api/raw":
                # файл из docs/ для предпросмотра (картинки по относительным путям)
                import mimetypes
                docs, _ = docs_root(q.get("repo", ""))
                f = (docs / safe_rel(q.get("path", ""))).resolve()
                if docs.resolve() not in f.parents or not f.is_file():
                    raise ValueError("Файл не найден")
                self.send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            elif u.path == "/api/ls":
                self.send(200, api_ls(q.get("repo", ""), q.get("path", "")))
            elif u.path == "/api/serve":
                self.send(200, PREVIEW.status())
            elif u.path == "/api/browse":
                self.send(200, api_browse(q.get("path", "")))
            elif u.path == "/api/state":
                repo = str(Path(q.get("repo", "")))
                if not yml_path(repo):
                    raise ValueError("В этой папке нет mkdocs.yml")
                cfg = load_config()
                cfg["last"] = repo
                cfg["recent"] = [repo] + [r for r in cfg.get("recent", []) if r != repo][:7]
                save_config(cfg)
                self.send(200, api_state(repo))
            else:
                self.send(404, {"error": "not found"})
        except ValueError as e:
            self.safe_send(400, {"error": str(e)})
        except Exception as e:
            log_error(self.command + " " + self.path)
            self.safe_send(500, {"error": f"Внутренняя ошибка программы: {e}. Подробности в navtool.log"})

    def do_POST(self):
        u = urlparse(self.path)
        if self.foreign(post=True):
            self.safe_send(403, {"error": "forbidden"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            if u.path == "/api/serve/start":
                if not yml_path(req.get("repo", "")):
                    raise ValueError("Не выбран проект")
                PREVIEW.start(req["repo"], bool(req.get("fast")))
                self.send(200, PREVIEW.status())
                return
            if u.path == "/api/shortcut":
                self.send(200, {"ok": ensure_shortcut(force=True)})
                return
            if u.path == "/api/render":
                self.send(200, RENDERER.render(req["repo"], req.get("text", "")))
                return
            if u.path == "/api/fix-yml":
                with LOCK:
                    self.send(200, api_fix_yml(req))
                return
            if u.path == "/api/open-yml":
                self.send(200, api_open_yml(req["repo"]))
                return
            if u.path == "/api/serve/install-node":
                if not yml_path(req.get("repo", "")):
                    raise ValueError("Не выбран проект")
                PREVIEW.install_node(req["repo"])
                self.send(200, PREVIEW.status())
                return
            if u.path == "/api/serve/stop":
                self.send(200, PREVIEW.stop())
                return
            with LOCK:
                if u.path == "/api/preview":
                    self.send(200, build_add(req))
                elif u.path == "/api/apply":
                    self.send(200, build_add(req, apply=True))
                elif u.path == "/api/op":
                    self.send(200, api_op(req))
                elif u.path == "/api/file":
                    self.send(200, api_file_save(req))
                elif u.path == "/api/presets":
                    self.send(200, api_presets(req))
                elif u.path == "/api/asset":
                    self.send(200, api_asset(req))
                elif u.path == "/api/selfupdate":
                    self.send(200, api_selfupdate())
                elif u.path == "/api/newfile":
                    self.send(200, api_newfile(req))
                elif u.path == "/api/mkdir":
                    self.send(200, api_mkdir(req))
                elif u.path == "/api/move":
                    self.send(200, api_move(req))
                elif u.path == "/api/rmfile":
                    self.send(200, api_rmfile(req))
                elif u.path == "/api/discard":
                    self.send(200, api_discard(req))
                elif u.path == "/api/section":
                    self.send(200, api_section(req))
                elif u.path == "/api/git":
                    self.send(200, api_git(req))
                elif u.path == "/api/undo":
                    self.send(200, api_undo(req["repo"]))
                else:
                    self.send(404, {"error": "not found"})
        except ValueError as e:
            self.safe_send(400, {"error": str(e)})
        except Exception as e:
            log_error(self.command + " " + self.path)
            self.safe_send(500, {"error": f"Внутренняя ошибка программы: {e}. Подробности в navtool.log"})


class Server(ThreadingHTTPServer):
    # по умолчанию HTTPServer ставит SO_REUSEADDR, и на Windows два экземпляра садятся на один порт
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def already_running(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/home", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


# ---------- режим приложения: свое окно (Edge в режиме приложения), ярлык, выход по закрытию окна ----------

APP_MODE = "--app" in sys.argv
APP_PROFILE = HERE / ".app-profile"
ICON = HERE / "navtool.ico"


def find_app_browser():
    env = os.environ
    for base in (env.get("ProgramFiles(x86)"), env.get("ProgramFiles"), env.get("LOCALAPPDATA")):
        for sub in (("Microsoft", "Edge", "Application", "msedge.exe"), ("Google", "Chrome", "Application", "chrome.exe")):
            if base and Path(base, *sub).is_file():
                return str(Path(base, *sub))
    return shutil.which("msedge") or shutil.which("chrome")


def open_window(url):
    """Окно программы без вкладок и адресной строки. Нет Edge/Chrome - обычный браузер."""
    exe = find_app_browser()
    if not exe:
        webbrowser.open(url)
        return False
    subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={APP_PROFILE}", "--no-first-run",
                      "--no-default-browser-check", "--window-size=1500,950"], creationflags=NO_WINDOW)
    return True


def app_window_alive():
    """Пока окно открыто, браузер держит lockfile в своей папке профиля: удалить его нельзя."""
    lf = APP_PROFILE / "lockfile"
    if not lf.exists():
        return False
    try:
        lf.unlink()
        return False
    except OSError:
        return True


def watch_app_window():
    """Закрыли окно программы - выключаемся (вместе с локальным сайтом)."""
    seen, gone_since = False, None
    while True:
        time.sleep(2)
        if app_window_alive():
            seen, gone_since = True, None
            continue
        if not seen:
            continue
        gone_since = gone_since or time.time()
        if time.time() - gone_since > 4:
            try:
                PREVIEW.stop_proc()
                RENDERER.stop()
            finally:
                os._exit(0)


def ensure_shortcut(force=False):
    """Ярлык «Nav Tool» на рабочем столе: запуск без консоли, со своим значком. Создается один раз."""
    if os.name != "nt":
        return False
    cfg = load_config()
    if cfg.get("shortcut") and not force:
        return False
    pyw = Path(sys.executable).with_name("pythonw.exe")
    if not pyw.is_file():
        pyw = Path(shutil.which("pythonw") or sys.executable)
    q = lambda s: str(s).replace("'", "''")
    ps = ("$d=[Environment]::GetFolderPath('Desktop');"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Nav Tool.lnk'));"
          f"$s.TargetPath='{q(pyw)}';$s.Arguments='" + '"' + q(HERE / "nav_tool.py") + '" --app' + "';"
          f"$s.WorkingDirectory='{q(HERE)}';$s.IconLocation='{q(ICON)},0';$s.Description='Nav Tool';$s.Save()")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       capture_output=True, timeout=60, creationflags=NO_WINDOW)
    if r.returncode == 0:
        cfg["shortcut"] = True
        save_config(cfg)
        return True
    log_error("shortcut: " + r.stderr.decode("cp866", "replace"))
    return False


def wait_port_free(port, seconds):
    end = time.time() + seconds
    while time.time() < end and already_running(port):
        time.sleep(0.5)


def main():
    restarted = RESTART_FLAG.exists()
    if restarted:
        # после обновления старая копия еще освобождает порт
        wait_port_free(PORT, 15)
    if already_running(PORT) and "--new" not in sys.argv:
        url = f"http://127.0.0.1:{PORT}/"
        print(f"Nav Tool уже запущен: {url}. Открываю его окно, второй экземпляр не нужен.")
        if "--no-browser" not in sys.argv:
            open_window(url)
        return
    srv = None
    ports = range(PORT, PORT + 10)
    if "--port" in sys.argv:
        # фиксированный порт (для отдельной копии): не перебираем, чтобы не занять основной 8765
        ports = [int(sys.argv[sys.argv.index("--port") + 1])]
    for port in ports:
        try:
            srv = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if not srv:
        sys.exit("Нет свободного порта 8765-8774")
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Nav Tool: {url}  (закрой это окно, чтобы остановить)")
    threading.Thread(target=find_repos, daemon=True).start()
    RESTART_FLAG.unlink(missing_ok=True)
    if "--no-browser" not in sys.argv:
        if not restarted:
            threading.Timer(0.6, lambda: open_window(url)).start()
        # окно программы закрыли - программа завершается (и после перезапуска на обновление тоже следим)
        threading.Thread(target=watch_app_window, daemon=True).start()
        if "--no-shortcut" not in sys.argv:
            threading.Thread(target=ensure_shortcut, daemon=True).start()
    refresh_path()
    kill_stale_preview()
    import atexit
    atexit.register(PREVIEW.stop_proc)
    atexit.register(RENDERER.stop)
    threading.excepthook = lambda a: (log_error("thread " + str(a.thread.name if a.thread else "")),
                                      threading.__excepthook__(a))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        log_error("server stopped")
        print("Программа упала, подробности в navtool.log")
        raise
    finally:
        PREVIEW.stop_proc()


if __name__ == "__main__":
    main()
