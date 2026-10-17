# -*- coding: utf-8 -*-
"""
core/button_trigger.py —— 鼠标按键当「热键」用
=================================================

## 为什么需要它

`RegisterHotKey` 注册的是**系统级按键**，但系统按键池是共享的：
F5 常被游戏/远程控制软件占走，F8、F9 也会被抢。而鼠标侧键
（G1/G2、鼠标中键）几乎没人占——尤其是**游戏鼠标的额外按键**。

而且本项目已经有一个Raw Input 监听线程在跑（为了识别自定义鼠标键），
它能捕获**任何鼠标按键的按下/抬起**，等于现成的全局触发器。

## 相比 RegisterHotKey 的优势

|维度 | RegisterHotKey | 本模块（Raw Input） |
|---|---|---|
| 占用冲突 | 会（F5/F8 等热门键常被抢） | 几乎不会（侧键冷门） |
| 前台窗口要求 | 无 | **无**（RIDEV_INPUTSINK，后台也收得到） |
| 能否被游戏屏蔽 | 会被部分游戏屏蔽 | 不会被（Raw Input 是驱动级） |
| 响应速度 | 即时 | 即时 |

## 注意：会「透传」给目标程序

Raw Input 监听是**旁路观察**，不阻止事件继续传递。
所以按侧键触发的同时，游戏里也会收到这个侧键——
对绝大多数场景这是优点（玩家本来就要用这个键）。
"""

import ctypes
import ctypes.wintypes as wt
import threading

_user32 = ctypes.windll.user32


# 鼠标按键编号（Raw Input / RI_MOUSE.usButtonOffset 体系）
class MouseBtn(object):
    LEFT = 0
    RIGHT = 1
    MIDDLE = 2       # 滚轮按下 = 鼠标中键，游戏里几乎没人用，最推荐
    XBUTTON1 = 3     # 侧键1（常标G1）
    XBUTTON2 = 4     # 侧键2（常标 G2）


# 对外的可读名称 -> 内部编号
BUTTON_NAMES = {
    "middle": MouseBtn.MIDDLE,
    "x1": MouseBtn.XBUTTON1,
    "x2": MouseBtn.XBUTTON2,
    "left": MouseBtn.LEFT,
    "right": MouseBtn.RIGHT,
}

# 显示名
BUTTON_LABELS = {
    "middle": "鼠标中键",
    "x1": "侧键1(G1)",
    "x2": "侧键2(G2)",
    "left": "鼠标左键",
    "right": "鼠标右键",
}

# ---- 自定义捕获用：Raw Input 位图编号 -> 规范名 / 显示名 ----
# 【为什么要这套】原来触发方式只能从 middle / x1 / x2 三个里下拉选，
# 于是**游戏鼠标的 G3~G8 绑不了**，用户必须回到「识别并添加」那条路，
# 还得再点两次按钮。改成「按一下就绑」后，这里要把整个位图
# （0~15，协议上限）都映射出名字，否则捕获到的编号认不出来。
#
# 注意：编号 0 是左键、1 是右键，这两个**不参与触发绑定**——
# 把左键绑成触发键意味着「点一下界面就启动/停止连点」，
# 用户在点按钮的过程中就会误触发。所以捕获时直接拒绝它们。
RAW_ID_TO_NAME = {
    0: "left", 1: "right", 2: "middle",
    3: "x1", 4: "x2", 5: "x3", 6: "x4",
    7: "x5", 8: "x6", 9: "x7", 10: "x8",
}

RAW_ID_LABELS = {
    0: "鼠标左键", 1: "鼠标右键", 2: "鼠标中键",
    3: "侧键1(G1)", 4: "侧键2(G2)",
    5: "侧键3(G3)", 6: "侧键4(G4)",
    7: "侧键5(G5)", 8: "侧键6(G6)",
    9: "侧键7(G7)", 10: "侧键8(G8)",
}

# 允许绑成触发器的编号（排除左键/右键）
BINDABLE_RAW_IDS = [i for i in sorted(RAW_ID_TO_NAME) if i >= 2]

# 规范名 -> 位图编号（RAW_ID_TO_NAME 的反查表）
# 【为什么必须有这张表】RAW_ID_TO_NAME 是「编号 -> 名字」，
# 用名字反查编号时 .get("x5") 永远返回 None——
# 于是 set() 里那句校验会误判成「不支持的鼠标键 x5」，
# 表现就是「捕获到 G5 却绑不上，报错信息还指向一个内部名」。
# 这类「方向搞反的字典」最难查：写的时候看起来完全合理。
NAME_TO_RAW_ID = {v: k for k, v in RAW_ID_TO_NAME.items()}


