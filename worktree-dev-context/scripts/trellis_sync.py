#!/usr/bin/env python3
"""在 git worktree 与主仓库之间同步被 .gitignore 忽略的 .trellis 目录。

用法（在 worktree 内任意目录执行）：
  trellis_sync.py pull            # 首次带入：复制主仓库 .trellis，并建立三方合并基线
  trellis_sync.py status          # 列出 worktree / 主仓库相对基线的变化
  trellis_sync.py merge           # 预演合并回主仓库（默认不写任何文件）
  trellis_sync.py merge --apply   # 执行：只写回无冲突的文件，冲突与删除只报告

基线快照存放在 $GIT_DIR/trellis-base（worktree 私有目录，随 worktree 删除）。
.runtime/、__pycache__/、*.pyc 属于会话或缓存状态，不参与对比与合并。
"""
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager

EXCLUDE_DIRS = {".runtime", "__pycache__"}
EXCLUDE_SUFFIX = (".pyc",)


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def locate():
    git_dir = os.path.realpath(git("rev-parse", "--path-format=absolute", "--git-dir"))
    common_dir = os.path.realpath(git("rev-parse", "--path-format=absolute", "--git-common-dir"))
    if git_dir == common_dir:
        sys.exit("当前不在 linked worktree 中（--git-dir 与 --git-common-dir 相同），无需同步。")
    worktree = git("rev-parse", "--show-toplevel")
    main = os.path.dirname(common_dir)
    return {
        "wt": os.path.join(worktree, ".trellis"),
        "main": os.path.join(main, ".trellis"),
        "base": os.path.join(git_dir, "trellis-base"),
    }


def list_files(root):
    result = set()
    if not os.path.isdir(root):
        return result
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in files:
            if name.endswith(EXCLUDE_SUFFIX):
                continue
            result.add(os.path.relpath(os.path.join(cur, name), root))
    return result


def same(a, b):
    if not (os.path.isfile(a) and os.path.isfile(b)):
        return os.path.exists(a) == os.path.exists(b)
    return filecmp.cmp(a, b, shallow=False)


def copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def snapshot(src, dst):
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*EXCLUDE_DIRS, "*.pyc"), symlinks=True)


def cmd_pull(p):
    if not os.path.isdir(p["main"]):
        sys.exit(f"主仓库没有 .trellis：{p['main']}，跳过。")
    if os.path.isdir(p["wt"]):
        if not os.path.isdir(p["base"]):
            sys.exit("worktree 已有 .trellis 但缺少基线，无法安全三方合并；请先人工确认差异再决定是否重建基线。")
        print(f"worktree 已有 .trellis，不覆盖。基线：{p['base']}")
        return
    # .runtime 一并带入，task.py current 才能沿用会话的任务指针；合并时会排除它
    shutil.copytree(p["main"], p["wt"], symlinks=True)
    snapshot(p["main"], p["base"])
    print(f"已带入：{p['main']} -> {p['wt']}\n基线：{p['base']}")


def classify(p):
    """按文件给出合并动作：copy / merge / conflict / delete / main-only。"""
    wt, main, base = (list_files(p[k]) for k in ("wt", "main", "base"))
    actions = []
    for rel in sorted(wt | base):
        w, m, b = (os.path.join(p[k], rel) for k in ("wt", "main", "base"))
        if rel in base and rel not in wt:
            # 主仓库也已删除：删除已落实，不再报告（基线残留由 merge --apply 清理）
            if rel in main:
                actions.append(("delete", rel, "worktree 已删除/移动，需用户确认后在主仓库处理"))
            continue
        if rel in base and same(w, b):
            continue
        if rel not in main:
            actions.append(("conflict", rel, "主仓库已删除，worktree 已修改") if rel in base
                           else ("copy", rel, "主仓库不存在，直接写入"))
        elif same(w, m):
            continue
        elif rel in base and same(m, b):
            actions.append(("copy", rel, "主仓库未改动，直接覆盖"))
        elif rel in base:
            merged = subprocess.run(["git", "merge-file", "-p", m, b, w], capture_output=True)
            if merged.returncode == 0:
                actions.append(("merge", rel, "双方都改过，三方合并无冲突"))
            else:
                actions.append(("conflict", rel, f"双方都改过，三方合并有 {merged.returncode} 处冲突"))
        else:
            actions.append(("conflict", rel, "双方都新增了同名文件且内容不同"))
    for rel in sorted(main & base):
        if not same(os.path.join(p["main"], rel), os.path.join(p["base"], rel)) and \
                same(os.path.join(p["wt"], rel), os.path.join(p["base"], rel)):
            actions.append(("main-only", rel, "仅主仓库改动，worktree 未同步（不影响合并）"))
    return actions


