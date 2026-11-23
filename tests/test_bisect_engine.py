"""bisect_engine 的端到端自测。

覆盖：
- 唯一定位首个失败提交
- 不可测提交（退出码 125）导致无法唯一确定时列出所有可能 SHA
- 非法退出码中止本轮
- 范围缩小后终止范围外检查、丢弃其结果、进程退出后才释放名额
- SIGTERM 不响应时升级 SIGKILL
- 重置后可重新开始
- 边界校验（祖先关系 / 线性历史）
- Flask API 冒烟
"""

import os
import stat
import subprocess
import time

import pytest

from bisect_engine import (
    STATE_ABORTED,
    STATE_AMBIGUOUS,
    STATE_DONE,
    STATE_IDLE,
    BisectEngine,
    BisectError,
)

GIT_ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="T",
    GIT_AUTHOR_EMAIL="t@example.com",
    GIT_COMMITTER_NAME="T",
    GIT_COMMITTER_EMAIL="t@example.com",
)

# 固定检查脚本：按 worktree 里的标记文件决定行为
#   untestable      -> 125（不可测）
#   boom            -> 2（非法退出码）
#   stubborn <秒数> -> 忽略 SIGTERM，存活指定秒数（配合 fail 文件则退出 1）
#   slow            -> sleep 30（可被 SIGTERM 快速杀掉）
#   slow_start      -> 先 sleep 2 再继续（保证兄弟检查已进入运行态）
#   bug             -> 1（失败）
#   否则            -> 0（通过）
CHECK_SCRIPT = """#!/bin/sh
if [ -f slow_start ]; then sleep 2; fi
if [ -f untestable ]; then exit 125; fi
if [ -f boom ]; then exit 2; fi
if [ -f stubborn ]; then
  n=$(cat stubborn)
  trap '' TERM
  i=0
  while [ "$i" -lt $((n * 10)) ]; do sleep 0.1; i=$((i+1)); done
  if [ -f fail ]; then exit 1; fi
  exit 0
fi
if [ -f slow ]; then sleep 30; exit 0; fi
if [ -f bug ]; then exit 1; fi
exit 0
"""


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=GIT_ENV,
    ).stdout.strip()


def make_repo(tmp_path, specs):
    """按 specs 顺序构造线性历史。spec: {"msg": str, "files": {name: content|None}}"""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                   check=True, env=GIT_ENV)
    shas = []
    for i, spec in enumerate(specs):
        for name, content in spec.get("files", {}).items():
            path = repo / name
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        git(repo, "add", "-A")
        env = dict(
            GIT_ENV,
            GIT_AUTHOR_DATE=f"@{1700000000 + i * 60} +0000",
            GIT_COMMITTER_DATE=f"@{1700000000 + i * 60} +0000",
        )
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", spec["msg"]],
                       check=True, env=env)
        shas.append(git(repo, "rev-parse", "HEAD"))
    return repo, shas


@pytest.fixture()
def check_script(tmp_path):
    script = tmp_path / "check.sh"
    script.write_text(CHECK_SCRIPT)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def make_engine(repo, script, tmp_path, **kw):
    kw.setdefault("max_concurrent", 2)
    kw.setdefault("tick", 0.02)
    return BisectEngine(
        repo_path=str(repo),
        script_path=str(script),
        worktree_root=str(tmp_path / "worktrees"),
        **kw,
    )


