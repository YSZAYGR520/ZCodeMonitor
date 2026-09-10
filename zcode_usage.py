# -*- coding: utf-8 -*-
"""
zcode_usage.py — 读取 ZCode (Z.ai / GLM coding runtime) 本地用量数据库并聚合统计。

数据源:  ~/.zcode/cli/db/db.sqlite  (SQLite)
关键表:
  - turn_usage  : 每一「轮」(一次用户消息 -> agent 完整回复) 的聚合
  - model_usage : 每一次 LLM 请求(含子 agent/thinking)的耗时与 token
  - tool_usage  : 每一次工具(命令/extension)调用
  - session     : 会话;task_type=interactive 为顶层任务,
                  task_type=subagent_child 为它的子智能体会话(parent_id 指向父)
"""
import os
import sqlite3

DEFAULT_DB = os.path.join(os.environ.get("USERPROFILE", ""), ".zcode", "cli", "db", "db.sqlite")


# ---------------------------------------------------------------------------
# 读取(只读直连,不整库拷贝——db 可高达数百 MB,拷贝代价不可接受)
#   注意: 库是 WAL 模式, mode=ro 连接在 Windows 上无 -shm 写权限, 当写入方
#   (ZCode CLI) checkpoint WAL 时读操作会永久挂死(实测)。改用普通连接 +
#   PRAGMA query_only 保住只读语义, 同时获得 WAL 并发读能力。
# ---------------------------------------------------------------------------
def open_usage_db(path=None):
    src = path or DEFAULT_DB
    if not os.path.exists(src):
        raise FileNotFoundError("找不到 ZCode 用量库: %s" % src)
    uri = "file:%s" % src.replace("\\", "/")
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.execute("PRAGMA query_only = 1")     # 连接级只读, 语义等同 mode=ro
    con.execute("PRAGMA busy_timeout = 5000")
    return con, None


# ---------------------------------------------------------------------------
# SQL 口径说明
#   轮 rounds  = turn_usage 行数
#   步 steps   = tool_usage 行数(每次工具执行 = 一步)
#   LLM 时长    = SUM(model_usage.duration_ms)
#   工具时长    = SUM(tool_usage.duration_ms)
#   首 token 平均 = AVG(model_usage.time_to_first_token_ms)  (completed)
#   tok/s      = (输入+输出)/ LLM 秒
#   缓存命中    = SUM(cache_read)/SUM(input_tokens)  (input_tokens 已含缓存读)
#   输入/输出 tok = SUM(input_tokens)/SUM(output_tokens)
# ---------------------------------------------------------------------------

def _scope_sql(scope, time_filter_sql=None, extra_where=""):
    """scope: 全部(默认)或一组 session_id 列表。返回 WHERE 子句与参数。"""
    where = ""
    params = []
    if scope:
        marks = ",".join("?" * len(scope))
        where = " session_id IN (%s)" % marks
        params = list(scope)
    if time_filter_sql:
        if where:
            where += " AND (" + time_filter_sql + ")"
        else:
            where = " (" + time_filter_sql + ")"
    if extra_where:
        where = (where + " AND " if where else "") + extra_where
    if where:
        where = " WHERE " + where
    return where, params


def summarize_model_usage(cur, scope=None, since_ms=None, completed_only=True):
    """在给定 scope(session_id 集合)与时间窗内聚合 LLM 用量。"""
    where, params = _scope_sql(scope, time_filter_sql=("started_at>=?" if since_ms else None),
                               extra_where=("status='completed'" if completed_only else ""))
    if since_ms is not None:
        params.append(since_ms)

    q = ("SELECT COUNT(*), "
         "COALESCE(SUM(duration_ms),0), COALESCE(AVG(time_to_first_token_ms),0), "
         "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
         "COALESCE(SUM(reasoning_tokens),0), "
         "COALESCE(SUM(cache_creation_input_tokens),0), "
         "COALESCE(SUM(cache_read_input_tokens),0), "
         "COALESCE(SUM(provider_total_tokens),0), COALESCE(SUM(computed_total_tokens),0) "
         "FROM model_usage" + where)
    r = cur.execute(q, params).fetchone()
    return {
        "model_calls": r[0], "llm_duration_ms": r[1], "avg_ttft_ms": r[2],
        "input_tokens": r[3], "output_tokens": r[4], "reasoning_tokens": r[5],
        "cache_creation_tokens": r[6], "cache_read_tokens": r[7],
        "provider_total_tokens": r[8], "computed_total_tokens": r[9],
    }


def summarize_tool_usage(cur, scope=None, since_ms=None):
    where, params = _scope_sql(scope, time_filter_sql=("started_at>=?" if since_ms else None))
    if since_ms is not None:
        params.append(since_ms)
    q = ("SELECT COUNT(*), COALESCE(SUM(duration_ms),0), "
         "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0) "
         "FROM tool_usage" + where)
    r = cur.execute(q, params).fetchone()
    return {"tool_calls": r[0], "tool_duration_ms": r[1], "tool_errors": r[2]}


def summarize_turns(cur, scope=None, since_ms=None):
    where, params = _scope_sql(scope, time_filter_sql=("started_at>=?" if since_ms else None))
    if since_ms is not None:
        params.append(since_ms)
    r = cur.execute("SELECT COUNT(*) FROM turn_usage" + where, params).fetchone()
    return {"turns": r[0]}


# ---------------------------------------------------------------------------
# 派生指标(轮/步/命中率/tok/s/费用 相关)
# ---------------------------------------------------------------------------
def _derive(stat):
    stat = dict(stat)
    stat["rounds"] = stat["turns"]
    stat["steps"] = stat["tool_calls"]
    llm_s = stat["llm_duration_ms"] / 1000.0
    stat["llm_seconds"] = llm_s
    stat["tool_seconds"] = stat["tool_duration_ms"] / 1000.0
    in_tok = stat["input_tokens"]
    cache_read = stat["cache_read_tokens"]
    cache_creation = stat["cache_creation_tokens"]
    stat["fresh_input_tokens"] = max(in_tok - cache_read - cache_creation, 0)
    stat["cache_hit_rate"] = (cache_read / in_tok) if in_tok else 0.0
    total_tok = stat["input_tokens"] + stat["output_tokens"]
    stat["throughput_tok_per_s"] = (total_tok / llm_s) if llm_s > 0 else 0.0
    stat["avg_ttft_s"] = stat["avg_ttft_ms"] / 1000.0
    return stat


