# -*- coding: utf-8 -*-
"""
================================================================================
get_ui/nodes.py — 注册层（原 mini_nodes.py）
================================================================================

职责：把 helper.py 的能力【注册】成 MAA pipeline 能引用的节点。

整个文件只注册一个识别节点 "get_ui"——通用控件定位器。
动作不用写：pipeline 原生 Click / Swipe 会自动接住识别返回的位置。

【"注册"到底是怎么回事】
@AgentServer.custom_recognition("get_ui") 这一行叫【装饰器】。
效果：MAA 的 agent 启动时 import 这个模块，装饰器就把下面这个类
登记到 MAA 的名单上，名字是 "get_ui"。之后 pipeline 里写
"custom_recognition": "get_ui" 就能找到它。

【识别节点的契约（就两条）】
  入：MAA 调 analyze(self, context, argv)，argv 里带着当前截图和 pipeline 参数
  出：返回 AnalyzeResult(box=..., detail=...)
      box 给了 (x, y, w, h) → 命中，pipeline 的动作会点这个位置
      box 给了 None        → 没命中，MAA 按 rate_limit 轮询 / timeout 后走下一个

pipeline 里怎么用（嵌套对象写法）：
    "找关闭钮": {
        "recognition": { "type": "Custom", "param": {
            "custom_recognition": "get_ui",
            "custom_recognition_param": { "keyword": "close" } } },
        "action": "Click",
        "rate_limit": 3000
    }
================================================================================
"""
from __future__ import annotations

import json
from typing import Any

from maa.agent.agent_server import AgentServer          # 注册中心
from maa.custom_recognition import CustomRecognition    # 识别节点的基类（爹）

# 相对导入：点(.)开头 = "和我在同一个包里找"。
# 这行只在"作为包的一部分被 import"时有效（import get_ui.nodes 就是这种），
# 直接 python nodes.py 单跑会报错——单文件调试请用包外的方式导入。
from .helper import dump_ui_tree, find_topmost, to_maa_rect

# 允许从 pipeline 透传给 find_topmost 的参数白名单
_QUERY_KEYS = ("keyword", "clickable")


def _parse_params(raw: Any) -> dict[str, Any]:
    """把 pipeline 传进来的参数统一成 dict。

    MAA 传参有两种形态：直接是 dict，或是一段 JSON 字符串，这里都兼容。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


@AgentServer.custom_recognition("get_ui")
class GetUi(CustomRecognition):
    """通用控件定位：找【屏幕最上层】的匹配控件，返回它的位置 box。

    pipeline 参数：
      keyword    必填。关键词，支持前缀指定字段：
                   "close"     → id/desc/text 三字段任一命中
                   "id:close"  → 只在 id 里找
                   "desc:跳过" → 只在 desc 里找
                   "text:领取" → 只在 text 里找
      clickable  可选，默认 true：只要可点控件

    命中多个时取最上层的那个。未命中返回 box=None。
    """

    def analyze(self, context, argv: CustomRecognition.AnalyzeArg):
        # ---- 1. 读参数，白名单透传 ----
        params = _parse_params(argv.custom_recognition_param)
        query = {k: params[k] for k in _QUERY_KEYS if k in params}
        if not query.get("keyword"):
            return CustomRecognition.AnalyzeResult(
                box=None, detail={"status": "keyword required"}
            )

        # ---- 2. 取数：context.tasker.controller 是 MAA 给的设备遥控器 ----
        try:
            nodes = dump_ui_tree(context.tasker.controller)
        except RuntimeError as exc:
            return CustomRecognition.AnalyzeResult(
                box=None, detail={"status": "dump failed", "reason": str(exc)}
            )

        # ---- 3. 挑目标（**query 把 dict 拆成关键字参数喂进去）----
        node = find_topmost(nodes, **query)
        if node is None:
            return CustomRecognition.AnalyzeResult(
                box=None, detail={"status": "not found", "query": query}
            )

        # ---- 4. 换算成 MAA 坐标并返回。image=argv.image 是 MAA 递进来的当前截图 ----
        box = to_maa_rect(context.tasker.controller, node, image=argv.image)
        return CustomRecognition.AnalyzeResult(
            box=box,                       # ← 核心产出：位置。MAA 会拿它当 Click/Swipe 的目标
            detail={                       # ← 调试信息，只进日志，不影响行为
                "status": "hit",
                "matched_id": node.resource_id,
                "desc": node.desc,
                "text": node.text,
                "maa_box": list(box),
            },
        )
