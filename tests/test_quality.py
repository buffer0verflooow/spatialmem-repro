"""画质门控测试：清晰/模糊/过暗/过曝/低分辨率。"""

import io

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from spatialmem.quality import QualityPolicy, evaluate, is_acceptable


def jpeg_from(gray: np.ndarray) -> bytes:
    img = Image.fromarray(np.asarray(gray, dtype=np.uint8), mode="L")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def sharp_frame(size: tuple[int, int] = (640, 360), luma: int = 160) -> bytes:
    img = Image.new("L", size, luma)
    d = ImageDraw.Draw(img)
    for i in range(0, size[0], 24):
        d.line([(i, 0), (i, size[1])], fill=max(0, luma - 90), width=3)
        d.line([(0, i), (size[0], i)], fill=min(255, luma + 90), width=3)
    return jpeg_from(np.asarray(img, dtype=np.uint8))


def test_sharp_normal_frame_passes() -> None:
    assert is_acceptable(sharp_frame())


def test_blurry_frame_rejected() -> None:
    img = Image.open(io.BytesIO(sharp_frame())).filter(ImageFilter.GaussianBlur(12))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    q = evaluate(buf.getvalue())
    ok, reasons = q.acceptable()
    assert not ok
    assert "blurry" in reasons


def test_too_dark_rejected() -> None:
    q = evaluate(sharp_frame(luma=18))
    ok, reasons = q.acceptable()
    assert not ok
    assert "too_dark" in reasons


def test_overexposed_rejected() -> None:
    q = evaluate(sharp_frame(luma=250))
    ok, reasons = q.acceptable()
    assert not ok
    assert "over_exposed" in reasons


def test_low_resolution_rejected() -> None:
    # 320×180 是实验里开始幻觉的分辨率，必须拒
    assert not is_acceptable(sharp_frame(size=(320, 180)))
    # 160×90 同理
    assert not is_acceptable(sharp_frame(size=(160, 90)))
    # 640×360 放行
    assert is_acceptable(sharp_frame(size=(640, 360)))


def test_policy_resolution_thresholds_are_loose_at_480x270() -> None:
    # 长边 480 短边 270 满足门限（短边 270 >= 320? 否 -> 拒）
    q = evaluate(sharp_frame(size=(480, 270)))
    assert not q.acceptable()[0]
    # 放宽短边门限后放行（可配置性）
    policy = QualityPolicy(min_short_edge=270)
    assert q.acceptable(policy)[0]