# ---------------------------------------------------------------------------
# 计算汇总字典(所有指标)
# ---------------------------------------------------------------------------
def compute(scope=None, since_ms=None, db_path=None):
    con, tmp = open_usage_db(db_path)
    try:
        cur = con.cursor()
        m = summarize_model_usage(cur, scope, since_ms)
        t = summarize_tool_usage(cur, scope, since_ms)
        tn = summarize_turns(cur, scope, since_ms)
    finally:
        con.close()
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass

    stat = {}
    stat.update(m)
    stat.update(t)
    stat.update(tn)
    return _derive(stat)


# ---------------------------------------------------------------------------
# 一次读库,批量算出多个顶层任务(含子树)的汇总——避免逐会话各复制一次大库
# ---------------------------------------------------------------------------
def _bulk_rows(cur, since_ms=None):
    """返回按 session_id 聚合的原始计数/汇总字典。SQL 全为静态字面量 + ? 参数绑定。"""
    agg = {}

    # model_usage (仅 completed)
    if since_ms is None:
        rows = cur.execute(
            "SELECT session_id, COUNT(*), "
            "COALESCE(SUM(duration_ms),0), COALESCE(AVG(time_to_first_token_ms),0), "
            "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(reasoning_tokens),0), COALESCE(SUM(cache_creation_input_tokens),0), "
            "COALESCE(SUM(cache_read_input_tokens),0), "
            "COALESCE(SUM(provider_total_tokens),0), COALESCE(SUM(computed_total_tokens),0) "
            "FROM model_usage WHERE status='completed' GROUP BY session_id").fetchall()
    else:
        rows = cur.execute(
            "SELECT session_id, COUNT(*), "
            "COALESCE(SUM(duration_ms),0), COALESCE(AVG(time_to_first_token_ms),0), "
            "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(reasoning_tokens),0), COALESCE(SUM(cache_creation_input_tokens),0), "
            "COALESCE(SUM(cache_read_input_tokens),0), "
            "COALESCE(SUM(provider_total_tokens),0), COALESCE(SUM(computed_total_tokens),0) "
            "FROM model_usage WHERE status='completed' AND started_at>=? GROUP BY session_id",
            [since_ms]).fetchall()
    for (sid, n, llm, ttft, inp, outp, reas, cc, cr, pt, ct) in rows:
        a = agg.setdefault(sid, {"model_calls": 0, "tool_calls": 0, "tool_duration_ms": 0,
                                 "tool_errors": 0, "turns": 0,
                                 "llm_duration_ms": 0, "avg_ttft_ms": 0.0,
                                 "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                                 "cache_creation_tokens": 0, "cache_read_tokens": 0,
                                 "provider_total_tokens": 0, "computed_total_tokens": 0})
        a["model_calls"] += n
        a["llm_duration_ms"] += llm
        a["input_tokens"] += inp
        a["output_tokens"] += outp
        a["reasoning_tokens"] += reas
        a["cache_creation_tokens"] += cc
        a["cache_read_tokens"] += cr
        a["provider_total_tokens"] += pt
        a["computed_total_tokens"] += ct
        a["avg_ttft_ms"] = ttft  # 简单近似:以该会话最新分组为准(不追求严格加权)

    # tool_usage
    if since_ms is None:
        rows = cur.execute(
            "SELECT session_id, COUNT(*), COALESCE(SUM(duration_ms),0), "
            "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0) "
            "FROM tool_usage GROUP BY session_id").fetchall()
    else:
        rows = cur.execute(
            "SELECT session_id, COUNT(*), COALESCE(SUM(duration_ms),0), "
            "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0) "
            "FROM tool_usage WHERE started_at>=? GROUP BY session_id",
            [since_ms]).fetchall()
    for (sid, n, dur, err) in rows:
        a = agg.setdefault(sid, {"model_calls": 0, "tool_calls": 0, "tool_duration_ms": 0,
                                 "tool_errors": 0, "turns": 0,
                                 "llm_duration_ms": 0, "avg_ttft_ms": 0.0,
                                 "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                                 "cache_creation_tokens": 0, "cache_read_tokens": 0,
                                 "provider_total_tokens": 0, "computed_total_tokens": 0})
        a["tool_calls"] += n
        a["tool_duration_ms"] += dur
        a["tool_errors"] += err

    # turn_usage
    if since_ms is None:
        rows = cur.execute(
            "SELECT session_id, COUNT(*) FROM turn_usage GROUP BY session_id").fetchall()
    else:
        rows = cur.execute(
            "SELECT session_id, COUNT(*) FROM turn_usage "
            "WHERE started_at>=? GROUP BY session_id", [since_ms]).fetchall()
    for (sid, n) in rows:
        a = agg.setdefault(sid, {"model_calls": 0, "tool_calls": 0, "tool_duration_ms": 0,
                                 "tool_errors": 0, "turns": 0,
                                 "llm_duration_ms": 0, "avg_ttft_ms": 0.0,
                                 "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                                 "cache_creation_tokens": 0, "cache_read_tokens": 0,
                                 "provider_total_tokens": 0, "computed_total_tokens": 0})
        a["turns"] += n

    return agg


def compute_many_trees(root_sessions, sessions, since_ms=None, db_path=None):
    """对每个 root_sessions(顶层任务 dict)算出含子树的 summary,一次开库。"""
    # 预建 parent->children 索引(避免对每个 root 全表扫描)
    children_of = {}
    for s in sessions.values():
        p = s.get("parent_id")
        if p is not None:
            children_of.setdefault(p, []).append(s["id"])

    con, tmp = open_usage_db(db_path)
    try:
        cur = con.cursor()
        agg = _bulk_rows(cur, since_ms)
    finally:
        con.close()
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass

    out = {}
    for r in root_sessions:
        st = {"model_calls": 0, "tool_calls": 0, "tool_duration_ms": 0, "tool_errors": 0,
              "turns": 0, "llm_duration_ms": 0, "avg_ttft_ms": 0.0,
              "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
              "cache_creation_tokens": 0, "cache_read_tokens": 0,
              "provider_total_tokens": 0, "computed_total_tokens": 0}
        for s in iter_tree(children_of, r["id"]):
            a = agg.get(s)
            if not a:
                continue
            for k in ("model_calls", "tool_calls", "tool_duration_ms", "tool_errors", "turns",
                      "llm_duration_ms", "input_tokens", "output_tokens", "reasoning_tokens",
                      "cache_creation_tokens", "cache_read_tokens",
                      "provider_total_tokens", "computed_total_tokens"):
                st[k] += a[k]
            st["avg_ttft_ms"] = a["avg_ttft_ms"] or st["avg_ttft_ms"]
        out[r["id"]] = _derive(st)
    return out


