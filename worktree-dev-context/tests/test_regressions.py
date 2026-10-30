import contextlib
import io
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
import subprocess
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import index_check as idx
import trellis_sync as ts
import worktree_advise as wa

class Regressions(unittest.TestCase):
    def test_root_covers_all_changes(self):
        root = str(pathlib.Path.cwd())
        self.assertEqual(idx.relevant({'src/A.java'}, [root], root), ['src/A.java'])

    def test_branch_slugs_do_not_collide(self):
        # Exercise CLI output with registry and Git replaced to avoid touching real repositories.
        def name(branch):
            values = {'--git-dir':'/repo/.git/worktrees/a', '--git-common-dir':'/repo/.git',
                      '--show-toplevel':'/repo/wt', '--show-current':branch, 'HEAD':'abcdef123456'}
            def git(*args, **kw): return values.get(args[-1], ''), 0
            out = io.StringIO()
            with patch.object(idx,'git',side_effect=git), patch.object(idx,'registry',return_value=[]), contextlib.redirect_stdout(out):
                idx.main()
            return out.getvalue().split('VERDICT: BUILD ')[1].strip()
        self.assertNotEqual(name('feature/a-b'), name('feature-a/b'))

    def test_parent_directory_name_rejected_before_git_changes(self):
        import argparse
        args = argparse.Namespace(name='../../escape',branch='fix/test',base='main')
        with patch.object(wa,'locate',return_value=({'main_root':'/tmp/repo'},None)), patch.object(wa,'git',side_effect=AssertionError('must reject before git')):
            with self.assertRaises(SystemExit): wa.cmd_create(args)

    def state(self, d):
        p = {k:str(pathlib.Path(d,k)) for k in ('wt','main','base')}
        for v in p.values(): pathlib.Path(v).mkdir()
        return p

    def test_modify_delete_is_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.state(d)
            pathlib.Path(p['base'],'task').write_text('old')
            pathlib.Path(p['wt'],'task').write_text('changed')
            self.assertEqual(ts.classify(p)[0][0], 'conflict')

    def test_failed_second_merge_preserves_all_files(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.state(d)
            for k in p: pathlib.Path(p[k],'task').write_text(k)
            with patch.object(ts,'classify',return_value=[('merge','task','ok')]), patch.object(ts.subprocess,'run',return_value=subprocess.CompletedProcess([],1,b'conflict',b'')), contextlib.redirect_stdout(io.StringIO()):
                ts.cmd_merge(p,True)
            for k in p: self.assertEqual(pathlib.Path(p[k],'task').read_text(),k)

    def test_clean_merge_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.state(d)
            for k in p: pathlib.Path(p[k],'task').write_text('old\n')
            pathlib.Path(p['wt'],'task').write_text('new\n')
            with contextlib.redirect_stdout(io.StringIO()):
                ts.cmd_merge(p,True)
                ts.cmd_merge(p,True)
            self.assertEqual(pathlib.Path(p['main'],'task').read_text(),'new\n')
            self.assertEqual(ts.classify(p),[])

    def test_shared_lock_prevents_another_writer(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.state(d)
            pathlib.Path(p['main']+'.sync-lock').mkdir()
            with self.assertRaises(SystemExit): ts.cmd_merge(p,True)
            self.assertTrue(pathlib.Path(p['main']+'.sync-lock').is_dir())

    def test_external_change_during_classification_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.state(d)
            for k in p: pathlib.Path(p[k],'task').write_text('base')
            pathlib.Path(p['wt'],'task').write_text('ours')
            def classify(_):
                pathlib.Path(p['main'],'task').write_text('other writer')
                return [('copy','task','previously unchanged')]
            with patch.object(ts,'classify',side_effect=classify), contextlib.redirect_stdout(io.StringIO()):
                ts.cmd_merge(p,True)
            self.assertEqual(pathlib.Path(p['main'],'task').read_text(),'other writer')
            self.assertEqual(pathlib.Path(p['base'],'task').read_text(),'base')

    def test_real_three_way_merge_preserves_both_edits(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.state(d)
            original='a\nb\nc\nd\ne\n'
            pathlib.Path(p['base'],'task').write_text(original)
            pathlib.Path(p['main'],'task').write_text('main\nb\nc\nd\ne\n')
            pathlib.Path(p['wt'],'task').write_text('a\nb\nc\nd\nwt\n')
            with contextlib.redirect_stdout(io.StringIO()): ts.cmd_merge(p,True)
            for k in p:
                self.assertEqual(pathlib.Path(p[k],'task').read_text(),'main\nb\nc\nd\nwt\n')

class Integrate(unittest.TestCase):
    """integrate 用真实的本地 origin 裸仓库验证，不触碰真实仓库。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = os.getcwd()
        self.addCleanup(os.chdir, self.old)
        d = pathlib.Path(self.tmp.name)
        self.origin, self.repo = str(d/'origin.git'), str(d/'repo')
        self.sh('git','init','-q','--bare','-b','master',self.origin)
        self.sh('git','clone','-q',self.origin,self.repo)
        for k,v in (('user.name','t'),('user.email','t@t'),('commit.gpgsign','false')):
            self.g('config',k,v)
        self.commit('a.txt','base\n','init')
        self.g('push','-q','origin','master')
        self.g('push','-q','origin','master:beta')
        os.chdir(self.repo)

    def sh(self,*a): return subprocess.run(a,check=True,capture_output=True,text=True).stdout.strip()
    def g(self,*a): return self.sh('git','-C',self.repo,*a)
    def commit(self,name,text,msg):
        pathlib.Path(self.repo,name).write_text(text); self.g('add',name); self.g('commit','-qm',msg)

    def run_integrate(self,yes=False,target=None):
        import argparse
        out = io.StringIO()
        digest = None
        if yes:
            target = target or 'beta'
            with contextlib.redirect_stdout(out):
                wa.cmd_integrate(argparse.Namespace(target=target,yes=False))
            digest = next((line.split(': ',1)[1] for line in out.getvalue().splitlines()
                           if line.startswith('PLAN_SHA256: ')), None)
            out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = wa.cmd_integrate(argparse.Namespace(target=target,yes=yes,expect_plan=digest))
        return rc, out.getvalue().strip().splitlines()[-1]

    def test_preview_then_merge_and_push(self):
        self.g('switch','-qc','feature/x')
        self.commit('b.txt','x\n','feat')
        beta = self.g('rev-parse','origin/beta')
        self.assertEqual(self.run_integrate(), (0,'VERDICT: READY beta'))
        self.g('fetch','-q','origin')
        self.assertEqual(self.g('rev-parse','origin/beta'), beta)
        rc, verdict = self.run_integrate(yes=True)
        self.assertEqual((rc, verdict.split()[:3]), (0, ['VERDICT:','MERGED','beta']))
        self.assertEqual(self.sh('git','-C',self.origin,'log','-1','--format=%s','beta'),
                         "Merge remote-tracking branch 'origin/feature/x' into beta")
        self.assertEqual(self.sh('git','-C',self.origin,'rev-parse','feature/x'), self.g('rev-parse','HEAD'))
        self.assertEqual(self.g('branch','--show-current'), 'feature/x')
        self.assertEqual(len(self.g('worktree','list').splitlines()), 1)
        self.assertEqual(self.run_integrate(), (0,'VERDICT: UP_TO_DATE beta'))

    def test_conflict_leaves_beta_untouched(self):
        self.g('switch','-qc','tmp','origin/beta')
        self.commit('a.txt','beta\n','beta change')
        self.g('push','-q','origin','HEAD:beta')
        self.g('switch','-qc','fix/y','master')
        self.commit('a.txt','fix\n','fix')
        beta = self.sh('git','-C',self.origin,'rev-parse','beta')
        rc, verdict = self.run_integrate(yes=True)
        self.assertEqual((rc, verdict), (1,'VERDICT: CONFLICT beta'))
        self.assertEqual(self.sh('git','-C',self.origin,'rev-parse','beta'), beta)

    def test_dirty_or_base_branch_blocked(self):
        self.assertIn('BLOCKED', self.run_integrate()[1])
        self.g('switch','-qc','feature/z')
        pathlib.Path(self.repo,'a.txt').write_text('dirty\n')
        self.assertIn('BLOCKED', self.run_integrate()[1])

    def test_multiple_lines_require_explicit_target_even_after_base_advances(self):
        self.g('switch','-qc','js-master')
        self.commit('js.txt','js','js base')
        self.g('push','-q','origin','js-master','js-master:js-beta')
        self.g('switch','-qc','feature/js-task','js-master')
        self.commit('task.txt','task','task')
        self.g('switch','js-master')
        for i in range(3): self.commit('advance.txt',str(i),'advance')
        self.g('push','-q','origin','js-master')
        self.g('switch','feature/js-task')
        self.assertIn('TARGET_ASK',self.run_integrate()[1])
        self.assertEqual(self.run_integrate(target='js-beta'),(0,'VERDICT: READY js-beta'))

    def test_already_integrated_still_publishes_task_branch(self):
        self.g('switch','-qc','feature/published')
        self.commit('task.txt','task','task')
        self.g('push','-q','origin','HEAD:beta')
        self.assertEqual(self.run_integrate(yes=True),(0,'VERDICT: UP_TO_DATE beta'))
        remote=subprocess.run(['git','-C',self.origin,'show-ref','--verify','refs/heads/feature/published'],capture_output=True,text=True)
        self.assertEqual(remote.returncode,0,remote.stderr)
        self.assertEqual(remote.stdout.split()[0],self.g('rev-parse','HEAD'))

    def test_execution_without_preview_is_blocked(self):
        import argparse
        self.g('switch','-qc','feature/no-plan')
        self.commit('task.txt','task','task')
        before=self.g('rev-parse','origin/beta')
        with contextlib.redirect_stdout(io.StringIO()):
            rc=wa.cmd_integrate(argparse.Namespace(target='beta',yes=True,expect_plan=None))
        self.assertEqual(rc,1)
        self.assertEqual(self.sh('git','-C',self.origin,'rev-parse','beta'),before)


    def test_preview_drift_blocks_before_publishing(self):
        import argparse
        self.g('switch','-qc','feature/drift')
        self.commit('task.txt','task','task')
        for change in ('head','target'):
            with self.subTest(change=change):
                output=io.StringIO()
                with contextlib.redirect_stdout(output):
                    wa.cmd_integrate(argparse.Namespace(target='beta',yes=False))
                digest=next(line.split(': ',1)[1] for line in output.getvalue().splitlines() if line.startswith('PLAN_SHA256: '))
                if change=='head':
                    self.commit('task.txt','changed','changed task')
                else:
                    self.g('switch','-qc','advance-beta','origin/beta')
                    self.commit('other.txt','other','beta advanced')
                    self.g('push','-q','origin','HEAD:beta')
                    self.g('switch','feature/drift')
                with contextlib.redirect_stdout(io.StringIO()):
                    rc=wa.cmd_integrate(argparse.Namespace(target='beta',yes=True,expect_plan=digest))
                self.assertEqual(rc,1)
                remote=subprocess.run(['git','-C',self.origin,'show-ref','--verify','refs/heads/feature/drift'],capture_output=True)
                self.assertNotEqual(remote.returncode,0)


class LinkAgentConfig(unittest.TestCase):
    """worktree 缺少被忽略的 .claude/hooks 等配置时，项目 hook 报 can't open file。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        self.main = os.path.realpath(os.path.join(self.tmp.name, 'repo'))
        os.makedirs(os.path.join(self.main, '.claude', 'hooks'))
        os.makedirs(os.path.join(self.main, '.codex'))
        self.g('init', '-q', '-b', 'main')
        for k, v in (('user.name', 't'), ('user.email', 't@t'), ('commit.gpgsign', 'false')):
            self.g('config', k, v)
        files = {'.gitignore': '.claude/\n.codex\nAGENTS.md\n', 'CLAUDE.md': 'tracked\n',
                 '.claude/hooks/inject.py': 'print(1)\n', '.claude/settings.json': '{}\n',
                 '.codex/config.toml': 'x\n', 'AGENTS.md': 'agents\n', 'src.txt': 's\n'}
        for name, text in files.items():
            pathlib.Path(self.main, name).write_text(text)
        self.g('add', '.gitignore', 'CLAUDE.md', 'src.txt')
        self.g('commit', '-qm', 'init')
        os.chdir(self.main)

    def g(self, *a):
        return subprocess.run(['git', '-C', self.main, *a], check=True, capture_output=True, text=True).stdout

    def test_create_links_ignored_config_and_removes_cleanly(self):
        import argparse
        with contextlib.redirect_stdout(io.StringIO()) as out:
            wa.cmd_create(argparse.Namespace(name='feature-a', branch='feature/a', base='main'))
        wt = os.path.join(self.main, '.claude', 'worktrees', 'feature-a')
        self.assertIn('LINKED: .codex, AGENTS.md, .claude/hooks, .claude/settings.json', out.getvalue())
        self.assertEqual(os.path.realpath(os.path.join(wt, '.claude', 'hooks')), os.path.join(self.main, '.claude', 'hooks'))
        self.assertTrue(os.path.isfile(os.path.join(wt, '.claude', 'hooks', 'inject.py')))
        self.assertFalse(os.path.lexists(os.path.join(wt, '.claude', 'worktrees')))
        self.assertFalse(os.path.islink(os.path.join(wt, 'CLAUDE.md')))  # 已跟踪的文件来自检出，不链接
        self.assertEqual(subprocess.run(['git', '-C', wt, 'status', '--porcelain'], capture_output=True, text=True).stdout, '')
        os.chdir(wt)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            wa.cmd_link(None)
        self.assertTrue(out.getvalue().strip().endswith('VERDICT: UP_TO_DATE'))
        os.chdir(self.main)
        self.g('worktree', 'remove', wt)  # 不需要 --force
        self.assertTrue(os.path.isfile(os.path.join(self.main, '.claude', 'hooks', 'inject.py')))
        self.assertTrue(os.path.isfile(os.path.join(self.main, 'AGENTS.md')))

    def test_existing_entries_untouched_and_session_hint_links(self):
        wt = os.path.join(self.tmp.name, 'wt')
        self.g('worktree', 'add', '-q', '-b', 'feature/b', wt)
        os.makedirs(os.path.join(wt, '.claude'))
        pathlib.Path(wt, '.claude', 'settings.json').write_text('{"own": 1}\n')
        hint = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'session_start_hint.py'
        res = subprocess.run([sys.executable, str(hint)], input='{"cwd": "%s"}' % wt, capture_output=True, text=True)
        ctx = __import__('json').loads(res.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('已把主仓库被忽略的 Agent 配置软链进本 worktree：.codex, AGENTS.md, .claude/hooks', ctx)
        self.assertNotIn('$S', ctx)
        self.assertEqual(pathlib.Path(wt, '.claude', 'settings.json').read_text(), '{"own": 1}\n')
        self.assertTrue(os.path.islink(os.path.join(wt, '.claude', 'hooks')))

class CheckPolicy(unittest.TestCase):
    """新需求默认进 worktree；只有 --continue（非基线分支）和 --in-place 才允许在主检出原地改。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        self.repo = os.path.realpath(os.path.join(self.tmp.name, 'repo'))
        os.makedirs(self.repo)
        self.g('init', '-q', '-b', 'main')
        for k, v in (('user.name', 't'), ('user.email', 't@t'), ('commit.gpgsign', 'false')):
            self.g('config', k, v)
        pathlib.Path(self.repo, 'a.txt').write_text('a\n')
        pathlib.Path(self.repo, 'b.txt').write_text('b\n')
        self.g('add', '.')
        self.g('commit', '-qm', 'init')
        os.chdir(self.repo)

    def g(self, *a):
        return subprocess.run(['git', '-C', self.repo, *a], check=True, capture_output=True, text=True).stdout

    def check(self, branch=None, cont=False, in_place=False, paths=()):
        import argparse
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(wa, 'other_sessions', return_value=[]):
            wa.cmd_check(argparse.Namespace(branch=branch, base=None, continue_task=cont, in_place=in_place,
                                            paths=list(paths)))
        lines = out.getvalue().splitlines()
        return lines[-1], out.getvalue()

    def test_new_task_on_baseline_requires_worktree_even_for_tiny_change(self):
        verdict, text = self.check(branch='feature/x', paths=['a.txt'])
        self.assertEqual(verdict, 'VERDICT: WORKTREE_REQUIRED')
        self.assertIn('REASON: 新需求/新任务默认在 worktree 中开发', text)

    def test_new_task_on_task_branch_still_requires_worktree(self):
        self.g('switch', '-qc', 'feature/old')
        verdict, text = self.check(branch='feature/new')
        self.assertEqual(verdict, 'VERDICT: WORKTREE_REQUIRED')
        self.assertIn('REASON:', text)
        verdict, _ = self.check()
        self.assertEqual(verdict, 'VERDICT: WORKTREE_REQUIRED')

    def test_continue_on_baseline_branch_is_refused(self):
        for name in ('main', 'beta', 'master'):
            self.g('switch', '-qC', name)
            verdict, text = self.check(cont=True, paths=['a.txt'])
            self.assertEqual(verdict, 'VERDICT: WORKTREE_REQUIRED', name)
            self.assertIn('基线/集成分支', text)

    def test_continue_on_task_branch_stays_and_own_uncommitted_changes_are_not_conflicts(self):
        self.g('switch', '-qc', 'feature/old')
        verdict, _ = self.check(cont=True, paths=['a.txt'])
        self.assertEqual(verdict, 'VERDICT: STAY')
        pathlib.Path(self.repo, 'a.txt').write_text('changed\n')
        verdict, _ = self.check(cont=True, paths=['a.txt'])
        self.assertEqual(verdict, 'VERDICT: STAY')
        verdict, _ = self.check(cont=True)
        self.assertEqual(verdict, 'VERDICT: STAY')

    def test_branch_equal_to_current_branch_is_not_auto_continue(self):
        # 是新需求还是续改由 agent 判断，不能仅凭 --branch 恰好等于当前分支就放行
        self.g('switch', '-qc', 'feature/old')
        verdict, text = self.check(branch='feature/old', paths=['a.txt'])
        self.assertEqual(verdict, 'VERDICT: WORKTREE_REQUIRED')
        self.assertIn('REASON: 新需求/新任务默认在 worktree 中开发', text)
        verdict, _ = self.check(branch='feature/old', cont=True, paths=['a.txt'])
        self.assertEqual(verdict, 'VERDICT: STAY')

    def test_continue_with_other_session_still_checks_overlap(self):
        import argparse
        self.g('switch', '-qc', 'feature/old')
        pathlib.Path(self.repo, 'a.txt').write_text('changed\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(wa, 'other_sessions', return_value=[123]):
            wa.cmd_check(argparse.Namespace(branch=None, base=None, continue_task=True, in_place=False, paths=['a.txt']))
        self.assertTrue(out.getvalue().strip().endswith('VERDICT: WORKTREE_REQUIRED'))
        self.assertIn('CONFLICT:', out.getvalue())

    def test_continue_with_different_branch_is_an_error(self):
        self.g('switch', '-qc', 'feature/old')
        with self.assertRaises(SystemExit):
            self.check(branch='feature/other', cont=True)

    def test_in_place_allowed_but_overlap_still_checked(self):
        verdict, text = self.check(in_place=True, paths=['a.txt'])
        self.assertEqual(verdict, 'VERDICT: STAY')
        self.assertIn('NOTE: 用户明确要求原地修改', text)
        self.assertIn('基线/集成分支', text)
        pathlib.Path(self.repo, 'a.txt').write_text('changed\n')
        verdict, text = self.check(in_place=True, paths=['a.txt'])
        self.assertEqual(verdict, 'VERDICT: WORKTREE_REQUIRED')
        self.assertIn('CONFLICT:', text)
        verdict, _ = self.check(in_place=True, paths=['b.txt'])
        self.assertEqual(verdict, 'VERDICT: STAY')

    def test_session_hint_reminds_on_main_checkout_and_stays_silent_outside_git(self):
        hint = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'session_start_hint.py'
        res = subprocess.run([sys.executable, str(hint)], input='{"cwd": "%s"}' % self.repo, capture_output=True, text=True)
        ctx = __import__('json').loads(res.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('主仓库检出，分支 main', ctx)
        self.assertIn('不要直接在当前分支上修改', ctx)
        self.assertIn('续改', ctx)
        self.assertIn('check --continue', ctx)
        self.assertIn('check --in-place', ctx)
        self.assertNotIn('$S', ctx)
        res = subprocess.run([sys.executable, str(hint)], input='{"cwd": "%s"}' % self.tmp.name, capture_output=True, text=True)
        self.assertEqual(res.stdout.strip(), '')

if __name__ == '__main__': unittest.main()
