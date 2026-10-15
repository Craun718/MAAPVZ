# -*- coding: utf-8 -*-
"""
================================================================================
get_ui/helper.py — 工具库（原 mini_helper.py）
================================================================================

职责：把"读设备界面"这件事拆成四个小函数，每个只干一件事：

    dump_ui_tree()   读屏幕 → 控件列表（list[DumpNode]）
    find_topmost()   从列表里挑目标（最上层、含关键词、可点的那个）
    maa_scale()      算坐标比例（设备像素 → MAA 空间）
    to_maa_rect()    把控件位置换算成 MAA 认的 (x, y, w, h)

调用链（记住这一条线，全库就懂了）：
    屏幕 → dump_ui_tree → find_topmost → to_maa_rect → box

本文件不注册任何 MAA 节点，它是纯工具。注册在同包的 nodes.py。
================================================================================
"""
from __future__ import annotations

import re                        # 正则：从 "[1700,100][1784,184]" 这种字符串里抠数字
import time                      # 重试前睡一会儿用
import xml.etree.ElementTree as ET   # 解析 uiautomator 吐出的 XML
from dataclasses import dataclass    # 让"定义一个数据类"少写很多样板代码
from typing import Any, Optional

# dump 出来的 XML 先存到设备这个路径，再读回来。_mini_ 前缀是为了不和别的 dump 文件搞混
DUMP_PATH = "/sdcard/_mini_dump.xml"

# 预编译正则：匹配 "[数字,数字][数字,数字]"，抓出 4 个整数
# 控件位置在 XML 里长这样：bounds="[1319,292][1403,376]"
_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


# ==============================================================================
# 数据结构：一个 DumpNode = 屏幕上一个控件
# ==============================================================================

@dataclass
class DumpNode:
    """屏幕上的一个控件（按钮/图片/文字……）。

    @dataclass 是 Python 的装饰器：写上它，Python 自动帮你生成
    __init__(self, resource_id, cls, ...) 这些构造函数代码，
    你只需要列出字段名和类型。

    bounds 是 (x1, y1, x2, y2)：左上角 + 右下角，单位是【设备物理像素】。
    """
    resource_id: str                     # 资源 id，如 "xxx:id/close_image"（找关闭钮的主力线索）
    cls: str                             # 控件类型，如 "android.widget.ImageView"
    desc: str                            # 无障碍描述（content-desc），如 "关闭"、"跳过 5s"
    text: str                            # 控件上显示的文字
    bounds: tuple[int, int, int, int]    # 位置
    clickable: bool                      # 能不能点（找按钮时只要 True 的）
    checked: bool                        # 勾选框状态（"今日不再展示"那种）
    package: str                         # 属于哪个应用（包名）

    @property
    def center(self) -> tuple[int, int]:
        """包围盒中心点。@property 让 n.center 像属性一样用，实际是个函数。"""
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    @property
    def short_id(self) -> str:
        """短 id："xxx:id/close_image" → "close_image"，日志里好看。"""
        return self.resource_id.split("/")[-1] if "/" in self.resource_id else self.resource_id


def _to_node(el: ET.Element) -> DumpNode:
    """把 XML 里的一个 <node> 元素变成 DumpNode（下划线开头 = 内部函数，外面别直接用）。"""
    m = _BOUNDS_RE.search(el.get("bounds", ""))          # el.get("属性名", 默认值) 读 XML 属性
    bounds = tuple(int(g) for g in m.groups()) if m else (0, 0, 0, 0)
    return DumpNode(
        resource_id=el.get("resource-id", ""),
        cls=el.get("class", ""),
        desc=el.get("content-desc", ""),
        text=el.get("text", ""),
        bounds=bounds,  # type: ignore[arg-type]
        clickable=el.get("clickable", "false") == "true",   # XML 里是字符串 "true"/"false"，转成布尔
        checked=el.get("checked", "false") == "true",
        package=el.get("package", ""),
    )


# ==============================================================================
# 函数①：读屏幕
# ==============================================================================