# ---------------------------------------------------------------------------
# 会话层次:顶层 interactive 任务 + 其全部后代子智能体会话
# ---------------------------------------------------------------------------
def load_sessions(db_path=None):
    con, tmp = open_usage_db(db_path)
    try:
        cur = con.cursor()
        rows = cur.execute(
            "SELECT id, parent_id, task_type, title, time_created, time_updated, directory, project_id "
            "FROM session").fetchall()
    finally:
        con.close()
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    s = {}
    for (sid, parent, ttype, title, tc, tu, directory, project_id) in rows:
        s[sid] = {"id": sid, "parent_id": parent, "task_type": ttype,
                  "title": title, "time_created": tc, "time_updated": tu,
                  "directory": directory, "project_id": project_id}
    return s


def root_session_id(sessions, sid):
    """沿 parent_id 上溯到顶层会话 id。"""
    seen = set()
    while sid in sessions and sessions[sid]["parent_id"] is not None:
        if sid in seen:
            break
        seen.add(sid)
        sid = sessions[sid]["parent_id"]
    return sid


def collect_tree(sessions, root_id):
    """返回 root_id 及其所有后代会话 id(兼容旧接口,仍为全表扫描)。"""
    out = []
    stack = [root_id]
    while stack:
        cid = stack.pop()
        if cid in out:
            continue
        out.append(cid)
        for s in sessions.values():
            if s["parent_id"] == cid and s["id"] not in out:
                stack.append(s["id"])
    return out


def iter_tree(children_of, root_id):
    """基于 parent->children 索引遍历 root_id 及其后代会话 id(避免 O(n²))。"""
    out = []
    stack = [root_id]
    while stack:
        cid = stack.pop()
        if cid in out:
            continue
        out.append(cid)
        for ch in children_of.get(cid, ()):
            if ch not in out:
                stack.append(ch)
    return out


def list_root_sessions(sessions, sort_key="time_updated", limit=None):
    """顶层任务列表(task_type 不以 subagent_child 开头或 parent 为根)。"""
    roots = [s for s in sessions.values() if s["task_type"] != "subagent_child"]
    roots.sort(key=lambda s: (s[sort_key] or 0) if sort_key in s else (s["time_updated"] or 0),
               reverse=True)
    if limit:
        roots = roots[:limit]
    return roots


def now_ms():
    import time
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# 费用估算(读用户可编辑的单价表 pricing.json,单位 $/百万 token)
#  schema: { "currency": "USD", "models": { "<model_id>": {
#             "input": .., "output": .., "cache_read": .., "cache_write": .. } } }
# 未配置/价格为 0 的模型按 0 计,并标记「未配置单价」。
# ---------------------------------------------------------------------------
def default_pricing():
    return {
        "currency": "USD",
        "_note": "单位:$/1M tokens。GLM Coding Plan 订阅内增量约 0,请按你的套餐/中转实际价格修改。"
                "input=未命中缓存的新输入,output=输出,cache_read=命中缓存读,cache_write=缓存写入。",
        "models": {},
    }


def load_pricing(path=None):
    import json as _json
    default = default_pricing()
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
            models = data.get("models", default["models"]) if isinstance(data, dict) else default["models"]
            return {"currency": data.get("currency", default["currency"]) if isinstance(data, dict) else default["currency"],
                    "models": models}
        except Exception:
            pass
    return default


def _model_price(pricing, model_id):
    models = pricing.get("models", {})
    m = models.get(model_id) or models.get(model_id.lower())
    if not m:
        return None
    return {
        "input": float(m.get("input", 0) or 0),
        "output": float(m.get("output", 0) or 0),
        "cache_read": float(m.get("cache_read", 0) or 0),
        "cache_write": float(m.get("cache_write", 0) or 0),
    }


def estimate_cost(calls, pricing):
    """calls: list of dicts {model_id, input_tokens, cache_read, cache_creation, output_tokens}.
    返回 (total_usd, 未配置单价模型集合). fresh 输入 = input - cache_read - cache_creation。"""
    total = 0.0
    missing = set()
    for c in calls:
        p = _model_price(pricing, c["model_id"])
        if p is None:
            missing.add(c["model_id"])
            continue
        fresh = max(c["input_tokens"] - (c["cache_read"] or 0) - (c["cache_creation"] or 0), 0)
        total += fresh / 1e6 * p["input"]
        total += (c["cache_read"] or 0) / 1e6 * p["cache_read"]
        total += (c["cache_creation"] or 0) / 1e6 * p["cache_write"]
        total += c["output_tokens"] / 1e6 * p["output"]
    return total, missing


def load_model_calls(cur, scope=None, since_ms=None):
    """返回每组聚合的每模型 token/耗时行(仅 completed)。用于费用与分组。"""
    where, params = _scope_sql(scope, time_filter_sql=("started_at>=?" if since_ms else None),
                               extra_where="status='completed'")
    if since_ms is not None:
        params.append(since_ms)
    q = ("SELECT model_id, provider_id, session_id, "
         "COUNT(*), "
         "COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read_input_tokens),0), "
         "COALESCE(SUM(cache_creation_input_tokens),0), COALESCE(SUM(output_tokens),0), "
         "COALESCE(SUM(reasoning_tokens),0), COALESCE(SUM(duration_ms),0) "
         "FROM model_usage" + where +
         " GROUP BY model_id, provider_id, session_id")
    rows = cur.execute(q, params).fetchall()
    cols = ["model_id", "provider_id", "session_id", "calls",
            "input_tokens", "cache_read", "cache_creation", "output_tokens",
            "reasoning_tokens", "duration_ms"]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# 按模型 / 按项目(会话目录)分组
