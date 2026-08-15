"""Qwen-VL 结构化输出契约（CLAUDE.md §5.3）。

一次调用同时完成识别 + 决策。结果整形层用模板从这个结构体生成 <=30 字文案，
不再调用模型——这是把端到端 P50 压进 1.5s 的前提。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

RiskLevel = Literal["none", "low", "medium", "high"]


class VLResult(BaseModel):
    """模型必须返回的字段。缺字段用默认值兜底，不让解析整体失败。"""

    scene: str = Field(default="", max_length=64, description="场景概括")
    objects: list[str] = Field(default_factory=list, description="关键物体标签")
    ocr_text: str = Field(default="", max_length=512, description="画面中的文字")
    keywords: list[str] = Field(default_factory=list, description="供下一帧 RAG 检索")
    risk_level: RiskLevel = Field(default="none")
    advice: str = Field(default="", max_length=128, description="给用户的建议原文")

    @field_validator("objects", "keywords", mode="before")
    @classmethod
    def _coerce_list(cls, v: object) -> list[str]:
        """模型有时把列表写成逗号分隔字符串，这里统一收敛。"""
        if v is None:
            return []
        if isinstance(v, str):
            return [s.strip() for s in v.replace("，", ",").split(",") if s.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []

    @field_validator("objects", "keywords", mode="after")
    @classmethod
    def _cap_list(cls, v: list[str]) -> list[str]:
        return v[:10]

    @field_validator("risk_level", mode="before")
    @classmethod
    def _normalize_risk(cls, v: object) -> str:
        if not isinstance(v, str):
            return "none"
        s = v.strip().lower()
        alias = {
            "": "none",
            "no": "none",
            "safe": "none",
            "normal": "none",
            "warn": "medium",
            "warning": "medium",
            "danger": "high",
            "critical": "high",
            "无": "none",
            "低": "low",
            "中": "medium",
            "高": "high",
        }
        return alias.get(s, s if s in ("none", "low", "medium", "high") else "none")