class ButtonTrigger(object):
    """
    把某个鼠标按键当成全局触发器。

    用法：
        bt = ButtonTrigger("middle", on_press)
        bt.install()      # 装进 Raw Input 监听（不自己起线程）
        ...
        bt.remove()

    为什么不自己起 Raw Input 线程：
        Windows 的 Raw Input 是**按线程**注册的，一个线程只能注册一次设备列表。
        dashboard.py 已经有一个监听线程在跑（start_mouse_listener），
        这里如果再起一个，两个线程会互相抢 WM_INPUT。
        所以做成「挂到已有监听上」的形式，由dashboard 分发。
    """

    def __init__(self, button, on_press, label=None, action=None):
        name = str(button).lower().strip()
        # 三个来源都能算出编号：BUTTON_NAMES（老接口用名字）、
        # RAW_ID_TO_NAME（G3~G8 的内部名 x5）、纯数字字符串。
        # 原来只认第一种，导致 set_by_raw_id 翻出来的 "x5" 被判非法 ——
        # 也就是「捕获到 G5 却绑不上」。
        if name in BUTTON_NAMES:
            bid = BUTTON_NAMES[name]
        elif name in NAME_TO_RAW_ID:
            bid = NAME_TO_RAW_ID[name]
        elif name.isdigit() and int(name) in RAW_ID_TO_NAME:
            bid = int(name)
        else:
            raise ValueError("不支持的鼠标键: %r（可选 %s）"
                             % (button, "、".join(BUTTON_NAMES)))
        self.name = name if name in BUTTON_NAMES else \
            RAW_ID_TO_NAME[bid]
        self.button_id = bid
        self.on_press = on_press
        self._armed = True      # 防连击：按住不放只触发一次
        self._custom_label = label
        # 这个键服务哪个动作（"mouse" 连点 / "key" 键盘锁定 / None 未知）。
        # 【为什么必须记】触发方式区要显示「鼠标连点现在绑的是哪个键」，
        # 只按按键名索引是反的：界面问的是「这个动作绑了谁」。
        # 以前两边各猜一半，表现为「明明绑了 G2，界面还显示 F5」。
        self.action = action

    # ---- 内部：被 Raw Input 监听线程调用 ----
    def feed(self, button_id, down):
        """收到一个鼠标按键事件。返回 True 表示这个事件被消费掉了。"""
        if button_id != self.button_id:
            return False
        if down:
            if not self._armed:
                return True
            self._armed = False
            try:
                if self.on_press:
                    self.on_press()
            except Exception:
                pass
        else:
            self._armed = True     # 松开了，重新武装
        return True

    def label(self):
        return BUTTON_LABELS.get(self.name, self.name)

    def set_label(self, label):
        """覆盖显示名（自定义捕获时用「侧键5(G5)」而不是「x5」）。"""
        if label:
            self._custom_label = label

    @property
    def display(self):
        return getattr(self, "_custom_label", None) or self.label()