# ---------------------------------------------------------------------------
def group_by_model(rows):
    """rows 来自 load_model_calls。返回 {model_id: 聚合}。"""
    out = {}
    for r in rows:
        m = r["model_id"] or "(未知)"
        g = out.setdefault(m, {"calls": 0, "input_tokens": 0, "cache_read": 0,
                               "cache_creation": 0, "output_tokens": 0,
                               "reasoning_tokens": 0, "duration_ms": 0,
                               "providers": set()})
        g["calls"] += r["calls"]
        g["input_tokens"] += r["input_tokens"]
        g["cache_read"] += r["cache_read"]
        g["cache_creation"] += r["cache_creation"]
        g["output_tokens"] += r["output_tokens"]
        g["reasoning_tokens"] += r["reasoning_tokens"]
        g["duration_ms"] += r["duration_ms"]
        if r["provider_id"]:
            g["providers"].add(r["provider_id"])
    return out


def session_directory(sessions, sid):
    """取某会话所属项目目录:优先自身,空则沿父链找。"""
    seen = set()
    cur_sid = sid
    while cur_sid in sessions:
        if cur_sid in seen:
            break
        seen.add(cur_sid)
        if sessions[cur_sid].get("directory"):
            return sessions[cur_sid]["directory"]
        cur_sid = sessions[cur_sid].get("parent_id")
    return None


def group_by_project(rows, sessions):
    """按根目录聚合(子智能体请求归属到其根项目目录)。"""
    out = {}
    for r in rows:
        d = session_directory(sessions, r["session_id"])
        key = d or "(未知项目)"
        g = out.setdefault(key, {"calls": 0, "input_tokens": 0, "cache_read": 0,
                                 "cache_creation": 0, "output_tokens": 0,
                                 "reasoning_tokens": 0, "duration_ms": 0})
        g["calls"] += r["calls"]
        g["input_tokens"] += r["input_tokens"]
        g["cache_read"] += r["cache_read"]
        g["cache_creation"] += r["cache_creation"]
        g["output_tokens"] += r["output_tokens"]
        g["reasoning_tokens"] += r["reasoning_tokens"]
        g["duration_ms"] += r["duration_ms"]
    return out


# ---------------------------------------------------------------------------
# 请求级分布统计 —— 口径对齐社区 zcode-monitor 项目(server/db.js)
#   每一行 model_usage(completed) = 一次 LLM 请求,作为一个样本:
#     生成速度 tok/s = (output_tokens + reasoning_tokens) / (duration_ms/1000)
#                      —— 思考token计入产出;分母为总耗时(含首token等待)
#     加权速度 weighted_tps = Σ(output+reasoning) / Σduration  (token加权,非逐请求平均)
#     首次token(秒) = time_to_first_token_ms / 1000
#     请求耗时(秒)   = duration_ms / 1000
#     单次输出(含思考) = output_tokens + reasoning_tokens
#     单次输入        = input_tokens (含缓存读)
#     算力(total_tokens) = Σ computed_total_tokens (缺省回退 in+out+reasoning)
# ---------------------------------------------------------------------------
def load_request_rows(cur, since_ms=None, session_ids=None):
    """逐请求明细(仅 completed)。SQL 为静态字面量 + ? 参数绑定;
    session_ids 非空时在 Python 侧过滤(悬浮窗等全局场景用不到)。"""
    if since_ms is None:
        rows = cur.execute(
            "SELECT session_id, model_id, query_source, duration_ms, "
            "time_to_first_token_ms, input_tokens, output_tokens, "
            "reasoning_tokens, computed_total_tokens "
            "FROM model_usage WHERE status='completed'").fetchall()
    else:
        rows = cur.execute(
            "SELECT session_id, model_id, query_source, duration_ms, "
            "time_to_first_token_ms, input_tokens, output_tokens, "
            "reasoning_tokens, computed_total_tokens "
            "FROM model_usage WHERE status='completed' AND started_at>=?",
            [since_ms]).fetchall()
    if session_ids:
        allowed = set(session_ids)
        rows = [r for r in rows if r[0] in allowed]
    return rows


def dist_stats(values):
    """values -> {avg, median, p90, min, max, count};P90 用线性插值。空返回 None。"""
    vs = sorted(values)
    n = len(vs)
    if n == 0:
        return None

    def pct(p):
        k = (n - 1) * p
        f = int(k)
        c = min(f + 1, n - 1)
        return vs[f] + (vs[c] - vs[f]) * (k - f)

    return {"avg": sum(vs) / float(n), "median": pct(0.5), "p90": pct(0.9),
            "min": vs[0], "max": vs[-1], "count": n}


def model_dist_panel(cur, since_ms=None, session_ids=None):
    """按模型聚合请求级分布 + 算力(total_tokens)与主/子任务来源计数。"""
    groups = {}
    for _sid, mid, qsrc, dur, ttft, inp, outp, reas, ctot in \
            load_request_rows(cur, since_ms, session_ids):
        g = groups.setdefault(mid, {
            "requests": 0, "speed": [], "ttft": [], "dur": [], "in": [], "out": [],
            "s_tok": 0.0, "s_ms": 0, "total_tokens": 0,
            "main_count": 0, "subagent_count": 0})
        reas = reas or 0
        out_all = (outp or 0) + reas
        secs = (dur or 0) / 1000.0
        g["requests"] += 1
        if (dur or 0) > 0:
            g["speed"].append(out_all / secs)
        if ttft:
            g["ttft"].append(ttft / 1000.0)
        g["dur"].append(secs)
        g["in"].append(inp or 0)
        g["out"].append(out_all)
        g["s_tok"] += out_all
        g["s_ms"] += (dur or 0)
        g["total_tokens"] += ctot if ctot else ((inp or 0) + out_all)
        if qsrc == "subagent":
            g["subagent_count"] += 1
        else:
            g["main_count"] += 1

    out = {}
    for mid, g in groups.items():
        s_ms_s = g["s_ms"] / 1000.0
        out[mid] = {
            "requests": g["requests"],
            "weighted_tps": (g["s_tok"] / s_ms_s) if s_ms_s > 0 else None,
            "speed_tok_s": dist_stats(g["speed"]),
            "ttft_s": dist_stats(g["ttft"]),
            "duration_s": dist_stats(g["dur"]),
            "output_tokens": dist_stats(g["out"]),
            "input_tokens": dist_stats(g["in"]),
            "total_tokens": g["total_tokens"],
            "main_count": g["main_count"],
            "subagent_count": g["subagent_count"],
        }
    return out


