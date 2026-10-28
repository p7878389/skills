#!/usr/bin/env python3
"""判断当前 git worktree 应该使用哪份 GitNexus 索引。

用法（在 worktree 内执行）：
  index_check.py [待分析的文件或目录 ...]

输出 VERDICT 行，供调用方据此行动：
  USE_WORKTREE <name>   worktree 已有独立索引且与 HEAD 一致，MCP 调用传 repo=<name>
  REFRESH <name>        worktree 已有独立索引但落后，执行 REFRESH_CMD 增量刷新后使用
  USE_MAIN <name>       无独立索引，但主仓库索引覆盖了待分析文件，可直接使用
  BUILD <name>          需要为 worktree 建独立索引，执行 BUILD_CMD（首次约 1 分钟）

注意：主仓库索引的“过期提示”比对的是主仓库检出的 HEAD，不代表 worktree；本脚本按 worktree HEAD 计算。
未传路径时，按“索引提交之后 worktree 有任何代码变化”判断，结论偏保守。
"""
import json
import hashlib
import os
import re
import subprocess
import sys

REGISTRY = os.path.expanduser("~/.gitnexus/registry.json")


def git(*args, check=True):
    # 关闭路径转义：默认 core.quotePath 会把中文路径输出成 "\346..."，与传入的目标路径前缀匹配不上
    proc = subprocess.run(["git", "-c", "core.quotePath=false", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout.strip(), proc.returncode


def registry():
    try:
        with open(REGISTRY, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def find_entry(entries, path):
    real = os.path.realpath(path)
    for entry in entries:
        if os.path.realpath(entry.get("path", "")) == real:
            return entry
    return None


def changed_since(commit):
    """索引提交之后的已提交变化 + 未提交变化（含未跟踪文件）。"""
    committed, _ = git("diff", "--name-only", f"{commit}..HEAD")
    uncommitted, _ = git("diff", "--name-only", "HEAD")
    untracked, _ = git("ls-files", "--others", "--exclude-standard")
    return set(filter(None, "\n".join((committed, uncommitted, untracked)).splitlines()))


def relevant(files, targets, root):
    if not targets:
        return sorted(files)
    prefixes = [os.path.relpath(os.path.realpath(t), root).rstrip("/") for t in targets]
    if any(p in ("", ".") for p in prefixes):
        return sorted(files)
    return sorted(f for f in files if any(f == p or f.startswith(p + "/") for p in prefixes))


def main():
    git_dir, _ = git("rev-parse", "--path-format=absolute", "--git-dir")
    common_dir, _ = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    root, _ = git("rev-parse", "--show-toplevel")
    branch, _ = git("branch", "--show-current")
    head, _ = git("rev-parse", "HEAD")
    main_root = os.path.dirname(os.path.realpath(common_dir))
    in_worktree = os.path.realpath(git_dir) != os.path.realpath(common_dir)

    entries = registry()
    main_entry = find_entry(entries, main_root)
    base_name = main_entry["name"] if main_entry else os.path.basename(main_root)
    slug = re.sub(r"[^\w.-]+", "-", branch or head[:10]).strip("-")
    slug += "-" + hashlib.sha256((branch or head).encode()).hexdigest()[:12]
    # 本地 worktree 用 @wt- 前缀，避开 ELK MCP 的 <repo>@<分支> / <repo>@<commit10> 命名，防止同名注册
    wt_name = f"{base_name}@wt-{slug}"
    print(f"worktree: {root} ({'linked worktree' if in_worktree else '主仓库检出'})")
    print(f"branch: {branch or '(detached)'}  HEAD: {head[:9]}")

    wt_entry = find_entry(entries, root) if in_worktree else main_entry
    if wt_entry:
        name, last = wt_entry["name"], wt_entry.get("lastCommit", "")
        dirty = changed_since(last) if last else {"(无记录提交)"}
        print(f"index: {name}  indexed: {last[:9]}  分支: {wt_entry.get('branch')}")
        dup = [e["path"] for e in entries if e.get("name") == name
               and os.path.realpath(e.get("path", "")) != os.path.realpath(root)]
        if in_worktree and (name == base_name or dup):
            print(f"WARN: 索引名 {name} 与其他注册项重名（{dup or main_root}），按名称调用会歧义；执行 "
                  f"(cd {root} && gitnexus analyze --index-only --name \"{wt_name}\") 改名")
            print(f"VERDICT: RENAME_REQUIRED {wt_name}")
            return
        if last == head and not relevant(dirty, sys.argv[1:], root):
            print(f"VERDICT: USE_WORKTREE {name}")
        else:
            print(f"变化文件（相关）: {len(relevant(dirty, sys.argv[1:], root))}")
            print(f"REFRESH_CMD: (cd {root} && gitnexus analyze --index-only --name \"{name}\")")
            print(f"VERDICT: REFRESH {name}")
        return

    if not main_entry:
        print(f"主仓库未建索引：{main_root}")
        print(f"BUILD_CMD: (cd {root} && gitnexus analyze --index-only --name \"{wt_name}\")")
        print(f"VERDICT: BUILD {wt_name}")
        return

    last = main_entry.get("lastCommit", "")
    _, not_ancestor = git("merge-base", "--is-ancestor", last, "HEAD", check=False)
    hits = relevant(changed_since(last), sys.argv[1:], root) if last else ["(无记录提交)"]
    behind = git("rev-list", "--count", f"{last}..HEAD", check=False)[0] if last else "?"
    print(f"main index: {main_entry['name']}  indexed: {last[:9]} on {main_entry.get('branch')}  "
          f"落后 worktree HEAD {behind} 个提交  是否祖先: {'否' if not_ancestor else '是'}")
    if not not_ancestor and not hits:
        print(f"VERDICT: USE_MAIN {main_entry['name']}")
        return
    print(f"索引之后变化的相关文件 {len(hits)} 个：" + ", ".join(hits[:10]) + (" ..." if len(hits) > 10 else ""))
    print(f"BUILD_CMD: (cd {root} && gitnexus analyze --index-only --name \"{wt_name}\")")
    print(f"VERDICT: BUILD {wt_name}")


if __name__ == "__main__":
    main()
