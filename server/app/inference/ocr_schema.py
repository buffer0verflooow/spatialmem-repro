"""阅读模式的模型输出契约。

和 VLResult 是两套：实时帧要的是「判断」（risk_level / advice），
阅读模式要的是「原文」。混在一个 schema 里会让两边的提示词互相干扰。
"""

from __future__ import annotations

from pydantic import BaseModel


class OcrResult(BaseModel):
    """qwen-vl-ocr 的识别结果。full_text 保留换行——它是分片的最强边界。"""

    full_text: str = ""

    def is_empty(self) -> bool:
        return not self.full_text.strip()