def compute_model_panels(since_ms=None, db_path=None, session_ids=None):
    """打开(只读)用量库计算模型分布面板;返回按有效请求数降序的有序 dict。"""
    con, tmp = open_usage_db(db_path)
    try:
        panel = model_dist_panel(con.cursor(), since_ms=since_ms, session_ids=session_ids)
    finally:
        con.close()
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    ordered = sorted(panel.items(), key=lambda kv: -kv[1]["requests"])
    out = {}
    for mid, v in ordered:
        out[mid] = v
    return out


def overview_kpis(since_ms=None, db_path=None):
    """总览 KPI: 请求/错误数、各类 token 合计、活跃会话数。静态参数化 SQL。"""
    con, tmp = open_usage_db(db_path)
    try:
        cur = con.cursor()
        if since_ms is None:
            m = cur.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                "COALESCE(SUM(reasoning_tokens),0), COALESCE(SUM(cache_read_input_tokens),0) "
                "FROM model_usage").fetchone()
            s = cur.execute("SELECT COUNT(DISTINCT session_id) FROM model_usage").fetchone()
        else:
            m = cur.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                "COALESCE(SUM(reasoning_tokens),0), COALESCE(SUM(cache_read_input_tokens),0) "
                "FROM model_usage WHERE started_at>=?",
                [since_ms]).fetchone()
            s = cur.execute(
                "SELECT COUNT(DISTINCT session_id) FROM model_usage WHERE started_at>=?",
                [since_ms]).fetchone()
    finally:
        con.close()
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return {
        "model_calls": m[0], "completed": m[1], "errors": m[2],
        "input_tokens": m[3], "output_tokens": m[4],
        "reasoning_tokens": m[5], "cache_read_tokens": m[6],
        "active_sessions": s[0],
    }


# ---------------------------------------------------------------------------
# 额度监控 —— 对齐 GLM Coding Plan「每5小时 + 每周」双窗口积分制(参考 ccusage blocks)
#   本地库不含积分字段,积分=请求数×credits_per_request 估算(官方 Pro≈600 prompts
#   =12000积分 → 20/请求),可在 monitor.json 调整;精确值请以 ZCode 设置页为准。
#   5h 区块划分: 相邻请求间隔 <5h 归同一区块,区块窗口=首请求起 5 小时(ccusage 口径)。
#   每周: 自然周(周一 00:00 本地时区)。
# ---------------------------------------------------------------------------
HOUR_MS = 3600 * 1000
BLOCK_MS = 5 * HOUR_MS

DEFAULT_PLANS = {
    "lite": {"block_credits": 2000, "week_credits": 10000},
    "pro": {"block_credits": 12000, "week_credits": 60000},
    "max": {"block_credits": 28000, "week_credits": 140000},
    "none": {"block_credits": 0, "week_credits": 0},
}
DEFAULT_CONTEXT_LIMITS = {
    "glm": 1000000,
    "deepseek": 128000,
    "kimi": 256000,
}


REG_BASE = r"Software\ZCodeMonitor"


def load_override():
    """读设置面板持久化的覆盖项(存注册表 HKCU\\Software\\ZCodeMonitor)。"""
    d = {}
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_BASE)
        try:
            for name in ("refresh_seconds", "warn_pct", "crit_pct"):
                try:
                    v, _ = winreg.QueryValueEx(k, name)
                    d[name] = int(v)
                except (FileNotFoundError, OSError, TypeError):
                    pass
            for name in ("theme", "modules"):
                try:
                    v, _ = winreg.QueryValueEx(k, name)
                    d[name] = v
                except (FileNotFoundError, OSError):
                    pass
        finally:
            k.Close()
    except Exception:
        pass
    return d


def save_override(updates):
    """设置面板写注册表(仅固定键;成功返回 True)。modules 传逗号分隔字符串。"""
    try:
        import winreg
        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG_BASE,
                               0, winreg.KEY_SET_VALUE)
        try:
            for name, v in updates.items():
                if v is None:
                    continue
                if name in ("refresh_seconds", "warn_pct", "crit_pct"):
                    winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, int(v))
                else:
                    winreg.SetValueEx(k, name, 0, winreg.REG_SZ, str(v))
        finally:
            k.Close()
        return True
    except Exception:
        return False


def monitor_config_path():
    """monitor.json 位置: 与 exe/脚本同目录(固定文件名,无用户输入)。"""
    import sys as _sys
    if getattr(_sys, "frozen", False):
        base = os.path.dirname(_sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(base, "monitor.json"))


def load_monitor_config(path=None):
    """读取 monitor.json;缺失/损坏时返回内置默认(pro 档、7积分/请求、10s刷新)。"""
    import json as _json
    cfg = {"plan": "pro", "credits_per_request": 7.0, "week_start": "monday",
           "refresh_seconds": 10, "warn_pct": 85, "crit_pct": 95,
           "theme": "system",
           "context_limits": dict(DEFAULT_CONTEXT_LIMITS)}
    if path is None:
        path = monitor_config_path()
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
            if isinstance(data, dict):
                cfg["plan"] = str(data.get("plan", cfg["plan"])).lower()
                cfg["credits_per_request"] = float(data.get("credits_per_request", 7.0) or 0)
                ws = str(data.get("week_start", "monday")).lower()
                cfg["week_start"] = ws if ws in ("monday", "rolling7d") else "monday"
                try:
                    rs = int(float(data.get("refresh_seconds", 10) or 10))
                except (TypeError, ValueError):
                    rs = 10
                cfg["refresh_seconds"] = max(2, min(3600, rs))
                for pk, dflt in (("warn_pct", 85), ("crit_pct", 95)):
                    try:
                        pv = int(float(data.get(pk, dflt) or dflt))
                    except (TypeError, ValueError):
                        pv = dflt
                    cfg[pk] = max(50, min(100, pv))
                th = str(data.get("theme", "system")).lower()
                cfg["theme"] = th if th in ("dark", "light", "system") else "system"
                # 显示模块(白名单,顺序即显示顺序;未知项忽略)
                known = ("kpi", "quota", "dist", "trend", "models")
                raw_mods = data.get("modules")
                if isinstance(raw_mods, list):
                    mods = [m for m in raw_mods if isinstance(m, str) and m in known]
                    seen = set()
                    uniq = []
                    for m in mods:
                        if m not in seen:
                            seen.add(m)
                            uniq.append(m)
                    if uniq:
                        cfg["modules"] = uniq
                if isinstance(data.get("context_limits"), dict):
                    cl = {}
                    for k, v in data["context_limits"].items():
                        try:
                            cl[str(k).lower()] = int(v)
                        except (TypeError, ValueError):
                            pass
                    if cl:
                        cfg["context_limits"] = cl
                # 允许直接覆盖档位数值
                for k in ("block_credits", "week_credits"):
                    if data.get(k):
                        cfg[k] = float(data[k])
        except Exception:
            pass
    plan = DEFAULT_PLANS.get(cfg["plan"], DEFAULT_PLANS["pro"])
    cfg["block_credits"] = float(cfg.get("block_credits", plan["block_credits"]) or 0)
    cfg["week_credits"] = float(cfg.get("week_credits", plan["week_credits"]) or 0)
    # 注册表覆盖项(设置面板保存的)优先于 monitor.json
    ov = load_override()
    if ov.get("refresh_seconds"):
        try:
            cfg["refresh_seconds"] = max(2, min(3600, int(ov["refresh_seconds"])))
        except (TypeError, ValueError):
            pass
    if ov.get("theme") in ("dark", "light", "system"):
        cfg["theme"] = ov["theme"]
    if isinstance(ov.get("modules"), str):
        mods = [m.strip() for m in ov["modules"].split(",") if m.strip()]
        known = ("kpi", "quota", "dist", "trend", "models")
        mods = [m for m in mods if m in known]
        if mods:
            cfg["modules"] = mods
    for pk in ("warn_pct", "crit_pct"):
        if ov.get(pk):
            try:
                cfg[pk] = max(50, min(100, int(ov[pk])))
            except (TypeError, ValueError):
                pass
    return cfg