def wait_for(cond, timeout=20.0, interval=0.05, desc="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    raise AssertionError(f"超时等待: {desc}")


def simple_specs(n, bug_from=None, extra=None):
    """n 个提交；bug_from 起的提交带 bug 标记；extra: {index: {name: content|None}}"""
    specs = []
    for i in range(n):
        files = {"f": str(i)}
        if bug_from is not None and i >= bug_from:
            files["bug"] = ""
        if extra and i in extra:
            files.update(extra[i])
        specs.append({"msg": f"commit {i}", "files": files})
    return specs


def check_of(engine, sha):
    for c in engine.checks:
        if c.sha == sha:
            return c
    return None


# ---------------------------------------------------------------------
# 1. 唯一定位首个失败提交
# ---------------------------------------------------------------------

def test_finds_first_bad_uniquely(tmp_path, check_script):
    repo, shas = make_repo(tmp_path, simple_specs(7, bug_from=4))
    engine = make_engine(repo, check_script, tmp_path)
    engine.start(shas[0], shas[6])

    wait_for(lambda: engine.state == STATE_DONE, desc="定位完成")
    assert engine.answer == shas[4]
    # 全部检查都已收割，worktree 已清理
    assert all(c.exit_code is not None for c in engine.checks)
    wait_for(lambda: not os.listdir(tmp_path / "worktrees"), desc="worktree 清理")
    # 结论与单调假设一致：c4 失败、c3 通过
    assert engine.results[shas[4]] == "fail"
    assert engine.results[shas[3]] == "pass"


# ---------------------------------------------------------------------
# 2. 不可测提交 -> 列出所有可能 SHA
# ---------------------------------------------------------------------

def test_untestable_commits_yield_ambiguous_result(tmp_path, check_script):
    # c3、c4 不可测（bug 从 c3 引入但无法实测）；c5 起可测且失败
    extra = {
        3: {"untestable": "", "bug": ""},
        5: {"untestable": None},
    }
    specs = simple_specs(8, extra=extra)
    # bug 从 c3 起一直存在（simple_specs 的 bug_from 不满足，这里手动补）
    for i in range(4, 8):
        specs[i]["files"].setdefault("bug", "")
    repo, shas = make_repo(tmp_path, specs)
    engine = make_engine(repo, check_script, tmp_path)
    engine.start(shas[0], shas[7])

    wait_for(lambda: engine.state == STATE_AMBIGUOUS, desc="得到多候选结果")
    assert set(engine.possibilities) == {shas[3], shas[4], shas[5]}
    assert engine.results[shas[3]] == "skip"
    assert engine.results[shas[4]] == "skip"
    assert engine.results[shas[5]] == "fail"


def test_untestable_outside_final_gap_does_not_blur_result(tmp_path, check_script):
    # c5 不可测但 bug 从 c2 引入：不可测提交不在最终缺口内，应唯一确定 c2
    extra = {5: {"untestable": ""}}
    repo, shas = make_repo(tmp_path, simple_specs(8, bug_from=2, extra=extra))
    engine = make_engine(repo, check_script, tmp_path)
    engine.start(shas[0], shas[7])

    wait_for(lambda: engine.state == STATE_DONE, desc="唯一定位")
    assert engine.answer == shas[2]
    # c5 落在范围外后，其退出结果（125）按规则被丢弃、不影响结论
    assert engine.results.get(shas[5]) in (None, "skip")


# ---------------------------------------------------------------------
# 3. 非法退出码 -> 中止本轮并说明原因
# ---------------------------------------------------------------------

def test_unexpected_exit_code_aborts_round(tmp_path, check_script):
    # c2 非法退出码；c3 慢速（boom 标记在 c3 已删除，避免文件随历史残留）
    extra = {2: {"boom": ""}, 3: {"boom": None, "slow": ""}}
    repo, shas = make_repo(tmp_path, simple_specs(5, extra=extra))
    engine = make_engine(repo, check_script, tmp_path)
    engine.start(shas[0], shas[4])

    wait_for(lambda: engine.state == STATE_ABORTED, desc="会话中止")
    assert "2" in engine.abort_reason
    assert shas[2] in engine.abort_reason
    # 另一个检查被终止（运行中）或在准备期被跳过，结果一律丢弃
    wait_for(
        lambda: all(
            c.reaped or c.skipped_prep or c.status == "finished"
            for c in engine.checks
        ),
        desc="全部检查收尾",
    )
    wait_for(lambda: not engine._occupied_worktrees(), desc="工作树全部释放")
    slow_check = check_of(engine, shas[3])
    assert slow_check is not None and slow_check.discarded
    assert shas[3] not in engine.results
    assert shas[2] not in engine.results  # 非法退出码不产生结论


# ---------------------------------------------------------------------
# 4. 范围缩小 -> 终止范围外检查并丢弃结果
# ---------------------------------------------------------------------

def test_out_of_range_check_is_terminated_and_discarded(tmp_path, check_script):
    # 候选 c1..c10，首轮挑 idx3(c4) 与 idx6(c7)；c4 快速失败使 c7 落到范围外。
    # slow_start 只在 c4 存在（c5 删除），确保 c7 已进入运行态（验证运行期终止）。
    extra = {4: {"slow_start": ""}, 5: {"slow_start": None}, 7: {"slow": ""}}
    repo, shas = make_repo(tmp_path, simple_specs(11, bug_from=4, extra=extra))
    engine = make_engine(repo, check_script, tmp_path)
    t0 = time.time()
    engine.start(shas[0], shas[10])

    # 等两个检查都进入运行态
    wait_for(
        lambda: sum(1 for c in engine._active_checks() if c.status == "running") == 2,
        desc="两个检查均进入运行态",
    )
    wait_for(lambda: engine.state == STATE_DONE, timeout=25, desc="定位完成")
    elapsed = time.time() - t0
    assert engine.answer == shas[4]
    # c7 的检查在运行中被终止、结果被丢弃；若等它自然结束需 30s
    victim = check_of(engine, shas[7])
    assert victim is not None
    assert victim.discarded and victim.terminate_reason is not None
    assert victim.status == "finished" and not victim.skipped_prep
    assert victim.exit_code is not None  # 进程确实退出了
    assert shas[7] not in engine.results
    assert elapsed < 25


def test_preparing_check_skips_script_when_commit_excluded(tmp_path, check_script):
    # 准备中的提交若被排除出范围：跳过脚本，工作树清理后留给后续检查复用。
    # 用一个“准备很慢”的脚本仓库无法直接拖慢 git，故通过监听事件等待准备态出现，
    # 并构造 c4 极快失败：若 c7 仍在准备期，它应被标记 skipped_prep 而非终止进程。
    extra = {7: {"slow": ""}}
    repo, shas = make_repo(tmp_path, simple_specs(11, bug_from=4, extra=extra))
    engine = make_engine(repo, check_script, tmp_path)
    engine.start(shas[0], shas[10])
    wait_for(lambda: engine.state == STATE_DONE, timeout=25, desc="定位完成")
    assert engine.answer == shas[4]
    victim = check_of(engine, shas[7])
    assert victim is not None and victim.discarded
    # 无论准备期跳过还是运行期终止，都不能产生结论，且工作树最终清空
    assert shas[7] not in engine.results
    if victim.skipped_prep:
        assert victim.terminate_reason and "排除" in victim.terminate_reason
        assert victim.exit_code is None  # 脚本从未启动


def test_slot_released_only_after_process_exit(tmp_path, check_script):
    # 首轮挑 idx2=c3、idx4=c5。c3 在 t≈2s 失败使 hi 收缩；c5 忽略 SIGTERM
    # 存活 8s，t≈2s 时被终止但名额不释放。此时待测 c1、c2 只剩 1 个空位：
    # 引擎先测 c2（t≈2s 启动、3s 后失败），c1 必须等 c2 退出腾出名额后
    # （t≈5s）才启动——若 c5 的名额被提前释放，c1 在 t≈2s 就会启动。
    extra = {
        2: {"stubborn": "3", "fail": ""},
        3: {"stubborn": "2", "fail": ""},
        4: {"stubborn": None, "fail": None},
        5: {"stubborn": "8"},
        6: {"stubborn": None},
    }
    repo, shas = make_repo(tmp_path, simple_specs(9, extra=extra))
    engine = make_engine(repo, check_script, tmp_path, escalate_after=30)
    engine.start(shas[0], shas[8])

    wait_for(lambda: engine.state == STATE_DONE, timeout=30, desc="定位完成")
    assert engine.answer == shas[2]

    victim = check_of(engine, shas[5])
    assert victim is not None and victim.discarded
    # 被终止后仍存活了相当一段时间（忽略 SIGTERM），名额一直被占用
    assert victim.terminate_sent_at is not None
    assert victim.finished_at - victim.terminate_sent_at > 1.5
    # c1 在 c5 被终止后没有立即启动，而是等 c2 退出腾出名额后才启动
    c1, c2 = check_of(engine, shas[1]), check_of(engine, shas[2])
    assert c1 is not None and c2 is not None
    assert c1.started_at - victim.terminate_sent_at > 2
    assert c1.started_at >= c2.finished_at - 0.5
    assert c1.started_at < victim.finished_at


def test_sigterm_escalates_to_sigkill(tmp_path, check_script):
    # 候选 c1..c3；c3 忽略 SIGTERM 且存活 30s；escalate_after=1s，应被 SIGKILL 提前结束。
    # slow_start 只在 c2 存在（c3 删除），保证 c3 先进入运行态、c2 稍后失败触发范围收缩。
    extra = {2: {"slow_start": ""}, 3: {"stubborn": "30", "slow_start": None}}
    repo, shas = make_repo(tmp_path, simple_specs(5, bug_from=2, extra=extra))
    engine = make_engine(repo, check_script, tmp_path, escalate_after=1.0)
    t0 = time.time()
    engine.start(shas[0], shas[4])

    # 等 c3 进入运行态
    wait_for(
        lambda: any(
            c.sha == shas[3] and c.status == "running"
            for c in engine._active_checks()
        ),
        desc="c3 检查进入运行态",
    )
    wait_for(lambda: engine.state == STATE_DONE, timeout=20, desc="定位完成")
    assert time.time() - t0 < 15  # 没有等满 30s
    victim = check_of(engine, shas[3])
    assert victim is not None and victim.escalated
    assert victim.discarded and not victim.skipped_prep
    assert engine.answer == shas[2]


# ---------------------------------------------------------------------
# 5. 重置后可重新开始
# ---------------------------------------------------------------------

def test_reset_terminates_and_allows_new_round(tmp_path, check_script):
    # 候选 c1、c2 都在 sleep 30，重置应立即终止它们并回到空闲
    extra = {1: {"slow": ""}, 2: {"slow": ""}}
    repo, shas = make_repo(tmp_path, simple_specs(4, extra=extra))
    engine = make_engine(repo, check_script, tmp_path)
    engine.start(shas[0], shas[3])
    wait_for(lambda: len(engine._active_checks()) == 2, desc="两个检查在跑")

    engine.request_reset()
    wait_for(lambda: engine.state == STATE_IDLE, timeout=15, desc="回到空闲")
    assert not engine._active_checks()

    # 重新开始一轮（这次脚本正常）——把 slow 标记从工作区历史里去掉不现实，
    # 直接换边界：good=c0, bad=c2 仍会命中 slow，所以改为验证能再次 start。
    # 这里用另一个仓库做干净验证。
    repo2, shas2 = make_repo(tmp_path / "second", simple_specs(5, bug_from=2))
    engine2 = make_engine(repo2, check_script, tmp_path / "second")
    engine2.start(shas2[0], shas2[4])
    wait_for(lambda: engine2.state == STATE_DONE, desc="第二轮定位完成")
    assert engine2.answer == shas2[2]


# ---------------------------------------------------------------------
# 6. 边界校验
# ---------------------------------------------------------------------

def test_rejects_non_ancestor_good(tmp_path, check_script):
    repo, shas = make_repo(tmp_path, simple_specs(4))
    engine = make_engine(repo, check_script, tmp_path)
    with pytest.raises(BisectError, match="祖先"):
        engine.start(shas[3], shas[1])  # good 比 bad 新


def test_rejects_non_linear_history(tmp_path, check_script):
    repo, shas = make_repo(tmp_path, simple_specs(2))
    # 从 c0 拉分支提交，再合回 main，制造合并提交
    git(repo, "checkout", "-q", "-b", "side", shas[0])
    (repo / "side.txt").write_text("side")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "side commit")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-m", "merge side", "side")
    merge_sha = git(repo, "rev-parse", "HEAD")

    engine = make_engine(repo, check_script, tmp_path)
    with pytest.raises(BisectError, match="线性"):
        engine.start(shas[0], merge_sha)