# ======================================================================
# 全局分发中心
# ======================================================================
class _TriggerHub(object):
    """
    持有所有鼠标键触发器，供 dashboard 的 Raw Input 线程统一分发。

    一个按键只能绑定一个动作，所以按 button_id 建索引。

    【改动】绑定不再只认middle/x1/x2 三个规范名，而是可以直接用
    Raw Input 位图编号（0~15）来绑，这样游戏鼠标的 G3~G8 也能当触发键。
    内部存的是 ButtonTrigger，只是构造时用RAW_ID_TO_NAME 翻成规范名；
    显示名走 RAW_ID_LABELS，避免侧键3 在界面上显示成「x3」。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self._by_id = {}        # {button_id: ButtonTrigger}
        self._by_name = {}      # {name: ButtonTrigger}
        self._labels = {}       # {name: 自定义显示名}

    def _label_for(self, name):
        t = self._by_name.get(name)
        if t is not None:
            return t.display
        return self._labels.get(name) or BUTTON_LABELS.get(name, name)

    def set(self, name, on_press, label=None, action=None):
        """把某个鼠标键绑到回调上，返回旧绑定（没有则 None）。

        【一个动作只保留一个键】传了 action 时，先把该动作原来的绑定
        摘掉再绑新的。不这么做会出大事：启动时默认把中键绑给连点，
        用户之后又绑了 G2，中键那个绑定还挂着 —— 一旦连点滴答的按键
        是中键，第一次注入就把连点自己关了（实测「点一下就没后续」）。
        """
        with self.lock:
            key = str(name).lower().strip()
            # 【坑】这里原来只查 BUTTON_NAMES（只有 middle/x1/x2/left/right），
            # 但 set_by_raw_id 传进来的是 RAW_ID_TO_NAME 翻出的 "x5"，
            # 于是「捕获到 G5 -> 报不支持的鼠标键 x5」。
            # 现在两套名字都认，具体解析交给 ButtonTrigger（它会翻回位图编号）。
            bid = BUTTON_NAMES.get(key) or NAME_TO_RAW_ID.get(key)
            if bid is None and key.isdigit():
                bid = int(key) if int(key) in RAW_ID_TO_NAME else None
            if bid is None:
                raise ValueError("不支持的鼠标键: %r" % (name,))
            old = self._by_name.get(key)
            # 同动作的旧绑定先摘掉：一个动作一个触发键，不许多个键
            # 同时指向同一个开关（那会让注入回环有可乘之机）。
            if action:
                for n, t in list(self._by_name.items()):
                    if t.action == action:
                        self._by_name.pop(n, None)
                        self._by_id.pop(t.button_id, None)
                        self._labels.pop(n, None)
            if old is not None:
                self._by_id.pop(old.button_id, None)
            t = ButtonTrigger(key, on_press, action=action)
            self._by_id[bid] = t
            self._by_name[t.name] = t
            # 显示名优先级：自定义捕获给的 label > 位图表 > 规范名表。
            # 不给 label 时回退到规范名（BUTTON_LABELS），保持老行为。
            t.set_label(label or RAW_ID_LABELS.get(bid))
            self._labels[t.name] = t.display
            return old

    def set_by_raw_id(self, raw_id, on_press, label=None, action=None):
        """
        直接用 Raw Input 位图编号绑定（自定义捕获走这条）。

        raw_id 是 usButtons 位图的编号（0左 1右 2中 3侧1 ...），
        由 dashboard 的监听线程解析出来 —— 它比「规范名」更底层，
        所以用户按G5 时我们拿到的就是 5，不需要任何预先登记。
        """
        raw_id = int(raw_id)
        name = RAW_ID_TO_NAME.get(raw_id)
        if name is None:
            raise ValueError(
                "该按键（编号 %d）超出可识别范围。\n"
                "鼠标按键位图上限是 16 个，编号 %d 不可用。"
                % (raw_id, raw_id))
        if raw_id < 2:
            raise ValueError("左键/右键不能作为触发键")
        return self.set(name, on_press, label=label, action=action)

    def clear(self, name=None):
        with self.lock:
            if name is None:
                self._by_id.clear()
                self._by_name.clear()
                self._labels.clear()
                return
            key = str(name).lower().strip()
            t = self._by_name.pop(key, None)
            self._labels.pop(key, None)
            if t is not None:
                self._by_id.pop(t.button_id, None)

    def dispatch(self, button_id, down):
        """分发一个按键事件。返回 True 表示有绑定消费了它。

        【顺带修掉一类「按一下就没后续」】触发器的 _armed 在「按下时
        置 False、抬起时置 True」。万一某个抬起事件丢了（驱动丢帧、
        报文被我们自己的过滤规则挡掉），_armed 就永远停在 False，
        之后再按这个键一次反应都没有 —— 用户看到的就是
        「触发键只灵了一下」。
        所以：**任何一个别的键有动静，就把所有触发器重新武装**。
        用户不可能同时按住两个键还指望旧键继续生效，这个补偿是安全的。
        """
        with self.lock:
            t = self._by_id.get(button_id)
            if t is None:
                # 没绑定的键也有信息量：说明用户正在操作别的键，
                # 借机把可能卡住的触发器全部恢复武装
                for other in self._by_id.values():
                    other._armed = True
                return False
        return t.feed(button_id, down)

    def list_labels(self):
        with self.lock:
            return {n: self._label_for(n) for n in self._by_name}

    def list_bindings(self):
        """
        返回 {name: {"label": 显示名, "raw_id": 位图编号, "action": 动作}}，
        供前端显示「当前绑的是哪个键」，以及还原选中项。
        """
        with self.lock:
            out = {}
            for n, t in self._by_name.items():
                out[n] = {"label": self._label_for(n),
                          "raw_id": t.button_id,
                          "action": t.action}
            return out

    def list_by_action(self):
        """
        返回 {action: {"label": 显示名, "raw_id": 编号, "name": 规范名}}。

        【为什么需要它】界面问的是「鼠标连点现在绑的是哪个键」，
        这是按**动作**查，而 HUB 内部是按**按键名**索引的。
        以前 button_hotkeys() 只返回 {name: label}，前端拿
        labels.mouse 去取，永远取到 undefined，于是回退显示系统热键 F5
        —— 这就是「绑了 G2，触发方式还显示 F5」的直接原因。
        """
        with self.lock:
            out = {}
            for t in self._by_name.values():
                if t.action:
                    out[t.action] = {"label": t.display,
                                     "raw_id": t.button_id,
                                     "name": t.name}
            return out


HUB = _TriggerHub()