# ---------------------------------------------------------------------------
# 官方额度接口 —— GET https://open.bigmodel.cn/api/monitor/usage/quota/limit
#   (智谱国内版;国际版为 api.z.ai。返回 level=套餐档位 + 两条 CREDIT_LIMIT:
#    按 nextResetTime 排序,早的是5小时、晚的是每周;TIME_LIMIT=MCP月度。)
#   认证用 Coding Plan 的 API Key,自动取自 ~/.zcode/cli/config.json 的
#   mcp.servers.zai-mcp-server.env.Z_AI_API_KEY。
#   仅 https + 域名白名单;60 秒内复用缓存,避免高频请求。
# ---------------------------------------------------------------------------
QUOTA_API_HOSTS = ("open.bigmodel.cn", "api.z.ai")
_official_quota_cache = {"ts": 0, "data": None}


def zcode_api_key():
    """从 ZCode 的 config.json 里找 Coding Plan API Key(找不到返回 None)。"""
    import json as _json
    config_path = os.path.abspath(os.path.expanduser("~/.zcode/cli/config.json"))
    if ".." in config_path:
        return None
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = _json.load(f)
        servers = (cfg.get("mcp") or {}).get("servers") or {}
        # 首选 zai-mcp-server 的 Z_AI_API_KEY;否则扫描所有 env 找 key 形态的值
        k = ((servers.get("zai-mcp-server") or {}).get("env") or {}).get("Z_AI_API_KEY")
        if k and "." in str(k) and len(str(k)) >= 32:
            return str(k)
        for s in servers.values():
            for v in ((s.get("env") or {}) or {}).values():
                v = str(v)
                if "." in v and 32 <= len(v) <= 128:
                    return v
    except Exception:
        pass
    return None


def fetch_official_quota(api_key=None, ttl_ms=60_000):
    """拉取官方真实额度(5h/每周/MCP + 档位)。失败返回 {"ok": False, "error": ...}。"""
    import json as _json
    import time as _time
    import urllib.parse as _up
    import urllib.request as _ur
    now = int(_time.time() * 1000)
    if _official_quota_cache["data"] and now - _official_quota_cache["ts"] < ttl_ms:
        return _official_quota_cache["data"]
    if not api_key:
        api_key = zcode_api_key()
    if not api_key:
        return {"ok": False, "error": "未在 ~/.zcode/cli/config.json 找到 API Key"}
    url = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
    # 安全校验:必须 https 且域名在白名单内,拒绝其它任意地址
    p = _up.urlparse(url)
    if p.scheme != "https" or p.hostname not in QUOTA_API_HOSTS:
        return {"ok": False, "error": "接口地址校验失败"}
    try:
        req = _ur.Request(url, headers={"Authorization": api_key,
                                        "Content-Type": "application/json"})
        with _ur.urlopen(req, timeout=3) as r:
            data = _json.loads(r.read().decode("utf-8"))
    except Exception as ex:
        return {"ok": False, "error": str(ex)[:120]}
    if not data.get("success") or not data.get("data"):
        return {"ok": False, "error": str(data.get("msg") or "接口返回异常")[:120]}
    limits = sorted([l for l in data["data"].get("limits", [])
                     if l.get("type") == "CREDIT_LIMIT"],
                    key=lambda l: l.get("nextResetTime") or 0)
    block = limits[0] if limits else {}
    week = limits[1] if len(limits) > 1 else {}
    mcp = next((l for l in data["data"].get("limits", [])
                if l.get("type") == "TIME_LIMIT"), None)
    out = {
        "ok": True, "level": data["data"].get("level"),
        "fetched_at": now,
        "block": {
            "used": block.get("currentValue"), "quota": block.get("usage"),
            "remaining": block.get("remaining"),
            "pct": (block.get("percentage") or 0) / 100.0,
            "reset_ms": block.get("nextResetTime"),
        },
        "week": {
            "used": week.get("currentValue"), "quota": week.get("usage"),
            "remaining": week.get("remaining"),
            "pct": (week.get("percentage") or 0) / 100.0,
            "reset_ms": week.get("nextResetTime"),
        },
        "mcp": ({"used": mcp.get("currentValue"), "quota": mcp.get("usage"),
                 "remaining": mcp.get("remaining"),
                 "pct": (mcp.get("percentage") or 0) / 100.0,
                 "reset_ms": mcp.get("nextResetTime")} if mcp else None),
    }
    _official_quota_cache["ts"] = now
    _official_quota_cache["data"] = out
    return out


_official_usage_cache = {}


