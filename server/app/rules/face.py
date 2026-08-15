"""人脸检测（CLAUDE.md §4.7）。

原方案想用"敏感词表 + 像素校验"达到 99% 拦截准确率，做不到。
这里给出可插拔接口 + 驳回路径；真实检测器在 W4 接入。
首期验收指标为「人脸检出召回 >= 95%」，不承诺 99% 的整体拦截准确率。
"""

from __future__ import annotations

import io
from typing import Protocol, runtime_checkable

from app.observability import get_logger

log = get_logger(__name__)

BBox = tuple[int, int, int, int]  # x, y, w, h


@runtime_checkable
class FaceDetector(Protocol):
    async def detect(self, image_bytes: bytes) -> list[BBox]: ...


class NullFaceDetector:
    """默认实现：不检测。face_detect_enabled=false 时使用。"""

    async def detect(self, image_bytes: bytes) -> list[BBox]:
        return []


class OpenCVFaceDetector:
    """基于 OpenCV Haar cascade 的 CPU 检测器（~20ms @ 640px）。

    需要 `pip install -e ".[face]"`。W4 任务：换成 YOLOv8n-face 并用
    标注测试集验证召回率，Haar 在侧脸和遮挡场景召回不足。
    """

    def __init__(self, min_size: int = 40) -> None:
        import cv2

        self._cv2 = cv2
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self._cascade = cv2.CascadeClassifier(cascade_path)
        if self._cascade.empty():
            raise RuntimeError(f"Haar cascade 加载失败: {cascade_path}")
        self._min_size = min_size

    async def detect(self, image_bytes: bytes) -> list[BBox]:
        import numpy as np
        from PIL import Image

        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                gray = np.asarray(img.convert("L"))
        except Exception as exc:
            log.warning("face_detect_decode_failed", error=str(exc))
            return []

        faces = self._cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5,
            minSize=(self._min_size, self._min_size),
        )
        return [(int(x), int(y), int(w), int(h)) for x, y, w, h in faces]


def build_face_detector(enabled: bool) -> FaceDetector:
    if not enabled:
        return NullFaceDetector()
    try:
        return OpenCVFaceDetector()
    except Exception as exc:
        # 降级而不是崩服务：人脸检测不可用时记警告，由告警发现
        log.error("face_detector_unavailable_fallback_to_null", error=str(exc))
        return NullFaceDetector()
