"""设置 API:排版等用户偏好设置。

GET /api/settings/typography 返回默认设置 + 字体清单 + 合法范围,
前端首次加载用它初始化设置面板与滑块约束。
"""
from __future__ import annotations

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from .. import typography

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/typography")
def get_typography():
    return {
        "defaults": typography.DEFAULT_SETTINGS,
        "fonts": typography.FONTS,
        "ranges": {
            "fontSize": {"min": typography.FONT_SIZE_MIN, "max": typography.FONT_SIZE_MAX},
            "spacing": {"min": typography.SPACING_MIN, "max": typography.SPACING_MAX},
            "margin": list(typography.MARGINS),
        },
    }


class TypographyIn(BaseModel):
    # 宽松类型:前端 localStorage 脏数据(非数字字符串等)不应在 pydantic 层 422,
    # 交由 _coerce 规整 + to_css 钳制。extra 字段忽略。
    fontSize: str | int | float | None = None
    spacing: str | int | float | None = None
    margin: object | None = None
    font: object | None = None

    model_config = {"extra": "ignore"}


def _coerce(raw: dict) -> dict:
    """把宽松输入规整成 to_css 能用的 dict:类型转换失败 → 不传该键(用默认)。"""
    out = {}
    for key in ("fontSize", "spacing", "margin", "font"):
        if key not in raw or raw[key] is None:
            continue
        val = raw[key]
        if key in ("fontSize", "spacing"):
            try:
                out[key] = float(val)
            except (TypeError, ValueError):
                continue  # 非数字 → 跳过,to_css 用默认
            if key == "fontSize":
                out[key] = int(out[key])
        else:
            out[key] = str(val)
    return out


@router.post("/typography/css")
def get_typography_css(body: TypographyIn):
    """前端发当前设置,后端返回可注入 foliate 的 CSS 字符串。

    校验在后端(_coerce 规整 + to_css 内部 clamp + 枚举回退),前端无需重复。
    对脏数据(非数字、类型错)宽容:回退默认值,返回 200,不 422。
    """
    css = typography.to_css(_coerce(body.model_dump()))
    return {"css": css}