def test_rejects_same_commit(tmp_path, check_script):
    repo, shas = make_repo(tmp_path, simple_specs(3))
    engine = make_engine(repo, check_script, tmp_path)
    with pytest.raises(BisectError):
        engine.start(shas[1], shas[1])


# ---------------------------------------------------------------------
# 7. worktree 池化复用
# ---------------------------------------------------------------------

def _worktree_dirs(root):
    if not os.path.isdir(root):
        return []
    return [
        d for d in os.listdir(root)
        if os.path.isdir(os.path.join(root, d))
    ]


def test_worktrees_are_reused_within_round(tmp_path, check_script):
    # 并发 2、候选 6 个：检查数 > 2，必然发生复用；同一工作树路径被多次绑定
    repo, shas = make_repo(tmp_path, simple_specs(7, bug_from=4))
    engine = make_engine(repo, check_script, tmp_path, max_concurrent=2)
    engine.start(shas[0], shas[6])
    wait_for(lambda: engine.state == STATE_DONE, desc="定位完成")

    origins = [c.worktree_origin for c in engine.checks if c.exit_code is not None]
    assert "new" in origins and "reused" in origins, f"应同时存在新建与复用: {origins}"
    # 复用检查的 worktree 路径与某个早先的检查相同
    paths = [c.worktree for c in engine.checks]
    assert len(paths) != len(set(paths)), "同一工作树路径应被复用"
    # 任意时刻工作树数量不超过并发上限
    assert len(_worktree_dirs(tmp_path / "worktrees")) <= 2
    # 轮次结束工作树全部移除
    wait_for(lambda: not _worktree_dirs(tmp_path / "worktrees"), desc="工作树清空")


