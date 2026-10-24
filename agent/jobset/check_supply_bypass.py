# -*- coding: utf-8 -*-
"""图级回归：真实 pipe 图 + 真实运行时 override 序列 -> 枚举
「闸门 -> 开始战斗」的全部路径，检查是否每条都经过 清空卡牌+选取植物。

背景（2026-10-24 水皮实锤）：boss 关前必弹补给屏，补给终点
「神器补给_确定」pipe 原值 next=[开始战斗]；「神器没变但阵容变」时
若无条件还原原值，选卡整段被旁路 —— boss 槽位覆盖永远不进卡组。
修复见 runtime.py `_inject_artifact` need=False 分支（按 lineup_changed
分流）；单元级回归在 selfcheck §13「补给旁路」。

四个场景：
  A. boss 关 + 补给屏在 + 阵容变 + 神器没变   <- 水皮场景
  B. boss 关 + 补给屏在 + 阵容变 + 神器也变
  C. 普通关 + 补给屏不在 + 阵容变（换表）
  D. boss 关 + 补给屏在 + 阵容没变 + 神器没变（合法跳过，特性）

依赖：编辑器（127.0.0.1:5000）在线，用其当前作业集（需带 boss 槽位覆盖）。
用法：.\\.venv\\Scripts\\python.exe -X utf8 agent\\jobset\\check_supply_bypass.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.jobset import runtime as rt  # noqa: E402

# ---------- 1) 载入真实 pipe（剥 // 注释）----------
PIPE_DIR = ROOT / "assets" / "resource" / "pipeline" / "Endless_ref"
nodes = {}
for f in list(PIPE_DIR.glob("*.json")) + list(PIPE_DIR.glob("*/*.json")):
    raw = "\n".join(ln for ln in f.read_text(encoding="utf-8").splitlines()
                    if not ln.lstrip().startswith("//"))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"!! 解析失败 {f.name}: {e}")
        continue
    nodes.update(data)
print(f"pipe 节点数: {len(nodes)}")

BATTLE = "无尽挑战_选取植物_开始战斗"
CLEAR = "无尽挑战_识别开始战斗_清空卡牌"
PICK = "无尽挑战_选取植物"
SUPPLY = "无尽局内_补给"
GATE_C = "无尽挑战_跳转_变化"
GATE_U = "无尽挑战_跳转_未变"


class Ctx:
    def __init__(self): self.overrides = []
    def override_pipeline(self, patch): self.overrides.append(patch)
    def run_task(self, node): return True


class Argv:
    def __init__(self, p): self.custom_action_param = json.dumps(p, ensure_ascii=False)


def build_graph(overrides_seq):
    """pipe 静态图 + 依序应用 override（字段级合并）。"""
    g = {k: json.loads(json.dumps(v)) for k, v in nodes.items()}
    for patch in overrides_seq:
        for name, fields in patch.items():
            if name == "__run_task__":
                continue
            g.setdefault(name, {})
            g[name].update(fields)
    return g


def paths_to_battle(g, entry, supply_screen, max_paths=64):
    """DFS 枚举 entry -> BATTLE 的全部路径（禁环）。
    supply_screen=False 时，补给节点识别不中（屏不在），该候选剪掉。
    返回 (good_paths, bad_paths)。"""
    good, bad = [], []

    def hit(name, screen):
        nd = g.get(name)
        if nd is None or nd.get("enabled") is False:
            return False
        if name == SUPPLY and not screen:
            return False
        return True

    def dfs(name, path):
        if len(good) + len(bad) >= max_paths:
            return
        if name == BATTLE:
            ok = (CLEAR in path) and (PICK in path)
            (good if ok else bad).append(path + [name])
            return
        if name in path:  # 环（含 self-retry）：截断
            return
        nd = g.get(name)
        if nd is None:
            return  # 悬空节点（资源外），视为终止
        nxt = nd.get("next") or []
        if isinstance(nxt, str):
            nxt = [nxt]
        for n2 in nxt:
            if isinstance(n2, str) and n2.startswith("[JumpBack]"):
                n2 = n2[len("[JumpBack]"):]
            if not isinstance(n2, str):
                continue
            if not hit(n2, supply_screen):
                continue
            dfs(n2, path + [name])

    # 入口本身先过一遍 hit（闸门被 disable 时 = 无路径）
    if hit(entry, supply_screen):
        for n2 in (g.get(entry, {}).get("next") or []):
            if isinstance(n2, str) and n2.startswith("[JumpBack]"):
                n2 = n2[len("[JumpBack]"):]
            if isinstance(n2, str) and hit(n2, supply_screen):
                dfs(n2, [entry])
    return good, bad


def _load_test_job():
    """拉正在运行的编辑器（127.0.0.1:5000）里的当前作业集。
    拉不到就明确跳过（持久回归在 selfcheck §13，本脚本是图级深查工具，
    依赖编辑器提供带 v3 compiled 的真实作业集）。"""
    import urllib.request
    with urllib.request.urlopen(
            "http://127.0.0.1:5000/list_jobs", timeout=3) as r:
        code = json.loads(r.read().decode("utf-8"))["current"]
    with urllib.request.urlopen(
            f"http://127.0.0.1:5000/load_job?code={code}", timeout=3) as r:
        job = json.loads(r.read().decode("utf-8"))["job"]
    print(f"（用编辑器当前作业集 {code}：{job.get('name', '')}）")
    return job


def simulate_overrides(boss_lv, lineup_changed, artifact_changed):
    """驱动真实 runtime，收集一个 boss 关（boss_lv）开局前的全部 override。"""
    job = _load_test_job()
    dst = ROOT / "assets" / "resource" / "jobs" / "_sim_current.json"
    dst.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    try:
        rt._STATE.clear()
        rt.JobSetLoad().run(Ctx(), Argv({"作业集代码": "_sim_current",
                                          "起始关卡": boss_lv - 1, "重置": True}))
        js = rt._STATE["jobset"]
        t2 = js.pick_table(boss_lv)  # 覆盖 boss_lv 的表（须带 boss 槽覆盖）
        if t2.lineup_sig(True) == t2.lineup_sig(False):
            raise SystemExit(
                f"当前作业集在 L{boss_lv} 的表{t2.index + 1} 没有 boss 差异阵容"
                "（boss 名单 == 普通名单）—— 换一个带 boss 槽位覆盖的作业集再跑")

        # 摆状态：前一关普通阵容已生效
        rt._STATE["lineup_sig"] = t2.lineup_sig(False)
        rt._STATE["artifact_sig"] = t2.eff_artifact(False)  # None（该表没配神器）
        rt._STATE["plan_table_index"] = t2.index
        rt._STATE["table_index"] = t2.index
        rt._STATE["locked_level"] = boss_lv - 1
        rt._STATE["is_boss"] = False
        rt._STATE["is_boss_level"] = None
        rt._STATE["training"] = False

        if artifact_changed:
            # 临时给表2塞个 boss 神器（葫芦神器 + en），制造「神器也变」
            t2.boss_artifact = "葫芦神器"
            t2.boss_artifact_en = "artifact_calabash"
        if not lineup_changed:
            # 抹掉 boss 覆盖：boss 名单 == 普通名单
            t2.boss_plants = list(t2.plants)

        ctx = Ctx()
        rt.JobSetPlan().run(ctx, Argv({"训练模式": False, "计数": True}))
        return ctx.overrides
    finally:
        dst.unlink(missing_ok=True)


def report(tag, entry, supply_screen, overrides, expect_bypass=False):
    g = build_graph(overrides)
    good, bad = paths_to_battle(g, entry, supply_screen)
    print(f"\n### {tag}")
    print(f"  到「开始战斗」的路径: 经过选卡 {len(good)} 条 / 绕过选卡 {len(bad)} 条")
    for p in bad[:3]:
        print(("  ✗ 旁路: " if not expect_bypass else "  · 合法直跳: ") + " -> ".join(p))
    for p in good[:2]:
        print("  ✓ 正路: " + " -> ".join(p))
    if expect_bypass:
        # 阵容没变：合法直跳是特性；若反而全走选卡 = 跳过失效（浪费一关时间）
        return 0 if len(bad) > 0 else 1
    return len(bad)


total_bad = 0
# A: 水皮场景（boss + 补给屏 + 阵容变 + 神器没变）
total_bad += report("A boss关 阵容变+神器没变（水皮场景）", GATE_C, True,
                    simulate_overrides(5, lineup_changed=True, artifact_changed=False))
# B: boss + 阵容变 + 神器也变
total_bad += report("B boss关 阵容变+神器也变", GATE_C, True,
                    simulate_overrides(5, lineup_changed=True, artifact_changed=True))
# C: 普通关（无补给屏）+ 阵容变
total_bad += report("C 普通关 阵容变（无补给屏）", GATE_C, False,
                    simulate_overrides(5, lineup_changed=True, artifact_changed=False))
# D: boss + 阵容没变 + 神器没变（合法跳过 —— 走未变闸门）
total_bad += report("D boss关 阵容没变（合法直跳，特性）", GATE_U, True,
                    simulate_overrides(5, lineup_changed=False, artifact_changed=False),
                    expect_bypass=True)

print("\n" + ("❌ 图级确认：存在绕过选卡的路径（见上）" if total_bad
      else "✅ 全绿：阵容变的场景每条路径都过选卡；阵容不变的合法直跳保留"))
