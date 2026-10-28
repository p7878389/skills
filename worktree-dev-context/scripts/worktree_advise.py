#!/usr/bin/env python3
"""在主仓库检出中开始开发任务前，判断是否应改用 git worktree，并按约定创建。

用法（在仓库内任意目录执行）：
  worktree_advise.py check [--branch <任务分支>] [--base <基线>] [要修改的文件或目录 ...]
  worktree_advise.py create --name <目录名> --branch <新分支> [--base <基线>]
  worktree_advise.py integrate [--target <集成分支>] [--expect-plan <摘要> --yes]
  worktree_advise.py link       # 在 linked worktree 中执行：链接主检出被忽略的 Agent 配置

口径：简单修改在主检出原地做；只有本次要改的文件正被同时修改时才用 worktree。
check 最后一行输出 VERDICT：
  IN_WORKTREE           已在 linked worktree，按 skill 第 1 节带入 Trellis、选索引
  NOT_APPLICABLE <原因>  非 git 仓库 / 子模块 / 裸仓库，不适用
  USE_EXISTING <路径>   任务分支已在某个 worktree 检出（git 不允许同一分支检出两次），直接进入该路径
  WORKTREE_REQUIRED     有冲突（CONFLICT 行）：要改的文件与主检出未提交改动重叠 / 工作区被占用时需换分支 /
                        有未完成的 merge、rebase 等 → 直接创建
  PATHS_REQUIRED        主检出有其他会话或未提交改动，但没传要改的文件，无法判断是否重叠
  STAY                  无冲突，原地修改（工作区被占用时只提交自己的文件）

基线约定：显式 --base 优先；否则从 main/master 切出（优先远端 origin/<分支>，create 时先 fetch）；
仓库同时存在 js-main/js-master 时有两条产品线，必须询问用户（输出 BASE: ASK）。

integrate：把当前分支（已提交）推送到 origin，再在临时 detached worktree 中把 origin/<分支> 合并进
origin/<集成分支>（唯一候选可预演，多产品线须指定 --target）并推送，不切换当前检出的分支。不加 --yes 只预演。
最后一行输出 VERDICT：
  READY <集成分支>         预演通过，可合并（显式 --target、--expect-plan 与 --yes 执行）
  MERGED <集成分支> <提交>  已合并并推送
  UP_TO_DATE <集成分支>    集成分支已包含当前分支全部提交，无需合并
  CONFLICT <集成分支>      合并有冲突（CONFLICT 行列出文件），未做任何改动
  TARGET_ASK <候选>        无法判断集成分支（beta/js-beta 都存在且产品线不明），需询问用户后带 --target
  BLOCKED <原因>           未提交改动 / detached / 当前分支就是集成或基线分支 / 推送被拒等

link：Trellis 等工具装在 .claude/（hooks、agents、commands、settings.json）、.codex、AGENTS.md 等被
.gitignore 忽略的位置，新 worktree 里没有，项目 hook 以相对路径 `python3 .claude/hooks/x.py` 注册时每次都报
can't open file。link 把主检出中这些被忽略、未跟踪且 worktree 缺失的项软链进 worktree（.claude 按子项链接，
跳过 .claude/worktrees）；已存在的不动。create 会自动执行。.trellis 由 trellis_sync.py 复制，不在此列。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

PRIMARY_BASES = ("main", "master")
ASK_BASES = ("js-main", "js-master")
# 集成分支及其产品线基线；多个候选时由用户指定目标
INTEGRATION_LINES = {"beta": PRIMARY_BASES, "js-beta": ASK_BASES}
# 主检出根目录下的 Agent 配置；.claude 单独按子项处理
AGENT_CONFIG = (".codex", ".agents", ".cursor", ".opencode", ".gemini", "AGENTS.md", "CLAUDE.md", "GEMINI.md")
CLAUDE_SKIP = {"worktrees"}
ELK_WORKTREES = os.path.realpath(os.path.expanduser("~/.cache/elk-log-analysis/worktrees"))
AGENT_EXES = {"claude", "codex"}
IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply")
NAME_SEGMENT = re.compile(r"^[\w.-]+$")


def git(*args, check=True, timeout=None, strip=True):
    # 关闭路径转义：中文路径原样输出
    proc = subprocess.run(["git", "-c", "core.quotePath=false", *args], capture_output=True, text=True,
                          timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} 失败")
    return (proc.stdout.strip() if strip else proc.stdout), proc.returncode


def uncommitted():
    """未提交改动（含未跟踪、不含忽略）的路径；-z 输出不转义且保留行首状态列。"""
    out, _ = git("status", "--porcelain", "-z", "--untracked-files=normal", strip=False)
    entries, files, i = out.split("\0"), [], 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) > 3:
            files.append(entry[3:])
            if entry[0] in "RC" and i + 1 < len(entries):  # 重命名/复制的下一项是原路径，两端都算在改
                i += 1
                files.append(entries[i])
        i += 1
    return files


def locate():
    """返回仓库信息；不适用时返回 (None, 原因)。"""
    try:
        inside, _ = git("rev-parse", "--is-inside-work-tree")
    except RuntimeError:
        return None, "不在 git 仓库内"
    if inside != "true":
        return None, "不在工作区内（裸仓库或 .git 目录）"
    if git("rev-parse", "--show-superproject-working-tree", check=False)[0]:
        return None, "位于子模块中"
    git_dir = os.path.realpath(git("rev-parse", "--path-format=absolute", "--git-dir")[0])
    common_dir = os.path.realpath(git("rev-parse", "--path-format=absolute", "--git-common-dir")[0])
    return {
        "git_dir": git_dir,
        "common_dir": common_dir,
        "root": os.path.realpath(git("rev-parse", "--show-toplevel")[0]),
        "main_root": os.path.dirname(common_dir),
        "in_worktree": git_dir != common_dir,
        "branch": git("branch", "--show-current")[0],
    }, None


def worktrees():
    """[(路径, 分支或 None)]，第一项是主检出。"""
    out, _ = git("worktree", "list", "--porcelain")
    result, path = [], None
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            path = os.path.realpath(line[len("worktree "):])
            branch = None
        elif line.startswith("branch "):
            branch = line[len("branch refs/heads/"):]
        elif not line and path:
            result.append((path, branch))
            path = None
    return result


def ancestors():
    pids, pid = set(), os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        pid = int(out) if out.isdigit() else 0
    return pids


def process_cwds(pids):
    if not pids:
        return {}
    if os.path.isdir("/proc"):
        cwds = {}
        for pid in pids:
            try:
                cwds[pid] = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                pass
        return cwds
    out = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))],
                         capture_output=True, text=True, timeout=5).stdout
    cwds, cur = {}, None
    for line in out.splitlines():
        if line.startswith("p"):
            cur = int(line[1:])
        elif line.startswith("n") and cur:
            cwds[cur] = line[1:]
    return cwds


def other_sessions(main_root, linked):
    """以主检出（不含其下 linked worktree）为工作目录的其他 Claude Code / Codex 会话。"""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    mine = ancestors()
    pids = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and os.path.basename(parts[1]) in AGENT_EXES \
                and int(parts[0]) not in mine:
            pids.append(int(parts[0]))
    try:
        cwds = process_cwds(pids)
    except (OSError, subprocess.SubprocessError):
        return None
    hits = []
    for pid, cwd in cwds.items():
        real = os.path.realpath(cwd)
        if not (real == main_root or real.startswith(main_root + os.sep)):
            continue
        if any(real == p or real.startswith(p + os.sep) for p in linked):
            continue
        hits.append(pid)
    return sorted(hits)


def ref_exists(ref):
    return git("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)[1] == 0


def resolve_base(explicit):
    """返回 (基线 ref 或 None, 说明, 待询问候选)。"""
    if explicit:
        if not ref_exists(explicit) and not ref_exists("origin/" + explicit):
            raise SystemExit(f"基线 {explicit} 不存在")
        return explicit, "需求/参数指定", []

    def exists(name):
        return ref_exists("refs/heads/" + name) or ref_exists("refs/remotes/origin/" + name)

    primary = [b for b in PRIMARY_BASES if exists(b)]
    js = [b for b in ASK_BASES if exists(b)]
    if js:
        return None, "存在 " + "/".join(js) + "，两条产品线需由用户选择基线", primary + js
    if not primary:
        return None, "没有 main/master 分支，需由用户指定基线", []
    if len(primary) > 1:
        head, _ = git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
        name = head.split("/", 1)[1] if head.startswith("origin/") and head.split("/", 1)[1] in primary else "main"
    else:
        name = primary[0]
    return name, "默认基线（main/master）", []


def cmd_check(args):
    info, reason = locate()
    if not info:
        print(f"VERDICT: NOT_APPLICABLE {reason}")
        return
    print(f"checkout: {info['root']} ({'linked worktree' if info['in_worktree'] else '主仓库检出'})  "
          f"branch: {info['branch'] or '(detached)'}")
    if info["in_worktree"]:
        print("VERDICT: IN_WORKTREE")
        return

    trees = worktrees()
    linked = [p for p, _ in trees if p != info["main_root"]]
    if args.branch and args.branch != info["branch"]:
        for path, branch in trees:
            if branch == args.branch:
                print(f"任务分支 {args.branch} 已在 {path} 检出")
                print(f"VERDICT: USE_EXISTING {path}")
                return

    sessions = other_sessions(info["main_root"], linked)
    if sessions is None:
        print("other_sessions: 无法检测（ps/lsof 不可用）")
    else:
        print(f"other_sessions: {len(sessions)}" + (f"  pid={','.join(map(str, sessions))}" if sessions else ""))
    files = uncommitted()
    print(f"uncommitted: {len(files)}" + (f"  {', '.join(files[:10])}{' ...' if len(files) > 10 else ''}" if files else ""))
    base, note, candidates = resolve_base(args.base)
    print(f"BASE: {base} （{note}）" if base else f"BASE: ASK {','.join(candidates) or '(无候选)'} （{note}）")

    # 口径：简单修改原地做；只有“本次要改的文件正被同时修改”才用 worktree。
    # 换分支、未完成的 merge/rebase 会改动整个工作区，在工作区被占用时等同于全部文件重叠。
    shared = bool(sessions) or bool(files)
    conflicts = []
    ops = [n for n in IN_PROGRESS if os.path.exists(os.path.join(info["git_dir"], n))]
    if ops:
        conflicts.append("主检出有未完成的 git 操作：" + ", ".join(ops) + "，此时修改会混入该操作")
    if args.branch and args.branch != info["branch"] and shared:
        conflicts.append(f"任务需要分支 {args.branch}（主检出在 {info['branch'] or '(detached)'}），"
                         "工作区被占用时原地切换会影响所有文件")
    if args.paths:
        overlap = relevant(files, args.paths, info["root"])
        print(f"paths: {', '.join(args.paths)}  overlap: {len(overlap)}"
              + (f"  {', '.join(overlap[:10])}{' ...' if len(overlap) > 10 else ''}" if overlap else ""))
        if overlap:
            conflicts.append(f"要修改的文件中有 {len(overlap)} 个正在被修改（未提交）：{', '.join(overlap[:5])}"
                             + (" ..." if len(overlap) > 5 else ""))
    for c in conflicts:
        print(f"CONFLICT: {c}")
    if conflicts:
        print("VERDICT: WORKTREE_REQUIRED")
    elif shared and not args.paths:
        print("NOTE: 主检出有其他会话或未提交改动，需传入本次要修改的文件/目录判断是否重叠")
        print("VERDICT: PATHS_REQUIRED")
    else:
        if shared:
            print("NOTE: 不重叠，原地修改；提交时只 git add 自己的文件，不用 -A/-a，提交前带同样路径再 check 一次")
        print("VERDICT: STAY")


def relevant(files, targets, root):
    """files 中落在 targets（文件或目录，相对当前目录）里的仓库相对路径。"""
    prefixes = [os.path.relpath(os.path.realpath(t), root).rstrip("/") for t in targets]
    if any(p in ("", ".") for p in prefixes):
        return sorted(files)
    return sorted(f for f in files if any(f == p or f.startswith(p + "/") for p in prefixes))


def ensure_ignored(info, path):
    if git("check-ignore", "-q", path, check=False)[1] == 0:
        return
    # 仓库 .gitignore 未忽略时写入本地 exclude（不进版本库），避免 worktree 目录出现在主检出的未跟踪文件里
    exclude = os.path.join(info["common_dir"], "info", "exclude")
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    with open(exclude, "a", encoding="utf-8") as fh:
        fh.write("\n/.claude/worktrees/\n")
    print(f"已在 {exclude} 忽略 /.claude/worktrees/")


def start_point(base):
    """优先远端最新：fetch origin <base> 后用 origin/<base>；fetch 失败时退回本地。"""
    if base.startswith("origin/") or not ref_exists("refs/remotes/origin/" + base):
        return base
    _, rc = git("fetch", "--quiet", "origin", base, check=False, timeout=60)
    if rc != 0:
        print(f"WARN: fetch origin {base} 失败，使用本地已有的引用")
    return "origin/" + base


def cmd_create(args):
    info, reason = locate()
    if not info:
        raise SystemExit(f"不适用：{reason}")
    if args.name in (".", "..") or not NAME_SEGMENT.fullmatch(args.name) or len(args.name) > 64:
        raise SystemExit("--name 每段只能包含字母、数字、. _ -，总长不超过 64")
    if git("check-ref-format", "--branch", args.branch, check=False)[1] != 0:
        raise SystemExit(f"非法分支名：{args.branch}")
    path = os.path.join(info["main_root"], ".claude", "worktrees", args.name)
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(os.path.dirname(path)):
        raise SystemExit("worktree 路径超出允许目录")
    if os.path.exists(path):
        raise SystemExit(f"目录已存在：{path}")
    for wt_path, branch in worktrees():
        if branch == args.branch:
            raise SystemExit(f"分支 {args.branch} 已在 {wt_path} 检出，直接进入该 worktree")
    ensure_ignored(info, path)

    if ref_exists("refs/heads/" + args.branch):
        git("worktree", "add", path, args.branch)
        used = f"已有分支 {args.branch}"
    else:
        base, note, candidates = resolve_base(args.base)
        if not base:
            raise SystemExit(f"需要询问用户基线：{note}；候选 {', '.join(candidates) or '无'}，确认后带 --base 重试")
        ref = start_point(base)
        # --no-track：不把 origin/<基线> 设为上游，避免之后 pull/push 误作用到 main/master
        git("worktree", "add", "--no-track", "-b", args.branch, path, ref)
        used = f"{ref} @ {git('rev-parse', '--short', ref)[0]}（{note}）"
    print(f"WORKTREE: {path}")
    print(f"BRANCH: {args.branch}  BASE: {used}")
    linked = link_agent_config(info["main_root"], path)
    if linked:
        print(f"LINKED: {', '.join(linked)}")
    print("NEXT: Claude Code 用 EnterWorktree(path=上述路径) 切入；Codex 后续命令以该路径为 workdir、文件用绝对路径；"
          "然后按 skill 第 1 节按需执行 Trellis 同步和 GitNexus 索引检查；未使用/不可用时跳过（脚本写字面路径）")


def agent_config_entries(main_root):
    """主检出中被忽略且未跟踪的 Agent 配置（相对路径）。"""
    entries = [n for n in AGENT_CONFIG if os.path.lexists(os.path.join(main_root, n))]
    claude = os.path.join(main_root, ".claude")
    if os.path.isdir(claude) and not os.path.islink(claude):
        entries += [".claude/" + n for n in sorted(os.listdir(claude)) if n not in CLAUDE_SKIP]
    if not entries:
        return []
    ignored = set(git("-C", main_root, "check-ignore", "--", *entries, check=False)[0].splitlines())
    tracked = git("-C", main_root, "ls-files", "--", *entries)[0].splitlines()
    return [e for e in entries
            if e in ignored and not any(t == e or t.startswith(e + "/") for t in tracked)]


def link_agent_config(main_root, root):
    """把缺失的 Agent 配置软链进 worktree，返回新链接的相对路径；worktree 已有的（文件或链接）不动。"""
    linked = []
    for rel in agent_config_entries(main_root):
        dst = os.path.join(root, rel)
        if os.path.lexists(dst):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.symlink(os.path.join(main_root, rel), dst)
        linked.append(rel)
    return linked


def cmd_link(args):
    info, reason = locate()
    if not info:
        print(f"VERDICT: NOT_APPLICABLE {reason}")
        return
    if not info["in_worktree"]:
        print("VERDICT: NOT_APPLICABLE 主仓库检出无需链接")
        return
    if info["root"].startswith(ELK_WORKTREES + os.sep):
        print("VERDICT: NOT_APPLICABLE ELK MCP 只读副本")
        return
    linked = link_agent_config(info["main_root"], info["root"])
    for rel in linked:
        print(f"LINKED: {rel} -> {os.path.join(info['main_root'], rel)}")
    print(f"VERDICT: LINKED {len(linked)}" if linked else "VERDICT: UP_TO_DATE")


def remote_exists(name):
    return ref_exists("refs/remotes/origin/" + name)


def resolve_target(explicit, branch):
    """返回 (集成分支或 None, 说明, 候选)。"""
    if explicit:
        if not remote_exists(explicit):
            raise SystemExit(f"origin/{explicit} 不存在")
        return explicit, "参数指定", []
    found = [t for t in INTEGRATION_LINES if remote_exists(t)]
    if not found:
        return None, "origin 上没有 " + "/".join(INTEGRATION_LINES), []
    if len(found) == 1:
        return found[0], "唯一的集成分支", []
    return None, "存在多条产品线，请明确指定 --target", found


def cmd_integrate(args):
    info, reason = locate()
    if not info:
        print(f"VERDICT: BLOCKED {reason}")
        return 1
    branch = info["branch"]
    print(f"checkout: {info['root']}  branch: {branch or '(detached)'}")
    if not branch:
        print("VERDICT: BLOCKED detached HEAD，先切到任务分支")
        return 1
    if branch in PRIMARY_BASES + ASK_BASES or branch in INTEGRATION_LINES:
        print(f"VERDICT: BLOCKED 当前在 {branch}，integrate 只用于任务分支")
        return 1
    dirty = uncommitted()
    if dirty:
        print(f"uncommitted: {len(dirty)}  {', '.join(dirty[:10])}{' ...' if len(dirty) > 10 else ''}")
        print("VERDICT: BLOCKED 有未提交改动，先提交（只 add 自己的文件）")
        return 1
    git("fetch", "--quiet", "origin", timeout=120)
    target, note, candidates = resolve_target(args.target, branch)
    if not target:
        print(f"TARGET: ASK （{note}）")
        print(f"VERDICT: TARGET_ASK {','.join(candidates)}")
        return 1
    print(f"TARGET: {target} （{note}）")
    remote = git("remote", "get-url", "origin")[0]
    print(f"REPO: {os.path.basename(remote[:-4] if remote.endswith('.git') else remote)}  ORIGIN: {remote}")

    head = git("rev-parse", "HEAD")[0]
    target_head = git("rev-parse", f"origin/{target}")[0]
    plan = {"origin": remote, "branch": branch, "head": head,
            "target": target, "target_head": target_head}
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    print("PLAN_SHA256: " + digest)
    if args.yes and (not args.target or getattr(args, "expect_plan", None) != digest):
        print("VERDICT: BLOCKED 执行需显式 --target 和匹配的 --expect-plan；请重新预演确认")
        return 1
    published = remote_exists(branch) and git("rev-parse", f"origin/{branch}")[0] == head
    print("TASK_BRANCH_PUBLISHED: " + ("yes" if published else "no"))
    commits = git("log", "--format=%h %s", f"origin/{target}..HEAD")[0]
    lines = commits.splitlines()
    print(f"commits: {len(lines)}")
    for line in lines[:20]:
        print(f"  {line}")
    if len(lines) > 20:
        print("  ...")
    changed = git("diff", "--name-only", f"origin/{target}...HEAD")[0].splitlines()
    dirs = sorted({c.split("/", 1)[0] if "/" in c else "." for c in changed})
    print(f"CHANGED_DIRS: {', '.join(dirs)}  （{len(changed)} 个文件，部署时据此定位服务/模块）")

    conflicts = merge_conflicts(f"origin/{target}", "HEAD")
    if conflicts:
        for f in conflicts:
            print(f"CONFLICT: {f}")
        print(f"VERDICT: CONFLICT {target}")
        return 1
    if not args.yes:
        print(f"PLAN: push origin {branch} → 临时 worktree 合并 origin/{branch} 到 origin/{target} → push origin {target}")
        print(f"VERDICT: {'READY' if commits else 'UP_TO_DATE'} {target}")
        return 0

    # 团队惯例：先推任务分支，再以 remote-tracking 分支合入集成分支
    if git("push", "--quiet", "-u", "origin", f"{branch}:{branch}", check=False, timeout=120)[1] != 0:
        print(f"VERDICT: BLOCKED push origin {branch} 被拒绝（远端分支有本地没有的提交），先与远端同步")
        return 1
    print("TASK_BRANCH_PUBLISHED: yes")
    if not commits:
        print(f"VERDICT: UP_TO_DATE {target}")
        return 0
    message = f"Merge remote-tracking branch 'origin/{branch}' into {target}"
    # 固定已预演的源与目标提交。远端推进导致 push 拒绝时停止，不擅自合入新内容。
    result = merge_in_temp(target, head, message, target_head)
    if result[0] == "conflict":
        for f in result[1]:
            print(f"CONFLICT: {f}")
        print(f"VERDICT: CONFLICT {target}")
        return 1
    if result[0] != "pushed":
        print(f"VERDICT: BLOCKED push origin {target} 失败；检查远端及权限后重新预演")
        return 1
    sha = result[1]
    if ref_exists("refs/heads/" + target) and \
            git("fetch", "--quiet", "origin", f"{target}:{target}", check=False, timeout=60)[1] != 0:
        print(f"NOTE: 本地 {target} 未更新，以 origin/{target} 为准")
    print(f"VERDICT: MERGED {target} {sha}")
    return 0


def merge_conflicts(ours, theirs):
    """不改动任何检出，用 merge-tree 预判冲突文件。"""
    out, rc = git("merge-tree", "--write-tree", "--name-only", "--no-messages", ours, theirs, check=False)
    if rc == 0:
        return []
    if rc == 1:
        return out.splitlines()[1:] or ["(未知文件)"]
    raise RuntimeError(f"git merge-tree 失败（需要 git ≥ 2.38）：{out}")


def merge_in_temp(target, source, message, target_head):
    """在临时 detached worktree 中合并并推送；返回 ("pushed", sha) / ("conflict", 文件) / ("rejected", None)。"""
    tmp = tempfile.mkdtemp(prefix="wt-integrate-")
    path = os.path.join(tmp, "wt")
    try:
        git("worktree", "add", "--quiet", "--detach", path, target_head)
        _, rc = git("-C", path, "merge", "--no-ff", "--no-edit", "-m", message, source, check=False)
        if rc != 0:
            files = git("-C", path, "diff", "--name-only", "--diff-filter=U", check=False)[0].splitlines()
            git("-C", path, "merge", "--abort", check=False)
            return "conflict", files or ["(合并失败，未列出冲突文件)"]
        sha = git("-C", path, "rev-parse", "--short", "HEAD")[0]
        if git("-C", path, "push", "--quiet", "origin", f"HEAD:refs/heads/{target}", check=False, timeout=120)[1] != 0:
            return "rejected", None
        return "pushed", sha
    finally:
        # 临时 worktree 由本命令创建，内容已推送或放弃，可强制删除
        git("worktree", "remove", "--force", path, check=False)
        git("worktree", "prune", check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("check")
    p.add_argument("--branch", help="任务要在哪个分支上开发（新建或已有）；沿用主检出当前分支时不传")
    p.add_argument("--base", help="需求指定的基线分支/提交")
    p.add_argument("paths", nargs="*", help="本次要修改的文件或目录（相对当前目录）")
    p = sub.add_parser("create")
    p.add_argument("--name", required=True, help=".claude/worktrees/ 下的目录名")
    p.add_argument("--branch", required=True, help="worktree 检出的分支，不存在时按基线新建")
    p.add_argument("--base", help="需求指定的基线分支/提交")
    p = sub.add_parser("integrate")
    p.add_argument("--target", help="集成分支；多产品线和执行时必须指定")
    p.add_argument("--expect-plan", help="已确认预演输出的 PLAN_SHA256，执行时必填")
    p.add_argument("--yes", action="store_true", help="真正推送并合并；不加只预演")
    sub.add_parser("link")
    args = parser.parse_args(sys.argv[1:] or ["check"])
    if args.cmd == "integrate":
        sys.exit(cmd_integrate(args))
    if args.cmd == "link":
        return cmd_link(args)
    (cmd_create if args.cmd == "create" else cmd_check)(args)


if __name__ == "__main__":
    main()
