"""并行 Git 二分定位引擎（worktree 池化复用版）。

维护者给定一个已知通过的提交（good）和一个已知失败的提交（bad），
假定“失败一旦出现就会延续”（单调假设）。引擎在 (good, bad] 的线性
历史上做二分：

- 有待测提交且有空位时就启动检查，最多同时 max_concurrent 个；
- 每个检查在独立的 `git worktree` 里执行同一个固定脚本；
- **同一轮内 worktree 池化复用**：检查退出后工作树回到池中，后续检查
  优先复用，减少重复检出的磁盘写入；
- 复用切换提交时，上次检出后未被改动、且在源/目标提交中内容（blob）
  与文件模式都相同的已跟踪文件，原样保留文件与修改时间；其余文件由
  git 检出重写。未跟踪文件与忽略文件每次准备前一律清除，因此每次脚本
  启动时 HEAD、暂存区、目录内容与全新检出目标提交完全一致；
- 准备中（preparing）、检查占用中（busy）、清理中（cleaning/removing）
  的工作树都计入并发上限；只有空闲（idle）工作树不计；
- 准备中的提交若被排除出搜索范围，跳过脚本启动，工作树清理后留给后续；
- 清理失败先尝试移除再补建；移除失败则中止本轮并在页面提示；
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
from dataclasses import dataclass, field

# 检查结论
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_SKIP = "skip"  # 不可测（退出码 125）

# 会话状态
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "done"            # 唯一确定首个失败提交
STATE_AMBIGUOUS = "ambiguous"  # 不可测提交导致无法唯一确定
STATE_ABORTED = "aborted"      # 脚本退出码非法 / worktree 故障，本轮中止

# 单个检查的状态
CHECK_PREPARING = "preparing"      # 工作树准备中（新建或复用检出）
CHECK_RUNNING = "running"
CHECK_TERMINATING = "terminating"  # 已发 SIGTERM，等待进程退出
CHECK_FINISHED = "finished"

# 工作树（Worktree）状态
WT_PREPARING = "preparing"  # 正在新建或复用检出，占用名额
WT_BUSY = "busy"            # 检查脚本运行中，占用名额
WT_CLEANING = "cleaning"    # 检查结束后清理为可复用状态，占用名额
WT_REMOVING = "removing"    # 轮次结束/故障后移除中，占用名额
WT_IDLE = "idle"            # 干净待命，可被复用，不占名额

# 工作树来源标记
ORIGIN_NEW = "new"    # 本轮新建（git worktree add）
ORIGIN_REUSED = "reused"  # 复用池中已有工作树

TERMINAL_STATES = (STATE_DONE, STATE_AMBIGUOUS, STATE_ABORTED)
# 占用并发名额的工作树状态
OCCUPYING_STATES = (WT_PREPARING, WT_BUSY, WT_CLEANING, WT_REMOVING)


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
    """一次针对某个提交的检查（含从未启动脚本的准备期检查）。"""

    id: int
    sha: str
    worktree: str
    proc: subprocess.Popen | None = None
    status: str = CHECK_PREPARING
    worktree_origin: str | None = None  # new / reused，准备完成后确定
    discarded: bool = False          # 结果被丢弃（范围外被终止 / 会话中止）
    skipped_prep: bool = False       # 准备完成但提交已被排除，未启动脚本
    terminate_reason: str | None = None
    terminate_sent_at: float | None = None
    escalated: bool = False          # 是否已升级为 SIGKILL
    exit_code: int | None = None
    verdict: str | None = None       # pass / fail / skip
    output_tail: str = ""
    started_at: float = 0.0          # 进入准备期的时间
    script_started_at: float | None = None
    finished_at: float | None = None
    reaped: bool = False


@dataclass
class Worktree:
    """池中一个工作树及其生命周期状态。"""

    name: str
    path: str
    state: str = WT_PREPARING
    check_id: int | None = None
    sha: str | None = None              # 当前 HEAD 指向的提交
    # 准备完成后、脚本启动前采集的已跟踪文件 mtime 快照：path -> mtime_ns
    mtime_snapshot: dict[str, int] = field(default_factory=dict)
    last_origin: str | None = None      # 最近一次绑定的来源（new/reused）
    remove_in_progress: bool = False    # 是否有移除线程在执行
    remove_failed: bool = False         # 最近一次移除是否失败


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
        self._git_lock = threading.Lock()  # 串行化主仓库元数据变更
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
        self.worktrees: dict[str, Worktree] = {}  # name -> Worktree
        self._next_id = 1
        self._next_wt_seq = 1
        self.events: list[dict] = []
        self.answer: str | None = None
        self.possibilities: list[str] | None = None
        self.abort_reason: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None

    def _startup_cleanup(self) -> None:
        """进程启动时清掉上次遗留的 worktree（目录与 git 元数据）。

        同时清掉上次取消可能残留的未跟踪/忽略文件：整个工作树根目录
        被删除，等价于全新开始。
        """
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
        """请求重置：终止所有在跑检查，等工作树全部移除后回到 idle。"""
        with self._lock:
            if self.state == STATE_IDLE:
                return
            self.reset_requested = True
            self._terminate_all_locked("会话被重置")
            self._begin_teardown_locked()
            thread_alive = self._thread is not None and self._thread.is_alive()
            if not self._occupied_worktrees() and not thread_alive:
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
                "worktrees": [self._worktree_info(w) for w in self.worktrees.values()],
                "events": list(self.events),
                "answer": self._sha_info(self.answer),
                "possibilities": [self._sha_info(s) for s in self.possibilities]
                if self.possibilities else None,
                "abort_reason": self.abort_reason,
                "progress": {
                    "total": len(self.commits),
                    "tested": len(self.results),
                    "remaining": needed,
                    "active": len(self._occupied_worktrees()),
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
                        self._begin_teardown_locked()
                        if not self._occupied_worktrees():
                            self._reset_state()
                            return
                    elif self.state in TERMINAL_STATES:
                        self._begin_teardown_locked()
                        if not self._occupied_worktrees():
                            return  # 工作树全部移除，状态保留供 UI 查询
                except Exception as exc:  # 调度器绝不能死
                    self._log(f"调度器内部错误: {exc!r}")
            time.sleep(self.tick)

    def _active_checks(self) -> list[Check]:
        # 仍在占用工作树的检查：进程未退出（含已被终止、结果将被丢弃），
        # 或仍处于准备期。准备期跳过（skipped_prep）的检查已交还工作树，
        # 不再活跃；其提交会因没有结论而被重新调度。
        return [
            c for c in self.checks
            if not c.reaped and not c.skipped_prep and c.exit_code is None
        ]

    def _occupied_worktrees(self) -> list[Worktree]:
        """占用并发名额的工作树（准备/占用/清理/移除中）。"""
        return [w for w in self.worktrees.values() if w.state in OCCUPYING_STATES]

    def _idle_worktrees(self) -> list[Worktree]:
        return [w for w in self.worktrees.values() if w.state == WT_IDLE]

    def _needed_indices(self, active: list[Check]) -> list[int]:
        bound = {c.sha for c in active}
        return [
            i for i in range(self.lo + 1, self.hi)
            if self.commits[i] not in self.results
            and self.commits[i] not in bound
        ]

    def _progress(self) -> None:
        """有待测提交且有空位就启动检查；没有可测的了就收尾。"""
        active = self._active_checks()
        needed = self._needed_indices(active)
        if not needed:
            if not self._occupied_worktrees():
                self._finalize()
            return
        # 名额按工作树占用计算（准备/清理中的工作树都占位）
        free = self.max_concurrent - len(self._occupied_worktrees())
        for idx in self._pick(needed, free):
            self._assign(self.commits[idx])

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
    # 工作树分配：优先复用空闲工作树，否则在名额内新建
    # ------------------------------------------------------------------

    def _assign(self, sha: str) -> None:
        """为提交绑定一个工作树并启动准备线程。"""
        check_id = self._next_id
        self._next_id += 1
        idle = self._idle_worktrees()
        if idle:
            wt = idle[0]
            origin = ORIGIN_REUSED
            source_sha = wt.sha  # 必须在改写 wt.sha 前记录源提交
            wt.state = WT_PREPARING
        else:
            if len(self._occupied_worktrees()) >= self.max_concurrent:
                return  # 名额已满（理论上 _progress 已控制，防御性返回）
            name = f"wt-{self._next_wt_seq}"
            self._next_wt_seq += 1
            wt = Worktree(
                name=name,
                path=os.path.join(self.worktree_root, name),
                state=WT_PREPARING,
            )
            origin = ORIGIN_NEW
            source_sha = None
            self.worktrees[name] = wt
        wt.check_id = check_id
        wt.sha = sha
        wt.last_origin = origin
        check = Check(
            id=check_id, sha=sha, worktree=wt.path, status=CHECK_PREPARING,
            worktree_origin=origin, started_at=time.time(),
        )
        self.checks.append(check)
        self._log(
            f"检查 #{check_id} 进入准备：{sha[:10]}，"
            f"{'复用' if origin == ORIGIN_REUSED else '新建'}工作树 {wt.name}"
        )
        threading.Thread(
            target=self._prepare_worker,
            args=(wt.name, sha, origin, check_id, source_sha),
            daemon=True, name=f"bisect-prep-{check_id}",
        ).start()

    # ------------------------------------------------------------------
    # 工作树准备（在独立线程执行阻塞 git 操作）
    # ------------------------------------------------------------------

    def _prepare_worker(
        self,
        wt_name: str,
        sha: str,
        origin: str,
        check_id: int,
        source_sha: str | None = None,
    ) -> None:
        """新建或复用工作树并检出目标提交；失败按约定处理。"""
        try:
            with self._lock:
                wt = self.worktrees.get(wt_name)
                if wt is None or wt.check_id != check_id:
                    return  # 已被重置/接管
                # 复用上一轮准备完成后采集的 mtime 快照（脚本启动前基线）
                prev_snapshot = dict(wt.mtime_snapshot)
            if origin == ORIGIN_NEW:
                self._create_worktree(wt.path, sha)
            else:
                self._reuse_worktree(
                    wt.path, sha, source_sha, prev_snapshot
                )
            # 准备完成：采集新的 mtime 快照（本次脚本启动前的基线）
            snapshot = self._snapshot_mtimes(wt.path)
            with self._lock:
                wt = self.worktrees.get(wt_name)
                if wt is None or wt.check_id != check_id:
                    return
                check = self._check_by_id(check_id)
                if check is None or check.reaped:
                    return
                wt.mtime_snapshot = snapshot
                wt.sha = sha
                idx = self._index[sha]
                out_of_range = idx <= self.lo or idx >= self.hi
                if (
                    self.state != STATE_RUNNING
                    or self.reset_requested
                    or out_of_range
                ):
                    # 准备期间提交已被排除：跳过脚本，工作树清理后留给后续
                    check.skipped_prep = True
                    check.discarded = True
                    check.status = CHECK_FINISHED
                    check.finished_at = time.time()
                    if out_of_range:
                        check.terminate_reason = "准备完成时提交已被排除出搜索范围"
                    elif self.reset_requested:
                        check.terminate_reason = "会话被重置"
                    else:
                        check.terminate_reason = "会话已中止"
                    self._log(
                        f"检查 #{check_id}（{sha[:10]}）准备完成但提交已被排除，"
                        "跳过脚本，工作树留待复用"
                    )
                    self._cleanup_async(wt)
                    return
                # 正常启动检查脚本
                proc = self._spawn_script(wt.path)
                check.proc = proc
                check.status = CHECK_RUNNING
                check.script_started_at = time.time()
                wt.state = WT_BUSY
            threading.Thread(
                target=self._waiter, args=(check_id,), daemon=True,
                name=f"bisect-waiter-{check_id}",
            ).start()
            with self._lock:
                self._log(
                    f"检查 #{check_id} 启动脚本：{sha[:10]}（工作树 {wt_name}，"
                    f"{'复用' if origin == ORIGIN_REUSED else '新建'}）"
                )
        except Exception as exc:
            with self._lock:
                self._handle_prepare_failure(wt_name, check_id, sha, exc)

    def _create_worktree(self, path: str, sha: str) -> None:
        """全新 git worktree add --detach。"""
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
        with self._git_lock:
            _git(self.repo_path, "worktree", "add", "--detach", path, sha)

    def _reuse_worktree(
        self, path: str, target_sha: str, source_sha: str | None,
        snapshot: dict[str, int],
    ) -> None:
        """复用已有工作树切换到目标提交。

        序列：
        1. checkout 前先记录被脚本改动过的已跟踪文件（内容或模式变化）；
        2. `checkout -f --detach <target>` 强制检出目标提交；
        3. `clean -ffdx` 清除未跟踪与忽略文件；
        4. 对“脚本未改动、且源/目标提交中 blob 与模式都相同”的文件恢复
           快照 mtime，其余文件保留 checkout 产生的新 mtime。

        完成后 HEAD、暂存区、目录内容与全新检出完全一致。
        """
        # 必须在 checkout 前采集：checkout 会把工作树还原，事后无法区分改动
        modified = self._modified_tracked(path) if source_sha is not None else set()
        _git(path, "checkout", "-f", "--detach", target_sha)
        # -f 两次：连嵌套的未跟踪 git 仓库也一并清掉；-x 清忽略文件
        _git(path, "clean", "-q", "-ffdx")
        if source_sha is not None:
            self._restore_mtimes(
                path, snapshot, source_sha, target_sha, modified
            )

    @staticmethod
    def _modified_tracked(path: str) -> set[str]:
        """返回相对 HEAD 内容或模式被改动过的已跟踪文件集合（脚本污染检测）。"""
        proc = subprocess.run(
            ["git", "-C", path, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, check=True,
        )
        modified = set()
        for line in proc.stdout.splitlines():
            if len(line) < 4:
                continue
            # 形如 " M path"、"M  path"、" M path"、"T  path"（类型变化）等
            rel = line[3:]
            if " -> " in rel:  # 重命名 R100 old -> new
                rel = rel.split(" -> ", 1)[1]
            modified.add(rel)
        return modified

    def _restore_mtimes(
        self,
        path: str,
        snapshot: dict[str, int],
        source_sha: str,
        target_sha: str,
        modified: set[str],
    ) -> None:
        """恢复未改动且源/目标一致文件的 mtime；其余文件刷新为当前时间。"""
        src = self._tree_meta(path, source_sha)
        tgt = self._tree_meta(path, target_sha)
        for rel, mtime_ns in snapshot.items():
            full = os.path.join(path, rel)
            if not os.path.lexists(full) or rel not in tgt or rel not in src:
                continue  # 目标中已删除
            untouched = rel not in modified
            same_in_both = tgt[rel] == src[rel]
            if untouched and same_in_both:
                # 脚本未改动 + 源/目标 blob 与模式都相同：保留原文件与 mtime
                os.utime(full, ns=(mtime_ns, mtime_ns), follow_symlinks=False)
            else:
                # 被脚本改动过，或内容/模式在提交间变化：不保留旧 mtime。
                # 若 git 只调了模式（chmod 不改 mtime），显式刷新以体现重写。
                if os.path.islink(full) or os.path.isfile(full):
                    now = time.time_ns()
                    os.utime(full, ns=(now, now), follow_symlinks=False)

    @staticmethod
    def _snapshot_mtimes(path: str) -> dict[str, int]:
        """采集工作树中所有现存已跟踪文件的 mtime（纳秒）。"""
        proc = subprocess.run(
            ["git", "-C", path, "ls-files"],
            capture_output=True, text=True, check=True,
        )
        snapshot = {}
        for rel in proc.stdout.splitlines():
            full = os.path.join(path, rel)
            if os.path.lexists(full):
                snapshot[rel] = os.lstat(full).st_mtime_ns
        return snapshot

    @staticmethod
    def _tree_meta(path: str, rev: str) -> dict[str, tuple[str, str]]:
        """返回某提交 tree 中 路径 -> (mode, blob) 的映射。"""
        proc = subprocess.run(
            ["git", "-C", path, "ls-tree", "-r", rev],
            capture_output=True, text=True, check=True,
        )
        out = {}
        for line in proc.stdout.splitlines():
            meta, rel = line.split("\t", 1)
            mode, _typ, blob = meta.split()
            out[rel] = (mode, blob)
        return out

    def _spawn_script(self, path: str) -> subprocess.Popen:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.Popen(
            [self.script_path],
            cwd=path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,  # 独立进程组，便于整组终止
        )

    def _handle_prepare_failure(
        self, wt_name: str, check_id: int, sha: str, exc: Exception
    ) -> None:
        """准备失败：尝试移除再补建；移除失败则中止本轮。

        破坏性 IO 在独立线程、锁外执行，避免阻塞调度循环。移除成功后工作树
        从池中删除，该提交仍在待测集合中，调度器会在有名额时补建新工作树。
        """
        wt = self.worktrees.get(wt_name)
        self._log(
            f"工作树 {wt_name} 准备提交 {sha[:10]} 失败: {exc}，尝试移除后补建"
        )
        if wt is None:
            self._abort_locked(f"工作树 {wt_name} 状态丢失，本轮中止")
            return
        # 统一交给清理失败的恢复路径（异步、锁外 IO）
        self._recover_after_failure_async(wt, exc)

    def _recover_after_failure_async(self, wt: Worktree, exc: Exception) -> None:
        """在锁外移除故障工作树，再回到锁内更新状态或中止。"""
        wt.state = WT_REMOVING
        wt.remove_in_progress = True
        threading.Thread(
            target=self._recover_worker,
            args=(wt.name, str(exc)),
            daemon=True, name=f"bisect-recover-{wt.name}",
        ).start()

    def _recover_worker(self, wt_name: str, exc_msg: str) -> None:
        with self._lock:
            wt = self.worktrees.get(wt_name)
            if wt is None:
                return
            check_id = wt.check_id
        # 锁外执行阻塞的 git/删除操作
        removed = self._destroy_worktree(wt)
        with self._lock:
            wt = self.worktrees.get(wt_name)
            if wt is None:
                return
            wt.remove_in_progress = False
            if not removed:
                wt.remove_failed = True
                self._abort_locked(
                    f"工作树 {wt_name} 移除失败，本轮中止。原始错误: {exc_msg}"
                )
                return
            self.worktrees.pop(wt_name, None)
            self._log(f"工作树 {wt_name} 已移除，后续检查将在有名额时补建新工作树")
            # 绑定的准备期检查标记结束（结果丢弃）；提交仍在待测集合，会重调度
            if check_id is not None:
                check = self._check_by_id(check_id)
                if check is not None and not check.reaped:
                    check.reaped = True
                    check.status = CHECK_FINISHED
                    check.discarded = True
                    check.finished_at = time.time()

    # ------------------------------------------------------------------
    # 检查进程的等待、收割
    # ------------------------------------------------------------------

    def _check_by_id(self, check_id: int) -> Check | None:
        for c in self.checks:
            if c.id == check_id:
                return c
        return None

    def _worktree_of(self, check_id: int) -> Worktree | None:
        for w in self.worktrees.values():
            if w.check_id == check_id:
                return w
        return None

    def _waiter(self, check_id: int) -> None:
        """每个检查一个等待线程：收输出、等进程退出。"""
        with self._lock:
            check = self._check_by_id(check_id)
            proc = check.proc if check else None
        if proc is None:
            return
        try:
            out, _ = proc.communicate()
        except Exception:
            out = ""
        with self._lock:
            check = self._check_by_id(check_id)
            if check is None:
                return
            check.output_tail = (out or "")[-4000:]
            check.exit_code = proc.returncode
            check.finished_at = time.time()

    def _reap_finished(self) -> None:
        for check in self.checks:
            if check.reaped or check.exit_code is None:
                continue
            check.reaped = True
            check.status = CHECK_FINISHED
            wt = self._worktree_of(check.id)
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
            # 进程已退出：工作树进入清理（占用名额直到清理完）
            if wt is not None and wt.state == WT_BUSY:
                self._cleanup_async(wt)

    def _record(self, check: Check, verdict: str) -> None:
        self.results[check.sha] = verdict
        check.verdict = verdict
        label = {VERDICT_PASS: "通过", VERDICT_FAIL: "失败",
                 VERDICT_SKIP: "不可测"}[verdict]
        self._log(f"检查 #{check.id}（{check.sha[:10]}）结果：{label}")

    # ------------------------------------------------------------------
    # 工作树清理与移除
    # ------------------------------------------------------------------

    def _cleanup_async(self, wt: Worktree) -> None:
        """把工作树清理为可复用的空闲状态（异步，期间占用名额）。"""
        if wt.state not in (WT_BUSY, WT_PREPARING):
            return
        wt.state = WT_CLEANING
        threading.Thread(
            target=self._cleanup_worker, args=(wt.name,),
            daemon=True, name=f"bisect-clean-{wt.name}",
        ).start()

    def _cleanup_worker(self, wt_name: str) -> None:
        with self._lock:
            wt = self.worktrees.get(wt_name)
            if wt is None:
                return
            # 会话已结束/正在重置：直接移除，不再回到池中
            if self.state != STATE_RUNNING or self.reset_requested:
                self._remove_async(wt)
                return
            path = wt.path
        try:
            # 先还原暂存区与工作树的已跟踪文件（脚本可能 git add / 改内容），
            # 再清未跟踪与忽略文件（-f 两次连嵌套 git 仓库也清掉）。
            _git(path, "reset", "-q", "--hard", "HEAD")
            _git(path, "clean", "-q", "-ffdx")
            with self._lock:
                wt = self.worktrees.get(wt_name)
                if wt is None:
                    return
                if self.state != STATE_RUNNING or self.reset_requested:
                    self._remove_async(wt)
                    return
                wt.state = WT_IDLE
                wt.check_id = None
                # 清理后重新采集快照，作为下一次复用的 mtime 基线。
                # reset 重写过的跟踪文件 mtime 为清理时刻，即“当前检出状态”，
                # 下次复用时若源/目标一致就保留这个值，符合未被脚本改动的语义。
                wt.mtime_snapshot = self._snapshot_mtimes(path)
                self._log(f"工作树 {wt_name} 清理完成，回到池中待命")
        except Exception as exc:
            with self._lock:
                wt = self.worktrees.get(wt_name)
                if wt is None:
                    return
                self._log(f"工作树 {wt_name} 清理失败: {exc}，尝试移除后补建")
                self._recover_after_failure_async(wt, exc)

    def _begin_teardown_locked(self) -> None:
        """轮次结束/重置：把所有未在移除中的工作树异步移除。

        已处于 REMOVING 但移除线程已结束（remove_failed）的工作树会重试，
        避免一次失败后永久卡住、会话无法回到 idle。
        """
        for wt in list(self.worktrees.values()):
            if wt.state == WT_REMOVING and (wt.remove_in_progress or not wt.remove_failed):
                continue
            self._remove_async(wt)

    def _remove_async(self, wt: Worktree) -> None:
        if wt.state == WT_REMOVING and wt.remove_in_progress:
            return  # 已有移除线程在跑，不重复发起
        wt.state = WT_REMOVING
        wt.remove_in_progress = True
        wt.remove_failed = False
        threading.Thread(
            target=self._remove_worker, args=(wt.name,),
            daemon=True, name=f"bisect-remove-{wt.name}",
        ).start()

    def _remove_worker(self, wt_name: str) -> None:
        with self._lock:
            wt = self.worktrees.get(wt_name)
            if wt is None:
                return
        ok = self._destroy_worktree(wt)
        with self._lock:
            wt = self.worktrees.get(wt_name)
            if wt is None:
                return
            wt.remove_in_progress = False
            if ok:
                self.worktrees.pop(wt_name, None)
                self._log(f"工作树 {wt_name} 已移除")
            elif self.state == STATE_RUNNING and not self.reset_requested:
                # 运行中移除失败：中止本轮，工作树保留为 removing 占位，
                # 等待用户重置（重置会重新尝试移除）。
                wt.remove_failed = True
                self._abort_locked(
                    f"工作树 {wt_name} 移除失败，本轮中止；"
                    "请检查磁盘权限或手动清理后重置"
                )
            elif self.reset_requested:
                # 重置中仍失败：再重试若干次由用户的后续重置触发；
                # 这里标记失败并保留占位，request_reset 不会假装已干净。
                wt.remove_failed = True
                self._log(
                    f"工作树 {wt_name} 移除失败，仍保留；可再次点击重试"
                )
            else:
                # 已在终态（非重置）：不能死等，弹出占位并记录，避免线程无法退出
                self.worktrees.pop(wt_name, None)
                self._log(
                    f"工作树 {wt_name} 移除失败但会话已结束，已放弃等待；"
                    "进程下次启动时会清理残留"
                )

    def _destroy_worktree(self, wt: Worktree | None) -> bool:
        """尽力移除工作树：先 git worktree remove，再强删目录 + prune。"""
        if wt is None:
            return True
        path = wt.path
        try:
            with self._git_lock:
                _git(self.repo_path, "worktree", "remove", "--force", path)
            return True
        except Exception as exc:
            self._log(f"git worktree remove {wt.name} 失败: {exc}，强制删除目录")
            try:
                shutil.rmtree(path, ignore_errors=False)
            except FileNotFoundError:
                pass
            except Exception as exc2:
                self._log(f"强制删除目录 {path} 仍失败: {exc2}")
                return False
            try:
                with self._git_lock:
                    _git(self.repo_path, "worktree", "prune")
            except Exception as exc3:
                self._log(f"worktree prune 失败: {exc3}")
                return False
            return not os.path.exists(path)

    # ------------------------------------------------------------------
    # 范围收缩后的终止
    # ------------------------------------------------------------------

    def _enforce_range(self) -> None:
        """终止已落在 (lo, hi] 范围之外、仍在运行的检查。

        准备中的检查不终止（无法安全中断 git 检出），由准备完成逻辑
        判断提交已被排除后跳过脚本。
        """
        for check in self._active_checks():
            if check.status != CHECK_RUNNING:
                continue  # 准备中的检查交给准备完成逻辑
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
            if check.status == CHECK_RUNNING:
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
        self._begin_teardown_locked()

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
        self._begin_teardown_locked()

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

    def _check_info(self, check: Check) -> dict:
        return {
            "id": check.id,
            "sha": check.sha,
            "short": check.sha[:10],
            "status": check.status,
            "worktree_origin": check.worktree_origin,
            "worktree": check.worktree,
            "discarded": check.discarded,
            "skipped_prep": check.skipped_prep,
            "terminate_reason": check.terminate_reason,
            "exit_code": check.exit_code,
            "verdict": check.verdict,
            "started_at": check.started_at,
            "script_started_at": check.script_started_at,
            "finished_at": check.finished_at,
            "output_tail": check.output_tail[-1000:],
        }

    def _worktree_info(self, wt: Worktree) -> dict:
        return {
            "name": wt.name,
            "state": wt.state,
            "sha": wt.sha,
            "check_id": wt.check_id,
            "origin": wt.last_origin,
            "path": wt.path,
        }