def is_peak_now(now=None):
    """GLM Coding Plan 积分峰谷规则: 工作日 14:00-18:00 高峰全额抵扣,其余(含周末)5 折。"""
    import datetime as _dt
    n = _dt.datetime.fromtimestamp((now or now_ms()) / 1000.0)
    return n.weekday() < 5 and 14 <= n.hour < 18


def _aggregate_days(hours):
    """小时桶 -> 日桶(按 label 的日期部分聚合)。"""
    days = {}
    order = []
    for h in hours:
        d = str(h.get("label") or "")[:10]
        if not d:
            continue
        if d not in days:
            days[d] = {"label": d[5:].replace("-", "/"), "calls": 0, "tokens": 0}
            order.append(d)
        days[d]["calls"] += h.get("calls") or 0
        days[d]["tokens"] += h.get("tokens") or 0
    return [days[d] for d in order]


def fetch_official_usage(range_key="24h", ttl_ms=None):
    """官方用量序列。range_key: 24h(含 MCP 工具统计)/7d/30d(仅模型)。
    各 range 独立缓存(24h 5分钟,更长窗口 10 分钟)。"""
    import datetime as _dt
    import json as _json
    import time as _time
    import urllib.parse as _up
    import urllib.request as _ur
    now = int(_time.time() * 1000)
    if ttl_ms is None:
        ttl_ms = 300_000 if range_key == "24h" else 600_000
    c = _official_usage_cache.get(range_key)
    if c and c.get("data", {}).get("ok") and now - c["ts"] < ttl_ms:
        return c["data"]
    key = zcode_api_key()
    if not key:
        return {"ok": False, "error": "未在 ~/.zcode/cli/config.json 找到 API Key"}
    base = "https://open.bigmodel.cn/api/monitor/usage/"
    try:
        n = _dt.datetime.now()
        if range_key == "24h":
            win = {"startTime": (n - _dt.timedelta(days=1)).strftime("%Y-%m-%d %H:00:00"),
                   "endTime": n.strftime("%Y-%m-%d %H:59:59")}
        elif range_key == "7d":
            win = {"startTime": (n - _dt.timedelta(days=7)).strftime("%Y-%m-%d 00:00:00"),
                   "endTime": n.strftime("%Y-%m-%d %H:59:59")}
        else:
            win = {"startTime": (n - _dt.timedelta(days=30)).strftime("%Y-%m-%d 00:00:00"),
                   "endTime": n.strftime("%Y-%m-%d %H:59:59")}
        qs = _up.urlencode(win)
        qs_month = _up.urlencode({"startTime": n.strftime("%Y-%m-01 00:00:00"),
                                  "endTime": n.strftime("%Y-%m-%d %H:59:59")})
        headers = {"Authorization": key, "Accept-Language": "zh-CN,zh",
                   "Content-Type": "application/json"}

        def get(path, query):
            url = base + path + "?" + query
            p = _up.urlparse(url)
            if p.scheme != "https" or p.hostname not in QUOTA_API_HOSTS:
                raise ValueError("接口地址校验失败")
            last = None
            for _attempt in range(1):   # 不在进程内重试: 10s 定时刷新即是重试; 超时必须短, 否则悬浮窗启动假死
                try:
                    req = _ur.Request(url, headers=headers)
                    with _ur.urlopen(req, timeout=3) as r:
                        d = _json.loads(r.read().decode("utf-8"))
                    if d.get("code") not in (200, None) and not d.get("success"):
                        raise ValueError(str(d.get("msg") or "接口返回异常")[:80])
                    return d.get("data") or {}
                except Exception as ex:
                    last = ex
            raise last

        mu = get("model-usage", qs)
        xs = mu.get("x_time") or []
        calls = mu.get("modelCallCount") or []
        toks = mu.get("tokensUsage") or []
        hours = [{"label": x, "calls": (calls[i] if i < len(calls) else 0),
                  "tokens": (toks[i] if i < len(toks) else 0)} for i, x in enumerate(xs)]
        tot = mu.get("totalUsage") or {}
        out = {"ok": True, "range": range_key, "fetched_at": now,
               "hours": hours, "days": _aggregate_days(hours),
               "total_calls": tot.get("totalModelCallCount"),
               "total_tokens": tot.get("totalTokensUsage"),
               "model_summary": tot.get("modelSummaryList") or []}
        if range_key == "24h":
            tu = get("tool-usage", qs)
            tu_m = get("tool-usage", qs_month)   # 本月窗口: MCP 月度用量
            tt = tu.get("totalUsage") or {}
            ttm = tu_m.get("totalUsage") or {}
            out["mcp"] = {"search": tt.get("totalNetworkSearchCount") or 0,
                          "web_read": tt.get("totalWebReadMcpCount") or 0,
                          "zread": tt.get("totalZreadMcpCount") or 0}
            out["mcp_month"] = {"search": ttm.get("totalNetworkSearchCount") or 0,
                                "web_read": ttm.get("totalWebReadMcpCount") or 0,
                                "zread": ttm.get("totalZreadMcpCount") or 0,
                                "details": ttm.get("toolDetails") or []}
    except Exception as ex:
        out = {"ok": False, "error": str(ex)[:120]}
    if out.get("ok"):
        _official_usage_cache[range_key] = {"ts": now, "data": out}
    return out


def _week_start_ms(now=None, mode="monday"):
    """每周窗口起点: monday=自然周周一00:00; rolling7d=now-7天(滚动)。"""
    import datetime as _dt
    n = now if now is not None else now_ms()
    if mode == "rolling7d":
        return n - 7 * 24 * HOUR_MS
    d = _dt.datetime.fromtimestamp(n / 1000.0)
    mon = d - _dt.timedelta(days=d.weekday())
    mon = mon.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(mon.timestamp() * 1000)


