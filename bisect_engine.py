"""并行 Git 二分定位引擎。

维护者给定一个已知通过的提交（good）和一个已知失败的提交（bad），
假定“失败一旦出现就会延续”（单调假设）。引擎在 (good, bad] 的线性
历史上做二分：

- 有待测提交且有空位时就启动检查，最多同时 max_concurrent 个；
- 每个检查在独立的 `git worktree` 里执行同一个固定脚本；
- 脚本退出码约定：0=通过，1=失败，125=不可测，其他退出码中止本轮；
- 搜索范围缩小后，仍在跑但已落在范围外的检查会被终止（SIGTERM，
  超时后 SIGKILL），其退出结果被丢弃；进程真正退出后才释放名额；
- 全部可测提交测完后：若唯一确定首个失败提交则给出 SHA；若不可测
  提交导致无法唯一确定，则列出所有可能的 SHA。

状态机：idle -> running -> done | ambiguous | aborted -> (reset) -> idle
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

# 检查结论
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_SKIP = "skip"  # 不可测（退出码 125）

# 会话状态
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "done"            # 唯一确定首个失败提交
STATE_AMBIGUOUS = "ambiguous"  # 不可测提交导致无法唯一确定
STATE_ABORTED = "aborted"      # 脚本退出码非法，本轮中止

# 单个检查的状态
CHECK_RUNNING = "running"
CHECK_TERMINATING = "terminating"  # 已发 SIGTERM，等待进程退出
CHECK_FINISHED = "finished"

TERMINAL_STATES = (STATE_DONE, STATE_AMBIGUOUS, STATE_ABORTED)


class BisectError(Exception):
    """可展示给用户的错误（边界非法、git 失败等）。"""


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise BisectError(f"git {' '.join(args)} 失败: {proc.stderr.strip()}")
    return proc.stdout


@dataclass
class Check:
    """一次针对某个提交的检查。"""

    id: int
    sha: str
    worktree: str
    proc: subprocess.Popen | None = None
    status: str = CHECK_RUNNING
    discarded: bool = False          # 结果被丢弃（范围外被终止 / 会话中止）
    terminate_reason: str | None = None
    terminate_sent_at: float | None = None
    escalated: bool = False          # 是否已升级为 SIGKILL
    exit_code: int | None = None
    verdict: str | None = None       # pass / fail / skip
    output_tail: str = ""
    started_at: float = 0.0
    finished_at: float | None = None
    reaped: bool = False


class BisectEngine:
    """单会话二分引擎。所有共享状态都由 self._lock 保护。"""

    def __init__(
        self,
        repo_path: str,
        script_path: str,
        worktree_root: str,
        max_concurrent: int = 2,
        escalate_after: float = 5.0,
        tick: float = 0.1,
    ):
        self.repo_path = os.path.abspath(repo_path)
        self.script_path = os.path.abspath(script_path)
        self.worktree_root = os.path.abspath(worktree_root)
        self.max_concurrent = max_concurrent
        self.escalate_after = escalate_after  # SIGTERM 后多久升级为 SIGKILL
        self.tick = tick                      # 调度循环间隔
        self._lock = threading.RLock()
        self._git_lock = threading.Lock()  # 串行化 git worktree add/remove
        self._thread: threading.Thread | None = None
        self._reset_state()
        self._startup_cleanup()

    # ------------------------------------------------------------------
    # 状态初始化与清理
    # ------------------------------------------------------------------

    def _reset_state(self) -> None:
        self.state = STATE_IDLE
        self.reset_requested = False
        self.good_boundary: str | None = None
        self.bad_boundary: str | None = None
        self.subjects: dict[str, str] = {}
        self.commits: list[str] = []   # 候选提交，旧->新；最后一个即 bad 边界
        self._index: dict[str, int] = {}
        self.results: dict[str, str] = {}  # sha -> verdict（仅实测过的）
        # 搜索范围为下标开区间 (lo, hi]：lo 是已知通过（-1 表示 good 边界），
        # hi 是已知最早失败（初始为 bad 边界）。范围只会单调缩小。
        self.lo = -1
        self.hi = -1
        self.checks: list[Check] = []
        self._next_id = 1
        self.events: list[dict] = []
        self.answer: str | None = None
        self.possibilities: list[str] | None = None
        self.abort_reason: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None

    def _startup_cleanup(self) -> None:
        """进程启动时清掉上次遗留的 worktree（目录与 git 元数据）。"""
        try:
            if os.path.isdir(self.worktree_root):
                shutil.rmtree(self.worktree_root, ignore_errors=True)
            if os.path.isdir(os.path.join(self.repo_path, ".git")):
                with self._git_lock:
                    _git(self.repo_path, "worktree", "prune")
        except Exception:
            pass  # 启动清理失败不致命

    def _log(self, msg: str) -> None:
        self.events.append({"ts": time.time(), "msg": msg})
        if len(self.events) > 300:
            del self.events[: len(self.events) - 300]

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def list_commits(self, limit: int = 500) -> list[dict]:
        """供边界选择：按旧->新列出仓库提交。"""
        if not os.path.isdir(os.path.join(self.repo_path, ".git")):
            raise BisectError(f"仓库不存在: {self.repo_path}")
        out = _git(
            self.repo_path, "log", "--reverse", "--format=%H%x00%s",
            f"--max-count={limit}",
        )
        commits = []
        for line in out.splitlines():
            if not line:
                continue
            sha, _, subject = line.partition("\x00")
            commits.append({"sha": sha, "short": sha[:10], "subject": subject})
        return commits

    def start(self, good: str, bad: str) -> None:
        """校验边界并启动新一轮二分。"""
        with self._lock:
            if self.state != STATE_IDLE:
                raise BisectError(f"当前会话状态为 {self.state}，请先重置")
            if not os.path.isdir(os.path.join(self.repo_path, ".git")):
                raise BisectError(f"不是 git 仓库: {self.repo_path}")
            if not (
                os.path.isfile(self.script_path)
                and os.access(self.script_path, os.X_OK)
            ):
                raise BisectError(f"检查脚本不存在或不可执行: {self.script_path}")

            good_sha = self._resolve(good)
            bad_sha = self._resolve(bad)
            if good_sha == bad_sha:
                raise BisectError("good 与 bad 不能是同一个提交")
            proc = subprocess.run(
                ["git", "-C", self.repo_path, "merge-base", "--is-ancestor",
                 good_sha, bad_sha],
                capture_output=True, text=True,
            )
            if proc.returncode == 1:
                raise BisectError("good 必须是 bad 的祖先（失败应出现在通过之后）")
            if proc.returncode != 0:
                raise BisectError(f"git merge-base 失败: {proc.stderr.strip()}")
            merges = _git(
                self.repo_path, "rev-list", "--merges", f"{good_sha}..{bad_sha}"
            ).split()
            if merges:
                raise BisectError("边界之间存在合并提交，本工具只支持线性历史")

            commits = _git(
                self.repo_path, "rev-list", "--reverse", f"{good_sha}..{bad_sha}"
            ).split()
            subjects = {good_sha: self._subject_of(good_sha)}
            for sha in commits:
                subjects[sha] = self._subject_of(sha)

            self._reset_state()
            self.state = STATE_RUNNING
            self.good_boundary = good_sha
            self.bad_boundary = bad_sha
            self.subjects = subjects
            self.commits = commits
            self._index = {sha: i for i, sha in enumerate(commits)}
            self.hi = len(commits) - 1  # bad 边界即最初的“已知最早失败”
            self.started_at = time.time()
            os.makedirs(self.worktree_root, exist_ok=True)
            with self._git_lock:
                try:
                    _git(self.repo_path, "worktree", "prune")
                except BisectError:
                    pass
            self._log(
                f"开始二分：good={good_sha[:10]} bad={bad_sha[:10]}，"
                f"候选提交 {len(commits)} 个，并发上限 {self.max_concurrent}"
            )
            self._thread = threading.Thread(
                target=self._run, name="bisect-scheduler", daemon=True
            )
            self._thread.start()

    def request_reset(self) -> None:
        """请求重置：终止所有在跑检查，等它们退出后回到 idle。"""
        with self._lock:
            if self.state == STATE_IDLE:
                return
            self.reset_requested = True
            self._terminate_all_locked("会话被重置")
            thread_alive = self._thread is not None and self._thread.is_alive()
            if not self._active_checks() and not thread_alive:
                self._reset_state()

    def snapshot(self) -> dict:
        """给 UI/API 的一致快照。"""
        with self._lock:
            active_by_sha = {c.sha: c for c in self._active_checks()}
            rows = []
            for i, sha in enumerate(self.commits):
                if sha in self.results:
                    status = self.results[sha]
                elif sha in active_by_sha:
                    status = active_by_sha[sha].status
                elif self.state != STATE_IDLE and (i <= self.lo or i >= self.hi):
                    status = "out-of-range"
                else:
                    status = "untested"
                rows.append({
                    "sha": sha,
                    "short": sha[:10],
                    "subject": self.subjects.get(sha, ""),
                    "status": status,
                    "is_bad_boundary": sha == self.bad_boundary,
                })
            needed = len(self._needed_indices(self._active_checks())) \
                if self.state == STATE_RUNNING else 0
            return {
                "state": self.state,
                "repo_path": self.repo_path,
                "script_path": self.script_path,
                "max_concurrent": self.max_concurrent,
                "good_boundary": self._sha_info(self.good_boundary),
                "bad_boundary": self._sha_info(self.bad_boundary),
                "known_good": self._sha_info(
                    self.good_boundary if self.lo == -1 else self.commits[self.lo]
                ),
                "first_bad": self._sha_info(
                    self.commits[self.hi] if self.hi >= 0 else None
                ),
                "commits": rows,
                "checks": [self._check_info(c) for c in self.checks],
                "events": list(self.events),
                "answer": self._sha_info(self.answer),
                "possibilities": [self._sha_info(s) for s in self.possibilities]
                if self.possibilities else None,
                "abort_reason": self.abort_reason,
                "progress": {
                    "total": len(self.commits),
                    "tested": len(self.results),
                    "remaining": needed,
                    "active": len(self._active_checks()),
                    "range_size": max(0, self.hi - self.lo - 1)
                    if self.state != STATE_IDLE else 0,
                },
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }

    # ------------------------------------------------------------------
    # 调度主循环
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._lock:
                try:
                    self._reap_finished()
                    if self.state == STATE_RUNNING and not self.reset_requested:
                        self._enforce_range()
                        self._progress()
                    self._maybe_escalate()
                    if self.reset_requested:
                        self._terminate_all_locked("会话被重置")
                        if not self._active_checks():
                            self._reset_state()
                            return
                    elif self.state in TERMINAL_STATES and not self._active_checks():
                        return  # 会话结束，状态保留供 UI 查询，线程退出
                except Exception as exc:  # 调度器绝不能死
                    self._log(f"调度器内部错误: {exc!r}")
            time.sleep(self.tick)

    def _active_checks(self) -> list[Check]:
        # 进程未退出即占用名额（即使已被终止、结果将被丢弃）
        return [c for c in self.checks if c.exit_code is None]

    def _needed_indices(self, active: list[Check]) -> list[int]:
        running = {c.sha for c in active}
        return [
            i for i in range(self.lo + 1, self.hi)
            if self.commits[i] not in self.results
            and self.commits[i] not in running
        ]

    def _progress(self) -> None:
        """有待测提交且有空位就启动检查；没有可测的了就收尾。"""
        active = self._active_checks()
        needed = self._needed_indices(active)
        if not needed:
            if not active:
                self._finalize()
            return
        free = self.max_concurrent - len(active)
        for idx in self._pick(needed, free):
            self._launch(self.commits[idx])

    @staticmethod
    def _pick(indices: list[int], k: int) -> list[int]:
        """从待测下标中均匀选取至多 k 个（k=2 时约在 1/3、2/3 处）。"""
        if k <= 0 or not indices:
            return []
        n = len(indices)
        if k >= n:
            return list(indices)
        picks, seen = [], set()
        for j in range(k):
            idx = indices[(j + 1) * n // (k + 1)]
            if idx not in seen:
                seen.add(idx)
                picks.append(idx)
        return picks

    # ------------------------------------------------------------------
    # 检查的启动、等待、收割
    # ------------------------------------------------------------------

    def _launch(self, sha: str) -> None:
        check_id = self._next_id
        self._next_id += 1
        path = os.path.join(self.worktree_root, f"chk-{check_id}")
        try:
            if os.path.exists(path):
                shutil.rmtree(path, ignore_errors=True)
            with self._git_lock:
                _git(self.repo_path, "worktree", "add", "--detach", path, sha)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            proc = subprocess.Popen(
                [self.script_path],
                cwd=path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                start_new_session=True,  # 独立进程组，便于整组终止
            )
        except Exception as exc:
            self._abort_locked(f"无法在 {sha[:10]} 上启动检查: {exc}")
            return
        check = Check(id=check_id, sha=sha, worktree=path, proc=proc,
                      started_at=time.time())
        self.checks.append(check)
        threading.Thread(
            target=self._waiter, args=(check,), daemon=True,
            name=f"bisect-waiter-{check_id}",
        ).start()
        self._log(f"检查 #{check_id} 启动：{sha[:10]}（worktree: {path}）")

    def _waiter(self, check: Check) -> None:
        """每个检查一个等待线程：收输出、等进程退出。"""
        try:
            out, _ = check.proc.communicate()
        except Exception:
            out = ""
        with self._lock:
            check.output_tail = (out or "")[-4000:]
            check.exit_code = check.proc.returncode
            check.finished_at = time.time()

    def _reap_finished(self) -> None:
        for check in self.checks:
            if check.reaped or check.exit_code is None:
                continue
            check.reaped = True
            check.status = CHECK_FINISHED
            idx = self._index[check.sha]
            out_of_range = idx <= self.lo or idx >= self.hi
            if (self.state != STATE_RUNNING or self.reset_requested
                    or check.discarded or out_of_range):
                check.discarded = True
                if not check.terminate_reason:
                    if out_of_range:
                        check.terminate_reason = "搜索范围缩小，该提交已在范围外"
                    elif self.reset_requested:
                        check.terminate_reason = "会话被重置"
                    elif self.state == STATE_ABORTED:
                        check.terminate_reason = "会话已中止"
                reason = f"（{check.terminate_reason}）" if check.terminate_reason else ""
                self._log(
                    f"检查 #{check.id}（{check.sha[:10]}）退出码 "
                    f"{check.exit_code}，结果被丢弃{reason}"
                )
            else:
                code = check.exit_code
                if code == 0:
                    self._record(check, VERDICT_PASS)
                    self.lo = max(self.lo, idx)
                elif code == 1:
                    self._record(check, VERDICT_FAIL)
                    self.hi = min(self.hi, idx)
                elif code == 125:
                    self._record(check, VERDICT_SKIP)
                else:
                    tail_lines = [
                        ln for ln in check.output_tail.strip().splitlines()
                        if ln.strip()
                    ]
                    tail = " | ".join(tail_lines[-3:])[:500]
                    self._abort_locked(
                        f"检查 #{check.id}（提交 {check.sha}）的脚本退出码为 "
                        f"{code}，不是约定的 0/1/125，本轮中止。"
                        + (f"脚本输出末尾: {tail}" if tail else "")
                    )
            self._remove_worktree(check)

    def _record(self, check: Check, verdict: str) -> None:
        self.results[check.sha] = verdict
        check.verdict = verdict
        label = {VERDICT_PASS: "通过", VERDICT_FAIL: "失败",
                 VERDICT_SKIP: "不可测"}[verdict]
        self._log(f"检查 #{check.id}（{check.sha[:10]}）结果：{label}")

    # ------------------------------------------------------------------
    # 范围收缩后的终止
    # ------------------------------------------------------------------

    def _enforce_range(self) -> None:
        """终止已落在 (lo, hi] 范围之外、仍在运行的检查。"""
        for check in self._active_checks():
            idx = self._index[check.sha]
            if idx <= self.lo or idx >= self.hi:
                self._terminate_locked(check, "搜索范围缩小，该提交已在范围外")

    def _terminate_locked(self, check: Check, reason: str) -> None:
        if check.status == CHECK_TERMINATING or check.exit_code is not None:
            return
        check.discarded = True
        check.status = CHECK_TERMINATING
        check.terminate_reason = reason
        check.terminate_sent_at = time.time()
        try:
            os.killpg(check.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        self._log(f"检查 #{check.id}（{check.sha[:10]}）被终止：{reason}")

    def _terminate_all_locked(self, reason: str) -> None:
        for check in self._active_checks():
            self._terminate_locked(check, reason)

    def _maybe_escalate(self) -> None:
        """SIGTERM 后超过 escalate_after 秒仍未退出的进程升级为 SIGKILL。"""
        now = time.time()
        for check in self.checks:
            if (
                check.status == CHECK_TERMINATING
                and check.exit_code is None
                and not check.escalated
                and check.terminate_sent_at is not None
                and now - check.terminate_sent_at > self.escalate_after
            ):
                check.escalated = True
                try:
                    os.killpg(check.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                self._log(
                    f"检查 #{check.id}（{check.sha[:10]}）未响应 SIGTERM，"
                    "已发送 SIGKILL"
                )

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------

    def _abort_locked(self, reason: str) -> None:
        if self.state != STATE_RUNNING:
            return
        self.state = STATE_ABORTED
        self.abort_reason = reason
        self.finished_at = time.time()
        self._log(f"本轮中止：{reason}")
        self._terminate_all_locked("会话已中止")

    def _finalize(self) -> None:
        # 首个失败提交只可能是：已知最早失败 hi，或 (lo, hi) 内的不可测提交
        possible = [
            self.commits[i] for i in range(self.lo + 1, self.hi + 1)
            if i == self.hi or self.results.get(self.commits[i]) == VERDICT_SKIP
        ]
        self.finished_at = time.time()
        if len(possible) == 1:
            self.state = STATE_DONE
            self.answer = possible[0]
            self._log(f"首个失败提交已确定：{possible[0]}")
        else:
            self.state = STATE_AMBIGUOUS
            self.possibilities = possible
            self._log(
                f"存在不可测提交，无法唯一确定首个失败提交，"
                f"剩余 {len(possible)} 个候选"
            )

    def _remove_worktree(self, check: Check) -> None:
        try:
            with self._git_lock:
                _git(self.repo_path, "worktree", "remove", "--force",
                     check.worktree)
        except Exception as exc:
            self._log(f"清理 worktree {check.worktree} 失败: {exc}，尝试强制删除")
            try:
                shutil.rmtree(check.worktree, ignore_errors=True)
                with self._git_lock:
                    _git(self.repo_path, "worktree", "prune")
            except Exception as exc2:
                self._log(f"强制清理 worktree 仍失败: {exc2}")

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------

    def _resolve(self, rev: str) -> str:
        try:
            return _git(
                self.repo_path, "rev-parse", "--verify", f"{rev}^{{commit}}"
            ).strip()
        except BisectError:
            raise BisectError(f"无法解析提交: {rev}")

    def _subject_of(self, sha: str) -> str:
        try:
            return _git(self.repo_path, "show", "-s", "--format=%s", sha).strip()
        except BisectError:
            return ""

    def _sha_info(self, sha: str | None) -> dict | None:
        if sha is None:
            return None
        return {"sha": sha, "short": sha[:10],
                "subject": self.subjects.get(sha, "")}

    @staticmethod
    def _check_info(check: Check) -> dict:
        return {
            "id": check.id,
            "sha": check.sha,
            "short": check.sha[:10],
            "status": check.status,
            "discarded": check.discarded,
            "terminate_reason": check.terminate_reason,
            "exit_code": check.exit_code,
            "verdict": check.verdict,
            "started_at": check.started_at,
            "finished_at": check.finished_at,
            "output_tail": check.output_tail[-1000:],
        }