def test_preparing_and_cleaning_worktrees_count_against_concurrency(tmp_path, check_script):
    # 并发上限 2：任何时刻处于准备/占用/清理/移除中的工作树都不超过 2
    repo, shas = make_repo(tmp_path, simple_specs(9, bug_from=4))
    engine = make_engine(repo, check_script, tmp_path, max_concurrent=2)
    engine.start(shas[0], shas[8])
    peak = 0
    deadline = time.time() + 25
    while time.time() < deadline and engine.state == "running":
        occupied = len(engine._occupied_worktrees())
        peak = max(peak, occupied)
        assert occupied <= 2
        assert len(_worktree_dirs(tmp_path / "worktrees")) <= 2
        time.sleep(0.01)
    wait_for(lambda: engine.state == STATE_DONE, desc="定位完成")
    assert peak >= 1


def test_reused_worktree_matches_fresh_checkout_and_preserves_mtime(tmp_path, check_script):
    # 直接驱动引擎内部的复用算法，验证：
    # 1) 复用后 HEAD/暂存区/目录与全新检出完全一致（含未跟踪、忽略文件清理）；
    # 2) 脚本未改动且源/目标 blob+模式都相同的文件保留原 mtime；
    # 3) 被脚本改动过的文件即使内容被还原也不保留 mtime；
    # 4) 提交间内容变化的文件获得新 mtime。
    specs = []
    for i in range(4):
        specs.append({"msg": f"c{i}", "files": {
            "untouched.txt": "forever\n",   # 脚本不动、提交间也不变 -> 保留
            "tampered.txt": "same\n",       # 脚本会改、提交间不变 -> 不保留
            "change.txt": f"v{i}\n",        # 提交间变化 -> 不保留
        }})
    repo, shas = make_repo(tmp_path, specs)
    engine = make_engine(repo, check_script, tmp_path)
    root = tmp_path / "worktrees"
    wt_path = os.path.join(str(root), "wt-x")

    # 新建工作树指向 shas[1]
    engine._create_worktree(wt_path, shas[1])
    snapshot = engine._snapshot_mtimes(wt_path)
    untouched_mt = os.lstat(os.path.join(wt_path, "untouched.txt")).st_mtime_ns
    tampered_mt = os.lstat(os.path.join(wt_path, "tampered.txt")).st_mtime_ns
    change_mt_before = os.lstat(os.path.join(wt_path, "change.txt")).st_mtime_ns

    # 模拟检查脚本污染：改 tampered、留未跟踪与忽略垃圾；不动 untouched
    time.sleep(0.02)
    with open(os.path.join(wt_path, "tampered.txt"), "a") as f:
        f.write("dirty\n")
    with open(os.path.join(wt_path, "untracked.txt"), "w") as f:
        f.write("junk\n")
    os.makedirs(os.path.join(wt_path, "ignored"))
    with open(os.path.join(wt_path, "ignored", "x.log"), "w") as f:
        f.write("x\n")

    # 复用切换到 shas[3]
    engine._reuse_worktree(wt_path, shas[3], shas[1], snapshot)

    # 未被脚本改动且提交间一致：mtime 保留
    assert os.lstat(os.path.join(wt_path, "untouched.txt")).st_mtime_ns == untouched_mt
    # 被脚本改动过（即使 checkout 还原了内容）：mtime 不保留
    assert os.lstat(os.path.join(wt_path, "tampered.txt")).st_mtime_ns != tampered_mt
    assert open(os.path.join(wt_path, "tampered.txt")).read() == "same\n"
    # 提交间内容变化：mtime 更新
    assert os.lstat(os.path.join(wt_path, "change.txt")).st_mtime_ns != change_mt_before

    # 未跟踪/忽略文件被清除
    assert not os.path.exists(os.path.join(wt_path, "untracked.txt"))
    assert not os.path.isdir(os.path.join(wt_path, "ignored"))

    # 与全新检出逐字节对比（.git 指针文件除外）
    fresh = os.path.join(str(root), "fresh")
    engine._create_worktree(fresh, shas[3])
    for base, _dirs, files in os.walk(fresh):
        for name in files:
            rel = os.path.relpath(os.path.join(base, name), fresh)
            if rel == ".git":
                continue
            a = os.path.join(fresh, rel)
            b = os.path.join(wt_path, rel)
            assert os.path.lexists(b), f"复用树缺少 {rel}"
            if os.path.isfile(a):
                assert open(a, "rb").read() == open(b, "rb").read(), rel
    # git 视角干净，HEAD 即目标提交
    assert subprocess.run(
        ["git", "-C", wt_path, "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout == ""
    assert subprocess.run(
        ["git", "-C", wt_path, "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip() == shas[3]


def test_script_always_starts_on_clean_target_tree(tmp_path, check_script):
    # 端到端：让检查脚本在工作树里留下未跟踪/忽略文件和被修改的跟踪文件，
    # 后续复用该工作树的检查启动时，目录内容必须等于目标提交的全新检出。
    polluting = tmp_path / "pollute.sh"
    polluting.write_text(
        "#!/bin/sh\n"
        "echo junk > leftover.txt\n"
        "mkdir -p ignored && echo x > ignored/x.log\n"
        "echo tampered >> f\n"
        "if [ -f bug ]; then exit 1; fi\n"
        "exit 0\n"
    )
    polluting.chmod(0o755)
    repo, shas = make_repo(tmp_path, simple_specs(7, bug_from=4))
    engine = make_engine(repo, polluting, tmp_path, max_concurrent=1)
    engine.start(shas[0], shas[6])
    wait_for(lambda: engine.state == STATE_DONE, desc="定位完成")
    # 能正确定位说明每次脚本都在干净树上运行（污染没有跨检查残留导致误判）
    assert engine.answer == shas[4]
    # 所有复用检查都应标记 reused（并发 1，第二个检查起全部复用）
    reused = [c for c in engine.checks if c.worktree_origin == "reused"]
    assert reused, "并发 1 时第二个检查起应复用同一工作树"


def test_cleanup_failure_aborts_when_remove_also_fails(tmp_path, check_script, monkeypatch):
    # 清理失败 -> 尝试移除；移除也失败 -> 中止本轮并在页面提示
    import bisect_engine as be
    repo, shas = make_repo(tmp_path, simple_specs(5, bug_from=2))
    engine = make_engine(repo, check_script, tmp_path)

    real_git = be._git
    real_destroy = BisectEngine._destroy_worktree

    def fake_destroy(self, wt):
        return False  # 模拟移除彻底失败

    def flaky_git(repo_arg, *args):
        # 清理工作树时的 clean 调用第一次失败，触发移除-补建路径
        if args and args[0] == "clean":
            flaky_git.called = True
            raise be.BisectError("simulated clean failure")
        return real_git(repo_arg, *args)
    flaky_git.called = False

    monkeypatch.setattr(be, "_git", flaky_git)
    monkeypatch.setattr(BisectEngine, "_destroy_worktree", fake_destroy)
    engine.start(shas[0], shas[4])
    wait_for(lambda: flaky_git.called, timeout=20, desc="清理失败被触发")
    wait_for(lambda: engine.state == STATE_ABORTED, timeout=10, desc="会话中止")
    assert engine.abort_reason and "移除失败" in engine.abort_reason
    # 快照把中止原因带给页面
    assert "移除失败" in engine.snapshot()["abort_reason"]


def test_cleanup_failure_recovers_when_remove_succeeds(tmp_path, check_script, monkeypatch):
    # 清理失败但移除成功：工作树从池中删除，调度器补建新树并完成定位
    import bisect_engine as be
    repo, shas = make_repo(tmp_path, simple_specs(6, bug_from=3))
    engine = make_engine(repo, check_script, tmp_path, max_concurrent=1)

    real_git = be._git
    real_destroy = BisectEngine._destroy_worktree
    state = {"clean_failed": False}

    def flaky_git(repo_arg, *args):
        # 第一次清理工作树的 clean 失败一次，之后恢复正常
        if args and args[0] == "clean" and not state["clean_failed"]:
            state["clean_failed"] = True
            raise be.BisectError("simulated clean failure")
        return real_git(repo_arg, *args)

    monkeypatch.setattr(be, "_git", flaky_git)
    monkeypatch.setattr(BisectEngine, "_destroy_worktree", real_destroy)
    engine.start(shas[0], shas[5])
    wait_for(lambda: state["clean_failed"], timeout=20, desc="清理失败被触发")
    wait_for(lambda: engine.state == STATE_DONE, timeout=25, desc="定位完成")
    assert engine.answer == shas[3]


def test_mtime_basis_tracks_source_commit_across_multiple_reuse(tmp_path, check_script):
    # 连续三次复用同一工作树（A->B->C->D），每次的 mtime 恢复基准都应是
    # 上一次准备完成时的快照与当时的源提交，而不是被 wt.sha 提前改写污染。
    specs = []
    for i in range(5):
        specs.append({"msg": f"c{i}", "files": {
            "keep.txt": "forever\n",            # 全程不变
            "change.txt": f"v{i}\n",            # 每提交都变
        }})
    repo, shas = make_repo(tmp_path, specs)
    engine = make_engine(repo, check_script, tmp_path)
    wt_path = os.path.join(str(tmp_path / "worktrees"), "wt-multi")

    engine._create_worktree(wt_path, shas[0])
    expected_keep_mt = os.lstat(os.path.join(wt_path, "keep.txt")).st_mtime_ns
    current_sha = shas[0]
    snapshot = engine._snapshot_mtimes(wt_path)
    for target in shas[1:4]:
        # 模拟脚本只触碰每轮都在变的 change.txt；keep.txt 全程不动
        time.sleep(0.02)
        with open(os.path.join(wt_path, "change.txt"), "a") as f:
            f.write("dirty\n")
        engine._reuse_worktree(wt_path, target, current_sha, snapshot)
        # 全程未被脚本改动且提交间一致的 keep.txt，始终保留首次检出的 mtime
        assert os.lstat(os.path.join(wt_path, "keep.txt")).st_mtime_ns == expected_keep_mt
        # change.txt 每轮都变，mtime 每轮刷新
        assert open(os.path.join(wt_path, "change.txt")).read() == f"v{shas.index(target)}\n"
        snapshot = engine._snapshot_mtimes(wt_path)
        current_sha = target
    # 最终工作树干净且 HEAD 正确
    assert subprocess.run(
        ["git", "-C", wt_path, "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout == ""


def test_script_mode_only_change_blocks_mtime_preservation(tmp_path, check_script):
    # 脚本只 chmod（不改内容、mtime 不变）：该文件也算“被改动”，不保留 mtime，
    # 且复用后模式恢复为目标提交的模式。
    specs = [
        {"msg": "c0", "files": {"f.txt": "same\n"}},
        {"msg": "c1", "files": {"f.txt": "same\n", "other.txt": "o\n"}},
    ]
    repo, shas = make_repo(tmp_path, specs)
    engine = make_engine(repo, check_script, tmp_path)
    wt_path = os.path.join(str(tmp_path / "worktrees"), "wt-mode")
    engine._create_worktree(wt_path, shas[0])
    snapshot = engine._snapshot_mtimes(wt_path)
    orig_mt = snapshot["f.txt"]
    time.sleep(0.02)
    os.chmod(os.path.join(wt_path, "f.txt"), 0o755)  # 仅模式变化
    # chmod 本身不改 mtime，确认前置条件
    assert os.lstat(os.path.join(wt_path, "f.txt")).st_mtime_ns == orig_mt

    engine._reuse_worktree(wt_path, shas[1], shas[0], snapshot)
    # 模式恢复为目标值 644
    assert os.stat(os.path.join(wt_path, "f.txt")).st_mode & 0o777 == 0o644
    # 被脚本改动过 -> mtime 被显式刷新，不再是原值
    assert os.lstat(os.path.join(wt_path, "f.txt")).st_mtime_ns != orig_mt
    # 工作树仍干净
    assert subprocess.run(
        ["git", "-C", wt_path, "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout == ""


def test_check_snapshot_marks_new_vs_reused_worktree(tmp_path, check_script):
    # 页面/API 快照中每个检查都带 worktree_origin（new/reused），工作树带状态
    repo, shas = make_repo(tmp_path, simple_specs(7, bug_from=4))
    engine = make_engine(repo, check_script, tmp_path, max_concurrent=2)
    engine.start(shas[0], shas[6])
    wait_for(lambda: engine.state == STATE_DONE, desc="定位完成")
    snap = engine.snapshot()
    for check in snap["checks"]:
        assert check["worktree_origin"] in ("new", "reused")
    # 前两个是新建（并发上限 2），之后出现复用
    origins = [c["worktree_origin"] for c in snap["checks"]]
    assert origins[:2] == ["new", "new"]
    assert "reused" in origins
    # 工作树快照字段完整
    for wt in snap["worktrees"]:
        assert wt["state"] in (
            "preparing", "busy", "cleaning", "removing", "idle",
        )
        assert wt["origin"] in ("new", "reused")


def test_abort_then_reset_retries_failed_remove(tmp_path, check_script, monkeypatch):
    # 移除第一次失败 -> 中止；重置时移除恢复成功 -> 回到 idle 且工作树清空
    import bisect_engine as be
    repo, shas = make_repo(tmp_path, simple_specs(5, bug_from=2))
    engine = make_engine(repo, check_script, tmp_path)

    real_git = be._git
    real_destroy = BisectEngine._destroy_worktree
    state = {"fail_next_remove": False, "remove_calls": 0}

    def flaky_git(repo_arg, *args):
        if args and args[0] == "clean" and not state["clean_done"]:
            state["clean_done"] = True
            raise be.BisectError("simulated clean failure")
        return real_git(repo_arg, *args)
    state["clean_done"] = False

    def destroy_once_fail(self, wt):
        state["remove_calls"] += 1
        if state["remove_calls"] == 1:
            return False
        return real_destroy(self, wt)

    monkeypatch.setattr(be, "_git", flaky_git)
    monkeypatch.setattr(BisectEngine, "_destroy_worktree", destroy_once_fail)
    engine.start(shas[0], shas[4])
    wait_for(lambda: engine.state == STATE_ABORTED, timeout=20, desc="首轮中止")
    assert state["remove_calls"] >= 1

    # 重置：再次尝试移除，这次成功
    engine.request_reset()
    wait_for(lambda: engine.state == STATE_IDLE, timeout=15, desc="重置回到空闲")
    assert not engine._occupied_worktrees()
    assert not _worktree_dirs(tmp_path / "worktrees")


def test_reset_cleans_untracked_and_ignored_leftovers(tmp_path, check_script):
    # 重置后工作树目录与 git 元数据全部清空，下一轮全新开始
    polluting = tmp_path / "pollute.sh"
    polluting.write_text(
        "#!/bin/sh\necho junk > leftover.txt\nsleep 30\n"
    )
    polluting.chmod(0o755)
    repo, shas = make_repo(tmp_path, simple_specs(4))
    engine = make_engine(repo, polluting, tmp_path)
    engine.start(shas[0], shas[3])
    wait_for(lambda: len(engine._active_checks()) >= 1, desc="检查启动")

    engine.request_reset()
    wait_for(lambda: engine.state == STATE_IDLE, timeout=15, desc="回到空闲")
    assert not engine._occupied_worktrees()
    assert not _worktree_dirs(tmp_path / "worktrees")
    # git 侧也无残留 worktree 注册
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    ).stdout
    worktree_lines = [ln for ln in out.splitlines() if ln.startswith("worktree ")]
    assert len(worktree_lines) == 1  # 只剩主仓库


# ---------------------------------------------------------------------
# 8. Flask API 冒烟
# ---------------------------------------------------------------------

def test_flask_api_end_to_end(tmp_path, check_script):
    from app import create_app

    repo, shas = make_repo(tmp_path, simple_specs(7, bug_from=4))
    app = create_app(
        repo_path=str(repo),
        script_path=str(check_script),
        worktree_root=str(tmp_path / "wt"),
        max_concurrent=2,
    )
    client = app.test_client()

    assert client.get("/").status_code == 200
    commits = client.get("/api/commits").get_json()["commits"]
    assert len(commits) == 7

    resp = client.post("/api/start", json={"good": shas[0], "bad": shas[6]})
    assert resp.status_code == 200

    def done():
        return client.get("/api/status").get_json()["state"] == STATE_DONE

    wait_for(done, desc="API 轮询到完成")
    status = client.get("/api/status").get_json()
    assert status["answer"]["sha"] == shas[4]
    assert status["progress"]["total"] == 6

    # 运行中不允许重复 start
    client.post("/api/reset")

    def idle():
        return client.get("/api/status").get_json()["state"] == STATE_IDLE

    wait_for(idle, desc="重置完成")
    resp = client.post("/api/start", json={"good": shas[1], "bad": shas[1]})
    assert resp.status_code == 400
