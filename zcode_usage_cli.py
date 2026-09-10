# -*- coding: utf-8 -*-
"""zcode_usage_cli.py — ZCode 使用统计命令行入口.

用法:
  python zcode_usage_cli.py                    # 默认总览(最近任务 + 时间窗总计)
  python zcode_usage_cli.py --window 7d        # today | 7d | 30d | all
  python zcode_usage_cli.py --json             # 机器可读 JSON
  python zcode_usage_cli.py --session <id>     # 只看某个会话(含其子会话)
  python zcode_usage_cli.py --db <路径>         # 指定用量库(默认为 ~/.zcode/cli/db/db.sqlite)
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zcode_usage as zu


def local_midnight_ms():
    now = datetime.datetime.now()
    mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(mid.timestamp() * 1000)


def window_since(win):
    if not win or win == "all":
        return None
    now = zu.now_ms()
    if win == "today":
        return local_midnight_ms()
    days = {"7d": 7, "30d": 30}.get(win, 7)
    return now - days * 24 * 3600 * 1000


def date_str(ms):
    if not ms:
        return ""
    return datetime.datetime.fromtimestamp(ms / 1000.0).strftime("%m-%d %H:%M")


def truncate_title(t, n=34):
    if not t:
        return ""
    t = " ".join(str(t).split())
    return t if len(t) <= n else t[: n - 1] + "…"


def short_dir(d, n=34):
    if not d:
        return ""
    d = str(d)
    # 尽量保留目录尾段便于识别项目
    return d if len(d) <= n else "…" + d[-(n - 1):]


def group_to_rows(groups, pricing, is_project):
    """把 model 或 project 组字典转成可直接打印/JSON 的条目列表(带费用)。"""
    out = []
    for key, g in groups.items():
        entry = {
            "name": key, "calls": g["calls"],
            "input_tokens": g["input_tokens"], "output_tokens": g["output_tokens"],
            "cache_read": g["cache_read"], "cache_creation": g["cache_creation"],
            "reasoning_tokens": g["reasoning_tokens"], "duration_ms": g["duration_ms"],
        }
        # 每项费用:按整体单价的精确算法
        if is_project:
            entry["cost_usd"] = None
            entry["missing"] = []
        else:
            call = {"model_id": key, "input_tokens": g["input_tokens"],
                    "cache_read": g["cache_read"], "cache_creation": g["cache_creation"],
                    "output_tokens": g["output_tokens"]}
            usd, miss = zu.estimate_cost([call], pricing)
            entry["cost_usd"] = usd
            entry["missing"] = sorted(miss)
        out.append(entry)
    return out


def compute_group_cost(model_g, proj_g, pricing, sessions, db_path, since_ms, scope=None):
    """计算项目级费用(需逐模型精度):读回项目内各模型各会话行逐条算太慢,
    改用:项目下各 model 单价 * 该项目该模型的 tokens。为此需返回 per (project,model)。"""
    # 直接重查按 session+model 投影更精确;此处退化:项目级费用用 model 级分摊近似不可行。
    # 采用精确法:把 model_g 按 project 无法还原,故这里逐行重查 cost。
    con, tmp = zu.open_usage_db(db_path)
    try:
        cur = con.cursor()
        rows = zu.load_model_calls(cur, scope=scope, since_ms=since_ms)
    finally:
        con.close()
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    proj_cost = {}
    proj_missing = {}
    for r in rows:
        d = zu.session_directory(sessions, r["session_id"]) or "(未知项目)"
        usd, miss = zu.estimate_cost([{
            "model_id": r["model_id"], "input_tokens": r["input_tokens"],
            "cache_read": r["cache_read"], "cache_creation": r["cache_creation"],
            "output_tokens": r["output_tokens"]}], pricing)
        proj_cost[d] = proj_cost.get(d, 0.0) + usd
        if miss:
            proj_missing.setdefault(d, set()).update(miss)
    return proj_cost, proj_missing


def _collect_scope(sessions, only_session):
    if not only_session:
        return None
    if only_session not in sessions:
        sys.exit("未找到会话 id: %s" % only_session)
    return zu.collect_tree(sessions, only_session)


def _load_breakdown(sessions, db_path, since, scope):
    """读取一次(会话+模型)明细行,组装 model/project 组。"""
    con, tmp = zu.open_usage_db(db_path)
    try:
        cur = con.cursor()
        rows = zu.load_model_calls(cur, scope=scope, since_ms=since)
    finally:
        con.close()
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return rows


def _summary_dict(stat):
    return {
        "rounds": stat["rounds"], "steps": stat["steps"],
        "llm_duration_ms": stat["llm_duration_ms"],
        "tool_duration_ms": stat["tool_duration_ms"],
        "avg_ttft_s": round(stat["avg_ttft_s"], 2),
        "tok_per_s": round(stat["throughput_tok_per_s"], 2),
        "cache_hit_rate": round(stat["cache_hit_rate"], 4),
        "input_tokens": stat["input_tokens"],
        "output_tokens": stat["output_tokens"],
        "reasoning_tokens": stat["reasoning_tokens"],
        "fresh_input_tokens": stat["fresh_input_tokens"],
        "cache_read_tokens": stat["cache_read_tokens"],
        "cache_creation_tokens": stat["cache_creation_tokens"],
        "tool_errors": stat["tool_errors"],
        "model_calls": stat["model_calls"],
    }


def _price_group_rows(rows, sessions, pricing):
    """由明细行组装 model/project 两个聚合 list(含每项 cost_usd / missing)。"""
    from collections import OrderedDict
    model_g = OrderedDict()
    proj_g = OrderedDict()
    for r in rows:
        mid = r["model_id"] or "(未知)"
        d = zu.session_directory(sessions, r["session_id"]) or "(未知项目)"
        usd, miss = zu.estimate_cost([{
            "model_id": r["model_id"], "input_tokens": r["input_tokens"],
            "cache_read": r["cache_read"], "cache_creation": r["cache_creation"],
            "output_tokens": r["output_tokens"]}], pricing)

        for key, agg, tag in ((mid, model_g, False), (d, proj_g, True)):
            g = agg.setdefault(key, {"calls": 0, "input_tokens": 0, "cache_read": 0,
                                     "cache_creation": 0, "output_tokens": 0,
                                     "reasoning_tokens": 0, "duration_ms": 0,
                                     "cost_usd": 0.0, "missing": set()})
            g["calls"] += r["calls"]
            g["input_tokens"] += r["input_tokens"]
            g["cache_read"] += r["cache_read"]
            g["cache_creation"] += r["cache_creation"]
            g["output_tokens"] += r["output_tokens"]
            g["reasoning_tokens"] += r["reasoning_tokens"]
            g["duration_ms"] += r["duration_ms"]
            g["cost_usd"] += usd
            if miss:
                g["missing"].update(miss)

    def clean(g):
        out = []
        for k, v in g.items():
            v = dict(v)
            v["name"] = k
            v["missing"] = sorted(v["missing"])
            out.append(v)
        out.sort(key=lambda x: -x["input_tokens"])
        return out

    return clean(model_g), clean(proj_g)


def _render_group(title, items, show_cost, is_model):
    print("\n【%s】" % title)
    if not items:
        print("  (无)")
        return
    currency = "$"
    for it in items:
        tok = "%s/出%s" % (zu.fmt_tokens(it["input_tokens"]), zu.fmt_tokens(it["output_tokens"]))
        cost_part = ""
        if show_cost:
            if it["missing"]:
                cost_part = "  费用:未配置单价(%s)" % ",".join(it["missing"])
            else:
                cost_part = "  费用: %s" % zu.fmt_usd(it["cost_usd"])
        name = it["name"] if is_model else short_dir(it["name"])
        print("  %-32s 调用%5d · 输%s%s" % (name[:32], it["calls"], tok, cost_part))


def print_dashboard(db_path=None, window="all", limit=12, only_session=None,
                    as_json=False, show_model=False, show_project=False,
                    show_cost=False, pricing_path=None):
    sessions = zu.load_sessions(db_path)
    since = window_since(window)
    scope = _collect_scope(sessions, only_session)
    stat = zu.compute(scope=scope, since_ms=since, db_path=db_path)

    pricing = zu.load_pricing(pricing_path)
    models = projects = []
    rows = None
    if show_model or show_project or show_cost or as_json:
        rows = _load_breakdown(sessions, db_path, since, scope)
        models, projects = _price_group_rows(rows, sessions, pricing)

    # 全局费用
    total_cost = sum(m["cost_usd"] for m in models)
    all_missing = sorted({mm for m in models for mm in m["missing"]})

    if as_json:
        payload = {
            "window": window, "since_ms": since,
            "db": db_path or zu.DEFAULT_DB,
            "pricing_file": pricing_path or None,
            "currency": pricing.get("currency", "USD"),
            "summary": _summary_dict(stat),
            "total_cost_usd": round(total_cost, 6),
            "unpriced_models": all_missing,
            "models": [{k: (v if k != "missing" else list(v)) for k, v in m.items()} for m in models],
            "projects": [{k: (v if k != "missing" else list(v)) for k, v in p.items()} for p in projects],
        }
        if not only_session:
            payload["tasks"] = []
            for r in zu.list_root_sessions(sessions, limit=limit):
                sc = zu.collect_tree(sessions, r["id"])
                st = zu.compute(scope=sc, since_ms=since, db_path=db_path)
                payload["tasks"].append({
                    "session_id": r["id"], "title": r["title"],
                    "time_updated": r["time_updated"],
                    "summary": _summary_dict(st),
                })
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    # ---- 文本输出 ----
    if only_session:
        print("# 会话统计  %s" % only_session)
    else:
        win_label = {"today": "今日", "7d": "最近7天", "30d": "最近30天", "all": "全部时间"}.get(window, window)
        print("# ZCode 使用统计  [%s]" % win_label)
        print("库: %s" % (db_path or zu.DEFAULT_DB))
        if pricing_path:
            print("单价表: %s" % pricing_path)
        print("")

    if not only_session:
        total_label = {"today": "今日", "7d": "最近7天", "30d": "最近30天", "all": "累计"}.get(window, window)
        print("【%s 总计】" % total_label)
        print("  " + zu.summary_line(stat))
        if stat["model_calls"]:
            print("  明细: LLM 调用 %d 次 · 工具执行 %d 次(失败 %d) · 思考token %s · 缓存读 %s / 新建缓存 %s"
                  % (stat["model_calls"], stat["tool_calls"], stat["tool_errors"],
                     zu.fmt_tokens(stat["reasoning_tokens"]),
                     zu.fmt_tokens(stat["cache_read_tokens"]),
                     zu.fmt_tokens(stat["cache_creation_tokens"])))
        if show_cost:
            if all_missing:
                print("  估算费用: %s(未配置单价: %s)" % (zu.fmt_usd(total_cost), ",".join(all_missing)))
            else:
                print("  估算费用: %s" % zu.fmt_usd(total_cost))
        print("")

        print("【最近任务】 (每行 = 一次顶层任务,含其子智能体用量)")
        roots = zu.list_root_sessions(sessions, limit=limit)
        shown = 0
        for i, r in enumerate(roots, 1):
            sc = zu.collect_tree(sessions, r["id"])
            st = zu.compute(scope=sc, since_ms=since, db_path=db_path)
            if st["rounds"] == 0 and st["model_calls"] == 0:
                continue
            shown += 1
            print("%2d. %s  [%s]" % (shown, truncate_title(r["title"]), date_str(r["time_updated"])))
            print("    " + zu.summary_line(st))
        if not shown:
            print("  (暂无)")
    else:
        print(zu.summary_line(stat))
        if stat["model_calls"]:
            print("  LLM 调用 %d 次 · 工具执行 %d 次(失败 %d) · 缓存读 %s / 新建 %s"
                  % (stat["model_calls"], stat["tool_calls"], stat["tool_errors"],
                     zu.fmt_tokens(stat["cache_read_tokens"]),
                     zu.fmt_tokens(stat["cache_creation_tokens"])))
        if show_cost:
            if all_missing:
                print("  估算费用: %s(未配置单价: %s)" % (zu.fmt_usd(total_cost), ",".join(all_missing)))
            else:
                print("  估算费用: %s" % zu.fmt_usd(total_cost))

    # 分组明细
    if show_model:
        _render_group("按模型明细", models, show_cost, is_model=True)
    if show_project:
        _render_group("按项目(会话目录)明细", projects, show_cost, is_model=False)


def main(argv=None):
    # Windows 控制台默认可能是 GBK/cp936;统一改 UTF-8 输出
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        os.system("chcp 65001 >nul")
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    # 打包成 exe 后 __file__ 指向临时解压目录,这里改取可执行文件/脚本所在目录
    if getattr(sys, "frozen", False):
        here = os.path.dirname(os.path.abspath(sys.executable))
    ap = argparse.ArgumentParser(prog="zcode-usage", description="ZCode 使用统计(读取本地 db.sqlite)")
    ap.add_argument("--window", choices=["today", "7d", "30d", "all"], default="all")
    ap.add_argument("--session", help="只看某会话 id(含其子会话)")
    ap.add_argument("--limit", type=int, default=12, help="任务列表条数(默认12)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--by-model", action="store_true", dest="show_model", help="按模型明细")
    ap.add_argument("--by-project", action="store_true", dest="show_project", help="按项目(会话目录)明细")
    ap.add_argument("--cost", action="store_true", dest="show_cost", help="估算费用(需单价表)")
    ap.add_argument("--pricing", default=None,
                    help="单价表 JSON 路径(默认自动找工具目录/用户目录 pricing.json)")
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args(argv)
    # 默认单价表:命令行 > 工具目录/pricing.json > ~/.zcode-dashbord-pricing.json
    pricing_path = a.pricing
    if pricing_path is None:
        cand = [os.path.join(here, "pricing.json")]
        if os.path.exists(cand[0]):
            pricing_path = cand[0]
    print_dashboard(db_path=a.db, window=a.window, limit=a.limit,
                    only_session=a.session, as_json=a.as_json,
                    show_model=a.show_model, show_project=a.show_project,
                    show_cost=a.show_cost, pricing_path=pricing_path)


if __name__ == "__main__":
    main()
