# Git 构建回归定位页

一个用 Flask 实现的 Git 二分定位（bisect）Web 工具：维护者选定一个已知通过
（good）和一个已知失败（bad）的提交作为边界，工具在 `(good, bad]` 的**线性
历史**上自动定位**首个失败的提交**。假定失败一旦出现就会延续（单调假设）。

## 工作方式

- 有待测提交且有空位时就启动检查，**最多同时检查 2 个提交**（`MAX_CONCURRENT`
  可调）；
- 每个检查在独立的 `git worktree` 中执行**同一个固定脚本**（`CHECK_SCRIPT`），
  互不干扰，也不需要切换当前工作区；
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
丢弃、名额在进程退出后才释放、SIGKILL 升级、重置复用、边界校验、Flask API。

## 项目结构

```
app.py                      Flask 应用与 API
bisect_engine.py            二分调度引擎（并发、终止、收尾逻辑）
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
- 单调假设下，范围外检查的退出结果不影响结论，因此丢弃是安全的；
- 固定脚本应对“构建不起来”的提交退出 125，而不是 1，否则会污染结论。