def dump_ui_tree(controller, retries: int = 2) -> list[DumpNode]:
    """抓取当前屏幕（焦点窗口）的控件树，返回控件列表。

    参数 controller 是 MAA 给的"设备遥控器"——它能让你在设备上跑命令、点屏幕。
    这一句 shell 干两件事（分号连接）：
      uiautomator dump 文件 >/dev/null 2>&1   → 把当前界面结构存成 XML 文件
      cat 文件                                 → 把文件内容读出来（这才是我们要的）
    post_shell(cmd).get(wait=True)：发命令并【等它跑完】，拿回全部输出文字。

    失败会重试 retries 次（设备忙时偶尔拿到半截 XML），还不行就抛 RuntimeError。
    """
    cmd = f"uiautomator dump {DUMP_PATH} >/dev/null 2>&1; cat {DUMP_PATH}"
    last_err = "no output"
    for _ in range(retries + 1):                 # range(3)：最多试 3 次
        out = controller.post_shell(cmd).get(wait=True) or ""
        start = out.find("<")                    # XML 一定以 "<" 开头，先粗校验
        if start >= 0:
            try:
                root = ET.fromstring(out[start:])            # 解析 XML
                return [_to_node(el) for el in root.iter("node")]   # 每个 <node> → DumpNode
            except ET.ParseError as exc:
                last_err = str(exc)
        time.sleep(0.3)
    raise RuntimeError(f"uiautomator dump 解析失败: {last_err}")


# ==============================================================================
# 函数②：挑目标（本包唯一的"识别逻辑"）
# ==============================================================================

def find_topmost(nodes: list[DumpNode], keyword: str,
                 clickable: bool = True) -> Optional[DumpNode]:
    """从控件列表里找【屏幕最上层】含关键词的可点控件。没找到返回 None。

    keyword 支持前缀指定"在哪个字段找"：
      "close"      → 不带前缀：id / desc / text 三个字段任一命中即可
      "id:close"   → 只在 resource_id 里找
      "desc:跳过"  → 只在 desc 里找（适合"跳过 5s"这种倒计时，子串匹配）
      "text:领取"  → 只在 text 里找

    clickable=True 过滤掉纯图片和纯容器（只留真能点的）。
    "最上层" = 列表里最后一个命中（uiautomator 树序 = 屏幕叠放序）。
    """
    # ---- 解析前缀："desc:跳过" → 只在 desc 字段找 "跳过" ----
    field = None                                   # None = 不带前缀，三字段都找
    for prefix, attr in (("id:", "resource_id"), ("desc:", "desc"), ("text:", "text")):
        if keyword.startswith(prefix):             # startswith：字符串以什么开头
            field = attr                           # 记住要找哪个字段
            keyword = keyword[len(prefix):]        # 把前缀切掉，留下真正的关键词
            break

    hits = []
    for n in nodes:
        if clickable and not n.clickable:
            continue                               # 不可点的不要
        if field is not None:
            # 指定了字段：getattr(n, 字段名) 按名字取属性，只在这一个字段里找
            if keyword in getattr(n, field):
                hits.append(n)
        elif keyword in n.resource_id or keyword in n.desc or keyword in n.text:
            hits.append(n)                         # 没带前缀：三字段任一命中
    return hits[-1] if hits else None              # 最后一个 = 最上层


# ==============================================================================
# 函数③④：坐标换算
# ==============================================================================

def maa_scale(controller, image: Any = None) -> tuple[float, float]:
    """算比例：MAA 截图尺寸 ÷ 设备物理尺寸。

    为什么要换算：dump 给的坐标是设备物理像素（如 1920×1080），
    而 MAA 的点击用的是截图缩放后的坐标（如 1280×720），两者差 1.5 倍。
    运行时现算，不写死，换分辨率的设备也能用。

    image：识别回调里 MAA 递进来的当前截图（numpy 数组，shape=(高,宽,3)）。
    """
    rw, rh = controller.resolution                 # 设备物理分辨率，如 (1920, 1080)
    if rw <= 0 or rh <= 0:                         # 还没初始化就先截一帧逼它初始化
        controller.post_screencap().wait()
        rw, rh = controller.resolution
    if image is not None:
        ih, iw = int(image.shape[0]), int(image.shape[1])     # 截图尺寸，如 720, 1280
    else:
        img = controller.cached_image              # 没有现成截图就用框架缓存的
        ih, iw = int(img.shape[0]), int(img.shape[1])
    if rw <= 0 or rh <= 0 or iw <= 0 or ih <= 0:
        raise RuntimeError(f"无法确定分辨率换算比例: raw=({rw},{rh}) img=({iw},{ih})")
    return iw / rw, ih / rh                        # 如 1280/1920 = 0.667


def to_maa_rect(controller, node: DumpNode, image: Any = None) -> tuple[int, int, int, int]:
    """把控件的 bounds（物理像素）换算成 MAA 空间的 (x, y, 宽, 高)。

    这个四元组就是要返回给 MAA 的 box——MAA 拿到它会自动换算出点击中心点。
    """
    sx, sy = maa_scale(controller, image)
    x1, y1, x2, y2 = node.bounds
    return (int(x1 * sx), int(y1 * sy), int((x2 - x1) * sx), int((y2 - y1) * sy))
