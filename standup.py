#!/usr/bin/env python3
"""standup — 从 git 历史生成每日站会日报的小工具。

在 git 仓库内运行，收集上个工作日以来的提交，用一次 LLM 调用
生成「昨天做了什么 / 今天计划 / 阻塞」三段式日报。

只依赖 Python 标准库。API Key 从环境变量 OPENAI_API_KEY 读取，
兼容任何 OpenAI-compatible 的 /chat/completions 接口。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

VERSION = "0.1.0"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT = 60


class StandupError(Exception):
    """可直接展示给用户的错误。"""


# ---------------------------------------------------------------- git 读取

def run_git(args, cwd, check=True):
    p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise StandupError("git 命令执行失败：git " + " ".join(args) + "\n" + p.stderr.strip())
    return p


def ensure_git_repo(cwd):
    p = run_git(["rev-parse", "--is-inside-work-tree"], cwd, check=False)
    if p.returncode != 0 or p.stdout.strip() != "true":
        raise StandupError("当前目录不是 git 仓库，请在 git 仓库内运行 standup。")


def default_since():
    """默认统计起点：上个工作日 09:00（本地时间）。周一则回看到上周五。"""
    now = datetime.now().astimezone()
    days_back = 3 if now.weekday() == 0 else 1
    dt = (now - timedelta(days=days_back)).replace(hour=9, minute=0, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")


def git_user_email(cwd):
    p = run_git(["config", "user.email"], cwd, check=False)
    return p.stdout.strip()


def collect_commits(cwd, since, author_email=None):
    fmt = "%H%x1f%an%x1f%ae%x1f%ad%x1f%s%x1f%b%x1e"
    args = ["log", "--since=" + since, "--no-merges", "--date=iso-strict",
            "--pretty=format:" + fmt]
    if author_email:
        args.append("--author=" + author_email)
    out = run_git(args, cwd).stdout
    commits = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        parts = rec.split("\x1f")
        if len(parts) < 6:
            continue
        sha, an, ae, ad, subject = parts[0], parts[1], parts[2], parts[3], parts[4]
        body = "\x1f".join(parts[5:]).strip()
        commits.append({"sha": sha, "short": sha[:7], "author": an,
                        "email": ae, "date": ad, "subject": subject.strip(),
                        "body": body})
    return commits


TODO_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b\s*:?\s*(.{0,100})", re.IGNORECASE)

BLOCKER_RE = re.compile(r"\b(fix|hotfix|revert)\b", re.IGNORECASE)


def todos_in_patch(cwd, sha):
    """从某次提交的 diff 新增行里提取 TODO/FIXME 等。"""
    p = run_git(["show", sha, "--format=", "--unified=0"], cwd)
    found = []
    for line in p.stdout.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            m = TODO_RE.search(line[1:])
            if m:
                found.append(m.group(0).strip()[:120])
                if len(found) >= 5:
                    break
    return found


def repo_context(cwd):
    """分支名 + 未提交改动，用于推测今天计划。"""
    p = run_git(["branch", "--show-current"], cwd, check=False)
    branch = p.stdout.strip() or "(detached HEAD)"
    p = run_git(["status", "--porcelain"], cwd, check=False)
    dirty = [line.strip() for line in p.stdout.splitlines() if line.strip()]
    return {"branch": branch, "dirty": dirty[:20]}


# ---------------------------------------------------------------- prompt

def build_prompt(commits, ctx, todos, lang):
    lines = []
    for c in commits:
        lines.append("- [%s] %s %s：%s" % (c["short"], c["author"], c["date"][:16], c["subject"]))
        if c["body"]:
            first = c["body"].splitlines()[0][:200]
            lines.append("    " + first)
    commit_block = "\n".join(lines)

    extra = []
    wip = [c["subject"] for c in commits if "wip" in c["subject"].lower()]
    if wip:
        extra.append("进行中的提交（WIP）：" + "；".join(wip))
    extra.append("当前分支：" + ctx["branch"])
    if ctx["dirty"]:
        extra.append("未提交的改动文件：" + "、".join(f.split()[-1] for f in ctx["dirty"][:10]))
    if todos:
        extra.append("代码中的 TODO：\n" + "\n".join("- " + t for t in todos[:10]))
    extra_block = "\n".join(extra)

    if lang == "en":
        return (
            "You are a senior engineer's assistant. Based on the git history below, "
            "draft a daily standup.\n\n"
            "Commits since last workday:\n" + commit_block + "\n\n"
            "Context for inferring today's plan:\n" + extra_block + "\n\n"
            "Output STRICTLY in this format, nothing else:\n"
            "DONE:\n"
            "- one concise bullet per item, verb-first, merge similar commits, max 5\n"
            "PLAN:\n"
            "- 2-3 bullets inferring today's work from WIP commits / branch name / "
            "TODOs / uncommitted files; if there is no basis, write "
            "\"Continue current branch work\"\n"
        )
    return (
        "你是一个资深工程师的助手。请根据以下 git 提交记录，起草今天的站会日报。\n\n"
        "上个工作日以来的提交：\n" + commit_block + "\n\n"
        "推测今天计划的依据：\n" + extra_block + "\n\n"
        "严格按以下格式输出，不要输出其他内容：\n"
        "DONE:\n"
        "- 每条一句话，中文，动词开头，合并同类提交，最多 5 条\n"
        "PLAN:\n"
        "- 2-3 条，根据 WIP 提交 / 分支名 / TODO / 未提交文件推测今天要做的事；"
        "若无依据写\"继续推进当前分支工作\"\n"
    )


def parse_sections(text):
    """解析模型输出的 DONE:/PLAN: 两段。"""
    done = []
    plan = []
    cur = None
    for raw in text.splitlines():
        s = raw.strip()
        up = s.upper()
        if up == "DONE:" or up.startswith("DONE:"):
            cur = done
            rest = s[5:].strip().lstrip("-").strip()
            if rest:
                done.append(rest)
            continue
        if up == "PLAN:" or up.startswith("PLAN:"):
            cur = plan
            rest = s[5:].strip().lstrip("-").strip()
            if rest:
                plan.append(rest)
            continue
        if cur is None or not s:
            continue
        if s[0] in ("-", "*", "•"):
            bullet = s[1:].strip()
        else:
            bullet = s
        if bullet:
            cur.append(bullet)
    return done, plan


# ---------------------------------------------------------------- LLM

def get_api_key():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise StandupError("未找到 API key。请先设置环境变量 OPENAI_API_KEY 后再运行。")
    return key


def call_llm(prompt, model, base_url, api_key):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model,
               "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.3}
    # key 只进 Authorization header，不打日志、不进 URL
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise StandupError("API 请求失败（HTTP %d）：%s" % (e.code, body))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise StandupError("网络请求失败：%s" % e)
    except json.JSONDecodeError:
        raise StandupError("API 返回的不是合法 JSON。")
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise StandupError("API 返回格式异常，缺少 choices[0].message.content。")
    if not isinstance(content, str) or not content.strip():
        raise StandupError("API 返回了空内容。")
    return content


# ---------------------------------------------------------------- 输出

def render(done, plan, blockers, lang, mode):
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    if mode == "json":
        return json.dumps({
            "date": today,
            "done": done,
            "plan": plan,
            "plan_inferred": True,
            "blockers": blockers,
        }, ensure_ascii=False, indent=2)

    if lang == "en":
        h_done, h_plan, h_block = "Yesterday", "Today (inferred)", "Blockers"
        none, title = "None", "Standup " + today
    else:
        h_done, h_plan, h_block = "昨天做了什么", "今天计划（推测）", "阻塞"
        none, title = "无", "【日报】" + today

    if mode == "slack":
        h_done = "*" + h_done + "*"
        h_plan = "*" + h_plan + "*"
        h_block = "*" + h_block + "*"
        title = "*" + title + "*"

    def section(items):
        return "\n".join("- " + i for i in items) if items else none

    return (title + "\n\n"
            + h_done + "\n" + section(done) + "\n\n"
            + h_plan + "\n" + section(plan) + "\n\n"
            + h_block + "\n" + section(blockers))


def copy_to_clipboard(text):
    for cmd, args in (("pbcopy", []),
                      ("xclip", ["-selection", "clipboard"]),
                      ("wl-copy", [])):
        if shutil.which(cmd):
            try:
                subprocess.run([cmd] + args, input=text.encode("utf-8"),
                               check=True, timeout=10)
                return True
            except (subprocess.SubprocessError, OSError):
                continue
    return False


# ---------------------------------------------------------------- 主流程

def build_parser():
    ap = argparse.ArgumentParser(
        prog="standup",
        description="从 git 历史生成每日站会日报（昨天 / 今天 / 阻塞）。")
    ap.add_argument("--since", default=None,
                    help="统计起点，传给 git --since，默认上个工作日 09:00（如 --since \"2 days ago\"）")
    ap.add_argument("--author", default=None,
                    help="按作者过滤，--author me 表示只取 git user.email 的提交")
    ap.add_argument("--blocker", action="append", default=[],
                    help="手动补充一条当前阻塞，可重复使用")
    ap.add_argument("--en", action="store_true", help="输出英文日报")
    ap.add_argument("--slack", action="store_true", help="输出 Slack mrkdwn 格式")
    ap.add_argument("--json", action="store_true", help="输出 JSON 格式")
    ap.add_argument("--dry-run", action="store_true",
                    help="只展示收集到的提交和 prompt，不调网络")
    ap.add_argument("--copy", action="store_true", help="把结果复制到剪贴板")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="模型名（默认 %s）" % DEFAULT_MODEL)
    ap.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL),
                    help="OpenAI-compatible 接口地址")
    ap.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    cwd = os.getcwd()
    lang = "en" if args.en else "zh"

    try:
        ensure_git_repo(cwd)

        since = args.since or default_since()

        author_email = None
        if args.author:
            if args.author == "me":
                author_email = git_user_email(cwd)
                if not author_email:
                    raise StandupError("读取 git user.email 失败，无法使用 --author me。"
                                       "请先用 git config user.email 设置。")
            else:
                author_email = args.author

        commits = collect_commits(cwd, since, author_email)
        ctx = repo_context(cwd)

        if not commits:
            hint = "（起点：" + since + ("，作者：" + author_email if author_email else "") + "）"
            if lang == "en":
                print("No new commits in this period - nothing to report. Enjoy the day!")
            else:
                print("该时间段内没有新的提交，不用写日报 " + hint)
            return 0

        # 从 diff 里找 TODO，供"今天计划"推测用（只看前 50 个提交，防爆）
        todos = []
        for c in commits[:50]:
            todos.extend(todos_in_patch(cwd, c["sha"]))
            if len(todos) >= 10:
                break

        prompt = build_prompt(commits, ctx, todos, lang)

        if args.dry_run:
            print("统计起点：" + since)
            if author_email:
                print("作者过滤：" + author_email)
            print("分支：" + ctx["branch"])
            print("")
            print("收集到 %d 个提交：" % len(commits))
            for c in commits:
                print("  %s %s：%s" % (c["short"], c["author"], c["subject"]))
            print("")
            print("---- PROMPT（发给模型的原文）----")
            print(prompt)
            return 0

        api_key = get_api_key()
        raw = call_llm(prompt, args.model, args.base_url, api_key)
        done, plan = parse_sections(raw)

        blockers = []
        for c in commits:
            if BLOCKER_RE.search(c["subject"]):
                if lang == "en":
                    blockers.append("Previously blocked: %s (already addressed in this commit)"
                                    % c["subject"])
                else:
                    blockers.append("曾遇阻塞：%s（已在本次提交中处理）" % c["subject"])
        for b in args.blocker:
            tag = "Current blocker" if lang == "en" else "当前阻塞"
            blockers.append("%s：%s" % (tag, b))

        if args.json:
            mode = "json"
        elif args.slack:
            mode = "slack"
        else:
            mode = "text"
        out = render(done, plan, blockers, lang, mode)
        print(out)

        if args.copy:
            if copy_to_clipboard(out):
                print("(已复制到剪贴板）" if lang == "zh" else "(Copied to clipboard)",
                      file=sys.stderr)
            else:
                print("警告：未找到 pbcopy / xclip / wl-copy，无法复制到剪贴板。",
                      file=sys.stderr)
        return 0

    except StandupError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
