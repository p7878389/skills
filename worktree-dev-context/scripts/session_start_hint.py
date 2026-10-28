#!/usr/bin/env python3
"""SessionStart hook：会话目录是 git linked worktree 时，注入加载 worktree-dev-context skill 的提示。

非 worktree、非 git 目录或任何异常都静默退出，不影响会话启动。
"""
import json
import os
import re
import subprocess
import sys

REGISTRY = os.path.expanduser("~/.gitnexus/registry.json")
ADVISE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "worktree_advise.py")
# 提示中的脚本写字面路径：Claude Code 隔离在 worktree 的会话会拒绝 `python3 $S/x.py` 这类运行时拼出的命令
S = "~/.claude/skills/worktree-dev-context/scripts"


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                          timeout=5, check=True).stdout.strip()


def registered_name(path):
    try:
        with open(REGISTRY, encoding="utf-8") as fh:
            entries = json.load(fh)
    except (OSError, ValueError):
        return None
    real = os.path.realpath(path)
    for entry in entries:
        if os.path.realpath(entry.get("path", "")) == real:
            return entry.get("name")
    return None


def emit(context):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}},
                     ensure_ascii=False))


def main_checkout_hint(cwd):
    """主仓库检出：有其他会话、未提交改动或未完成的 git 操作时，提示改代码前检查文件是否重叠；无此情况时静默。"""
    try:
        out = subprocess.run([sys.executable, ADVISE, "check"], cwd=cwd, capture_output=True, text=True,
                             timeout=8).stdout
    except (subprocess.SubprocessError, OSError):
        return
    lines = out.splitlines()
    verdict = next((line.split(": ", 1)[1] for line in reversed(lines) if line.startswith("VERDICT: ")), "")
    if verdict not in ("WORKTREE_REQUIRED", "PATHS_REQUIRED"):
        return

    def count(key):
        line = next((x for x in lines if x.startswith(key + ": ")), "")
        m = re.match(r"(\d+)", line[len(key) + 2:])
        return int(m.group(1)) if m else 0

    facts = [f"另有 {count('other_sessions')} 个 Claude Code/Codex 会话在用主检出"] if count("other_sessions") else []
    if count("uncommitted"):
        facts.append(f"{count('uncommitted')} 个未提交改动")
    facts += [x.split(": ", 1)[1] for x in lines if x.startswith("CONFLICT: ")]
    emit("[worktree-dev-context] 当前会话在主仓库检出：" + "；".join(facts) + "。\n"
         "- 简单修改原地做，只有要改的文件正被同时修改时才用 worktree：改代码前加载 `worktree-dev-context` skill，"
         f"定位到要改的文件后运行 `python3 {S}/worktree_advise.py check <文件或目录...>`，按 VERDICT 处理；只读任务跳过。")


def link_agent_config(root):
    """worktree 缺少主检出被忽略的 .claude/hooks 等 Agent 配置时自动软链，返回新链接的相对路径。"""
    try:
        out = subprocess.run([sys.executable, ADVISE, "link"], cwd=root, capture_output=True, text=True,
                             timeout=8).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return [line[len("LINKED: "):].split(" -> ", 1)[0] for line in out.splitlines() if line.startswith("LINKED: ")]


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    cwd = payload.get("cwd") or os.getcwd()
    try:
        git_dir = os.path.realpath(git(cwd, "rev-parse", "--path-format=absolute", "--git-dir"))
        common_dir = os.path.realpath(git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        if git_dir == common_dir:
            main_checkout_hint(cwd)
            return
        root = git(cwd, "rev-parse", "--show-toplevel")
        branch = git(cwd, "branch", "--show-current") or "(detached)"
    except (subprocess.SubprocessError, OSError):
        return
    main_root = os.path.dirname(common_dir)
    trellis_state = (
        "已带入" if os.path.isdir(os.path.join(root, ".trellis")) and os.path.isdir(os.path.join(git_dir, "trellis-base"))
        else "未带入" if os.path.isdir(os.path.join(main_root, ".trellis"))
        else "主仓库无 .trellis"
    )
    index_name = registered_name(root)
    linked = link_agent_config(root)
    context = (
        f"[worktree-dev-context] 当前会话位于 git linked worktree：{root}（分支 {branch}），主仓库：{main_root}。\n"
        f"- Trellis：{trellis_state}；GitNexus 独立索引：{index_name or '未建立'}。\n"
        "- 其他 SessionStart 注入的 Trellis 分支/任务状态、GitNexus“落后 N 个提交”均以主仓库检出为准，不代表本 worktree。\n"
        f"- 第一次读写代码前先加载 `worktree-dev-context` skill：执行 `python3 {S}/trellis_sync.py pull` 与 "
        f"`python3 {S}/index_check.py`（写字面路径，不要用 shell 变量拼脚本路径），按 VERDICT 选定 GitNexus repo；"
        "完成后回合 .trellis；仅在已授权集成/部署时按 skill 第 3 节收尾，普通提交不自动合并、删除或部署。"
    )
    if linked:
        context += (f"\n- 已把主仓库被忽略的 Agent 配置软链进本 worktree：{', '.join(linked)}。"
                    "项目 hook 现在可用；本会话启动时不存在的 project settings/agents 如未生效，重开会话即可。")
    if not re.match(r"(feature|fix|hotfix)/", branch):
        context += (
            f"\n- 分支 {branch} 不符合命名规范：需求用 feature/<简述>，bug 用 fix/ 或 hotfix/<简述>，不带 hash 后缀；"
            "第一次提交前按 skill 改名。"
        )
    emit(context)


if __name__ == "__main__":
    main()