def cmd_status(p):
    actions = classify(p)
    if not actions:
        print("worktree 与主仓库的 .trellis 相对基线均无差异。")
    for kind, rel, note in actions:
        print(f"[{kind}] {rel} —— {note}")


def prune_resolved_deletes(p):
    """worktree 与主仓库都已删除的文件，从基线移除；否则主仓库日后重建同名文件会被误判为 worktree 删除。"""
    for rel in sorted(list_files(p["base"]) - list_files(p["wt"]) - list_files(p["main"])):
        os.remove(os.path.join(p["base"], rel))
        print(f"[基线清理] {rel} —— 双方均已删除")


@contextmanager
def merge_lock(p):
    # 所有 worktree 共用同一主仓库锁；异常退出遗留锁时拒绝写入，避免猜测锁是否失效。
    lock = p["main"] + ".sync-lock"
    try:
        os.mkdir(lock)
    except FileExistsError:
        raise SystemExit("Trellis 合并锁已存在，请确认没有其他同步任务后处理：" + lock)
    try:
        yield
    finally:
        os.rmdir(lock)


def read_state(path):
    if os.path.islink(path):
        raise RuntimeError("拒绝写回符号链接：" + path)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def cmd_merge(p, apply):
    if apply:
        with merge_lock(p):
            return merge_files(p, True)
    return merge_files(p, False)


def merge_files(p, apply):
    before = {rel: tuple(read_state(os.path.join(p[k], rel)) for k in ("wt", "main", "base"))
              for rel in list_files(p["wt"]) | list_files(p["base"])}
    if apply:
        prune_resolved_deletes(p)
    actions = [a for a in classify(p) if a[0] != "main-only"]
    if not actions:
        print("没有需要合并回主仓库的变更。")
        return
    blocked = 0
    for kind, rel, note in actions:
        w, m, b = (os.path.join(p[k], rel) for k in ("wt", "main", "base"))
        if kind in ("conflict", "delete"):
            blocked += 1
            print(f"[跳过-{kind}] {rel} —— {note}")
            continue
        if apply:
            expected = before[rel]
            if tuple(read_state(x) for x in (w, m, b)) != expected:
                blocked += 1
                print(f"[跳过-conflict] {rel} —— 分类期间内容发生变化")
                continue
            if kind == "copy":
                data = expected[0]
            else:
                merged = subprocess.run(["git", "merge-file", "-p", m, b, w], capture_output=True, check=False)
                if merged.returncode != 0:
                    blocked += 1
                    print(f"[跳过-conflict] {rel} —— 合并失败（退出码 {merged.returncode}）")
                    continue
                data = merged.stdout
            if tuple(read_state(x) for x in (w, m, b)) != expected:
                blocked += 1
                print(f"[跳过-conflict] {rel} —— 写入前内容发生变化")
                continue
            atomic_write(m, data)
            if kind == "merge":
                # 合并结果同时写回 worktree：否则下次 worktree 仍是旧内容，会被误判为“主仓库未改动”而覆盖主仓库的修改
                atomic_write(w, data)
            # 只推进已写回文件的基线，未解决的冲突保留原基线以便再次三方合并
            atomic_write(b, data)
        print(f"[{'已写入' if apply else '将写入'}-{kind}] {rel} —— {note}")
    if not apply:
        print("\n以上为预演，未写任何文件；确认后执行 merge --apply。")
    if blocked:
        print(f"\n有 {blocked} 个文件需要用户决策（冲突/删除），未处理。")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("pull", "status", "merge"):
        sys.exit(__doc__)
    p = locate()
    if sys.argv[1] == "pull":
        cmd_pull(p)
        return
    if not os.path.isdir(p["wt"]) or not os.path.isdir(p["base"]):
        sys.exit("worktree 尚未带入 .trellis 或缺少基线，先执行 pull。")
    if sys.argv[1] == "status":
        cmd_status(p)
    else:
        cmd_merge(p, "--apply" in sys.argv[2:])


if __name__ == "__main__":
    main()
