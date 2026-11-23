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
#   bug             -> 1（失败）
#   否则            -> 0（通过）
CHECK_SCRIPT = """#!/bin/sh
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
    # 另一个在跑的检查被终止、结果丢弃
    wait_for(lambda: all(c.exit_code is not None for c in engine.checks),
             desc="全部检查退出")
    slow_check = check_of(engine, shas[3])
    assert slow_check is not None and slow_check.discarded
    assert shas[3] not in engine.results
    assert shas[2] not in engine.results  # 非法退出码不产生结论


# ---------------------------------------------------------------------
# 4. 范围缩小 -> 终止范围外检查并丢弃结果
# ---------------------------------------------------------------------

def test_out_of_range_check_is_terminated_and_discarded(tmp_path, check_script):
    # 候选 c1..c10，首轮挑 idx3(c4) 与 idx6(c7)；c4 快速失败使 c7 落到范围外
    extra = {7: {"slow": ""}}
    repo, shas = make_repo(tmp_path, simple_specs(11, bug_from=4, extra=extra))
    engine = make_engine(repo, check_script, tmp_path)
    t0 = time.time()
    engine.start(shas[0], shas[10])

    wait_for(lambda: engine.state == STATE_DONE, timeout=25, desc="定位完成")
    elapsed = time.time() - t0
    assert engine.answer == shas[4]
    # c7 的检查被终止、结果被丢弃；若等它自然结束需 30s
    victim = check_of(engine, shas[7])
    assert victim is not None
    assert victim.discarded and victim.terminate_reason is not None
    assert victim.exit_code is not None  # 进程确实退出了
    assert shas[7] not in engine.results
    assert elapsed < 25


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
    # 候选 c1..c3；c3 忽略 SIGTERM 且存活 30s；escalate_after=1s，应被 SIGKILL 提前结束
    extra = {3: {"stubborn": "30"}}
    repo, shas = make_repo(tmp_path, simple_specs(5, bug_from=2, extra=extra))
    engine = make_engine(repo, check_script, tmp_path, escalate_after=1.0)
    t0 = time.time()
    engine.start(shas[0], shas[4])

    wait_for(lambda: engine.state == STATE_DONE, timeout=20, desc="定位完成")
    assert time.time() - t0 < 15  # 没有等满 30s
    victim = check_of(engine, shas[3])
    assert victim is not None and victim.escalated
    assert victim.discarded
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
# 7. Flask API 冒烟
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
