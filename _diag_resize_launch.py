# -*- coding: utf-8 -*-
"""诊断启动器（用完即删）：把窗口拖动/缩放的入参和矩形变化写进 launch.log。"""
import dashboard as D

_rz, _rzm, _dm = D.win_resize_begin, D.win_resize_move, D.win_drag_move


def _rz_wrapped(edge, x=0, y=0, scale=1.0):
    res = _rz(edge, x, y, scale)
    D._log("[RSZ] begin %r at=(%r,%r) scale=%r -> %r" % (edge, x, y, scale, res))
    return res


def _rzm_wrapped(x, y):
    res = _rzm(x, y)
    if res.get("w"):
        D._log("[RSZ] move -> %dx%d" % (res["w"], res["h"]))
    return res


def _dm_wrapped(x, y):
    res = _dm(x, y)
    if res.get("dragging"):
        D._log("[DRAG] move(%r,%r) -> dragging" % (x, y))
    return res


D.win_resize_begin, D.win_resize_move, D.win_drag_move = _rz_wrapped, _rzm_wrapped, _dm_wrapped
D._APP_MUTEX_NAME = "Local\\WBDiagResize_5"
raise SystemExit(D.main())
