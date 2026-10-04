# standup

`git log` 进，昨天 / 今天 / 阻塞三段式日报出。

每天早上在仓库里跑一行，自动收集上个工作日以来的提交，用一次 LLM 调用起草站会日报。零依赖（Python 标准库），兼容任何 OpenAI-compatible 接口。

## 安装

```bash
# 需要 Python 3.10+
export OPENAI_API_KEY="sk-..."
# 可选：自建 / 第三方兼容接口
export OPENAI_BASE_URL="https://api.deepseek.com/v1"

# 放到 PATH 里（或用 alias）
cp standup.py ~/.local/bin/standup && chmod +x ~/.local/bin/standup
# 也可以直接用：
python3 -m standup
```

## 用法

```bash
cd your-repo
standup # 中文日报（默认）
standup --en # 英文日报
standup --slack # Slack mrkdwn 格式，直接粘贴
standup --json # JSON，方便接入机器人
standup --since "2 days ago" # 自定义统计起点（传给 git --since）
standup --author me # 只统计我自己的提交（按 git user.email）
standup --blocker "等后端联调接口" # 手动补充当前阻塞，可重复
standup --dry-run # 只看收集到的提交和 prompt，不调网络
standup --copy # 顺手复制到剪贴板（pbcopy/xclip/wl-copy）
```

示例输出：

```
2026-10-05

昨天做了什么
- 完成了登录页开发，接入短信验证码流程
- 修复了首页列表滑动崩溃的问题

今天计划（推测）
- 继续推进支付模块开发（分支 feature/pay 仍有 WIP 提交）
- 处理代码中的 TODO：支付回调验签

阻塞
- 曾遇阻塞：fix: 修复崩溃（已在本次提交中处理）
- 当前阻塞：等后端联调接口
```

## 统计规则

- 默认统计起点：**上个工作日 09:00（本地时间）**；周一会自动回看到上周五 09:00。
- `--since` 直接透传给 `git --since`，支持 `"2 days ago"`、`"2026-10-01"` 等写法。
- `--author me` 按 `git config user.email` 过滤。

## 今天计划是怎么来的（诚实说明）

"今天计划"是**推测**，依据按优先级是：

1. 标题含 WIP 的提交（没做完的事，大概率今天继续）；
2. 当前分支名（如 `feature/pay`）；
3. diff 新增行里的 `TODO` / `FIXME`；
4. 未提交的改动文件。

实在没有依据时，模型会被要求写"继续推进当前分支工作"。输出标题里明确标了"（推测）"，别直接当承诺发出去——过一眼再贴。

## 阻塞是怎么来的

- 提交信息里含 `fix` / `hotfix` / `revert` 的提交 → 记为"曾遇阻塞（已在本次提交中处理）"；
- `--blocker "文本"` 手动传入的 → 记为"当前阻塞"。

## 安全

- API Key 只从 `OPENAI_API_KEY` 环境变量读，只放在 HTTP header 里，**永不打印到输出或日志**；
- `--dry-run` 模式完全不发起网络请求，适合先检查 prompt 里有没有敏感提交信息。

## 已知局限

- "今天计划"是推测，不是承诺；标题已标注，发出前请人工过一眼。
- squash merge 的仓库里 `git log` 可能看不到分支上的细粒度提交，日报名单会变粗。
- 变基 / 改写历史后，`--since` 窗口可能抓到重复提交。
- 模型偶尔不按 `DONE:` / `PLAN:` 格式输出，程序有兜底解析，但极端情况下分段可能错位。

## License

MIT，见 [LICENSE](LICENSE)。