def quota_snapshot(db_path=None, cfg=None, config_path=None):
    """额度快照: 5h 区块(倒计时/速率/外推) + 本周进度 + 最近上下文。"""
    if cfg is None:
        cfg = load_monitor_config(config_path)
    now = now_ms()
    con, tmp = open_usage_db(db_path)
    try:
        cur = con.cursor()
        # 24h 内逐请求(区块划分只需回看 24h)
        rows = cur.execute(
            "SELECT started_at, input_tokens, output_tokens, reasoning_tokens "
            "FROM model_usage WHERE status='completed' AND started_at>=? "
            "ORDER BY started_at ASC",
            [now - 24 * HOUR_MS]).fetchall()
        week = cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens+output_tokens"
            "+COALESCE(reasoning_tokens,0)),0) "
            "FROM model_usage WHERE status='completed' AND started_at>=?",
            [_week_start_ms(now, cfg.get("week_start", "monday"))]).fetchone()
        latest = cur.execute(
            "SELECT model_id, input_tokens FROM model_usage "
            "WHERE status='completed' ORDER BY started_at DESC LIMIT 1").fetchone()
        series = cur.execute(
            "SELECT (started_at/3600000)*3600000 AS bucket, "
            "COUNT(*), COALESCE(SUM(input_tokens+output_tokens"
            "+COALESCE(reasoning_tokens,0)),0) "
            "FROM model_usage WHERE started_at>=? "
            "GROUP BY bucket ORDER BY bucket ASC",
            [now - 24 * HOUR_MS]).fetchall()
    finally:
        con.close()
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass

    # ---- 5h 区块划分(相邻间隔 <5h 归同一区块) ----
    blocks = []
    curblk = []
    for t, inp, outp, reas in rows:
        if curblk and t - curblk[-1][0] >= BLOCK_MS:
            blocks.append(curblk)
            curblk = []
        curblk.append((t, (inp or 0) + (outp or 0) + (reas or 0)))
    if curblk:
        blocks.append(curblk)
    active = blocks[-1] if blocks else []

    cpr = cfg.get("credits_per_request", 20.0) or 0.0
    b_tok = sum(r[1] for r in active)
    b_req = len(active)
    b_start = active[0][0] if active else None
    b_end = (b_start + BLOCK_MS) if b_start is not None else None
    remaining_ms = max(0, (b_end - now)) if b_end is not None else 0
    elapsed_ms = max(now - b_start, 10 * 60 * 1000) if b_start is not None else 0
    est_cr = b_req * cpr
    quota_b = cfg.get("block_credits", 0) or 0
    burn_tok_h = b_tok / (elapsed_ms / HOUR_MS) if elapsed_ms else 0.0
    proj_cr = (est_cr / (elapsed_ms / HOUR_MS)) * 5.0 if elapsed_ms else 0.0  # 全5h外推

    # ---- 本周 ----
    w_req, w_tok = week[0], week[1] or 0
    est_wk = w_req * cpr
    quota_w = cfg.get("week_credits", 0) or 0

    # ---- 最近上下文 ----
    ctx = None
    if latest:
        mid, in_tok = latest[0], latest[1] or 0
        limit = None
        for k, v in (cfg.get("context_limits") or {}).items():
            if k in (mid or "").lower():
                limit = v
                break
        ctx = {"model": mid, "input_tokens": in_tok, "limit": limit,
               "pct": (in_tok / limit) if limit else None}

    # ---- 24h 逐小时序列(补零对齐 24 桶) ----
    by_bucket = {b: (c, tk) for b, c, tk in series}
    start_bucket = (now - 23 * HOUR_MS) // HOUR_MS * HOUR_MS
    trend = []
    for i in range(24):
        b = start_bucket + i * HOUR_MS
        c, tk = by_bucket.get(b, (0, 0))
        trend.append({"bucket": b, "calls": c, "tokens": tk})

    return {
        "now": now, "credits_per_request": cpr,
        "official": fetch_official_quota(),
        "official_usage": fetch_official_usage(),
        "block": {
            "start_ms": b_start, "end_ms": b_end, "remaining_ms": remaining_ms,
            "requests": b_req, "tokens": b_tok, "est_credits": est_cr,
            "quota": quota_b,
            "pct": (est_cr / quota_b) if quota_b else None,
            "burn_tok_h": burn_tok_h,
            "projected_credits": proj_cr,
            "projected_pct": (proj_cr / quota_b) if quota_b else None,
        },
        "week": {
            "requests": w_req, "tokens": w_tok, "est_credits": est_wk,
            "quota": quota_w,
            "pct": (est_wk / quota_w) if quota_w else None,
            "start_ms": _week_start_ms(now, cfg.get("week_start", "monday")),
        },
        "context": ctx,
        "trend24h": trend,
    }


# ---------------------------------------------------------------------------
# 格式化(中文,人类可读;含紧凑摘要行)
# ---------------------------------------------------------------------------
def fmt_duration(ms):
    """毫秒 -> 'X分Y秒' / 'X小时Y分' 风格"""
    s = ms / 1000.0
    if s < 60:
        return "%.0f秒" % s
    minutes = int(s // 60)
    sec = int(s % 60)
    if minutes < 60:
        return "%d分%d秒" % (minutes, sec)
    hours = int(minutes // 60)
    minutes = minutes % 60
    return "%d小时%d分" % (hours, minutes)


def fmt_tokens(n):
    """大数字 -> '203M' / '237K' / 原样"""
    if n >= 1e9:
        return "%.1fG" % (n / 1e9)
    if n >= 1e6:
        return "%.0fM" % (n / 1e6)
    if n >= 1e3:
        return "%.0fK" % (n / 1e3)
    return "%d" % n


def fmt_usd(usd):
    """美元费用显示:>=1 显示 2 位,<1 显示更多小数避免全 0。"""
    if usd >= 100:
        return "$%.0f" % usd
    if usd >= 1:
        return "$%.2f" % usd
    return "$%.4f" % usd


def summary_line(stat, indent=""):
    """生成类似「21轮 · 560步 | LLM 83分7秒 · 工具调用 20分9秒 | …」的紧凑行。"""
    parts = [
        "%d轮" % stat["rounds"],
        "%d步" % stat["steps"],
    ]
    parts2 = [
        "LLM %s" % fmt_duration(stat["llm_duration_ms"]),
        "工具调用 %s" % fmt_duration(stat["tool_duration_ms"]),
    ]
    parts3 = [
        "首token平均 %.1fs" % stat["avg_ttft_s"],
        "%.0f tok/s" % stat["throughput_tok_per_s"],
        "缓存命中 %d%%" % (stat["cache_hit_rate"] * 100),
        "输入 %s tok · 输出 %s tok" % (fmt_tokens(stat["input_tokens"]), fmt_tokens(stat["output_tokens"])),
    ]
    return indent + " · ".join(parts) + " | " + " · ".join(parts2) + " | " + " · ".join(parts3)
