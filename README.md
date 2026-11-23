# Git 构建回归定位页

一个用 Flask 实现的 Git 二分定位（bisect）Web 工具：维护者选定一个已知通过
（good）和一个已知失败（bad）的提交作为边界，工具在 `(good, bad]` 的**线性
历史**上自动定位**首个失败的提交**。假定失败一旦出现就会延续（单调假设）。

## 工作方式

- 有待测提交且有空位时就启动检查，**最多同时检查 2 个提交**（`MAX_CONCURRENT`
  可调）；
- 每个检查在独立的 `git worktree` 中执行**同一个固定脚本**（`CHECK_SCRIPT`），
  互不干扰，也不需要切换当前工作区；
- **同一轮定位内工作树池化复用**：检查退出后工作树回到池中，后续检查优先复用，
  只有池中没有空闲工作树且仍有名额时才新建，显著减少重复检出的磁盘写入；
- **复用切换提交时的写入优化**：上次检出后未被检查脚本改动、且在源提交与目标
  提交中内容（blob）与文件模式都相同的已跟踪文件，原样保留文件和修改时间
  （mtime）；内容或模式不同的文件由 git 检出重写。每次准备都会先
  `git checkout -f --detach <目标>` 再 `git clean -ffdx`，因此**脚本启动时
  HEAD、暂存区、目录内容与全新检出目标提交完全一致**，前次检查留下的未跟踪
  文件和 `.gitignore` 忽略文件都会被清掉；
- **并发名额按工作树占用计算**：准备中（新建/复用检出）、检查占用中、清理中、
  移除中的工作树都计入 `MAX_CONCURRENT`，只有干净待命（idle）的工作树不占位；
- **准备中的提交若被排除**出搜索范围，则跳过脚本启动，把工作树清理后留给后续
  检查复用（运行中的检查仍按 SIGTERM→SIGKILL 终止）；
- **清理/检出失败处理**：清理失败先尝试移除工作树再补建；若移除也失败则中止
  本轮并在页面提示原因；
- 脚本退出码约定：
  | 退出码 | 含义 |
  | ------ | ---- |
  | 0      | 该提交通过 |
  | 1      | 该提交失败 |
  | 125    | 该提交不可测（跳过） |
  | 其他   | 本轮立即中止，页面展示原因（提交、退出码、脚本输出末尾） |
- 搜索结果使范围 `(已知通过, 当前最早失败]` 缩小后，**仍在跑但已落在范围外的
  检查会被终止**（先 SIGTERM，超时未退出再 SIGKILL），其退出结果被**丢弃**；
  **进程真正退出后才释放并发名额**；
- 全部可测提交测完后：
  - 能唯一确定时，页面展示首个失败提交的 SHA；
  - 若范围内存在不可测提交导致无法唯一确定，页面列出**所有可能的 SHA**
    （当前最早失败提交 + 缺口内的不可测提交）。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

首次启动会在 `/data/sample-repo`（命名卷 `bisect-data`）生成一个带线性历史的
样例仓库：9 个提交，第 4 个提交（`Refactor factorial loop`）引入了
`factorial` 差一错误，是首个失败提交；第 6 个提交把 `calc.py` 改了名，属于
不可测提交（固定脚本对其退出 125）。

在页面上选择 good = `Improve division error message`（第 3 个提交）、
bad = `Bump version`（HEAD），点击“开始定位”，几秒内即可看到首个失败提交
的完整 SHA。

## 本地开发

```bash
pip install -r requirements-dev.txt
scripts/make_sample_repo.sh ./sample-repo   # 生成样例仓库
python app.py                               # http://localhost:8000
```

环境变量：

| 变量 | 默认值 | 说明 |
| ---- | ------ | ---- |
| `REPO_PATH` | `./sample-repo`（容器内 `/data/sample-repo`） | 目标 Git 仓库 |
| `CHECK_SCRIPT` | `./scripts/sample_check.sh` | 固定检查脚本 |
| `WORKTREE_ROOT` | `./worktrees`（容器内 `/data/worktrees`） | worktree 临时目录（专用，启动时清空） |
| `MAX_CONCURRENT` | `2` | 并发检查上限 |
| `PORT` | `8000` | 监听端口 |

## 运行自测

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

覆盖：唯一定位、不可测导致的多候选、非法退出码中止、范围缩小后的终止与结果
丢弃、准备期提交被排除时跳过脚本、名额在进程退出后才释放、SIGKILL 升级、
重置复用、边界校验、Flask API，以及工作树池化复用专项（新建/复用标记、
准备/清理工作树计入并发、复用后与全新检出等价、未变文件 mtime 保留、
多跳复用的 mtime 基准、清理失败的恢复与中止、重置后未跟踪/忽略残留清理）。

## 项目结构

```
app.py                      Flask 应用与 API
bisect_engine.py            二分调度引擎（工作树池、并发、终止、收尾逻辑）
templates/index.html        定位页（每秒轮询 /api/status）
scripts/make_sample_repo.sh 生成确定性线性历史的样例仓库
scripts/sample_check.sh     样例仓库对应的固定检查脚本
scripts/entrypoint.sh       容器入口（首启生成样例仓库）
tests/test_bisect_engine.py 自测
```

## 说明与限制

- 仅支持线性历史（边界之间存在合并提交会被拒绝）；
- 单会话：同一时间只跑一轮定位，重置后可开始下一轮；状态保存在内存中，
  进程重启即清空；
- 工作树池在**同一轮定位内**复用；轮次结束（done/ambiguous/aborted）或重置后
  所有工作树都会被移除，进程启动时也会清空工作树根目录，保证每轮从干净状态
  开始；
- 单调假设下，范围外检查的退出结果不影响结论，因此丢弃是安全的；
- 固定脚本应对“构建不起来”的提交退出 125，而不是 1，否则会污染结论。
