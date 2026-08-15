"""Read COLMAP binary output (cameras.bin / images.bin / points3D.bin).

Binary layout follows the COLMAP reconstruction format documented at
https://colmap.github.io/format.html
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class Camera:
    camera_id: int
    model: str
    width: int
    height: int
    params: np.ndarray


@dataclass
class Image:
    image_id: int
    qvec: np.ndarray  # (4,) w,x,y,z
    tvec: np.ndarray  # (3,) world->camera translation
    camera_id: int
    name: str

    def rotmat(self) -> np.ndarray:
        qw, qx, qy, qz = self.qvec
        return np.array(
            [
                [1 - 2 * (qy**2 + qz**2), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
                [2 * (qx * qy + qz * qw), 1 - 2 * (qx**2 + qz**2), 2 * (qy * qz - qx * qw)],
                [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx**2 + qy**2)],
            ]
        )

    def center(self) -> np.ndarray:
        return -self.rotmat().T @ self.tvec

    def pose_matrix(self) -> np.ndarray:
        """4x4 world->camera."""
        T = np.eye(4)
        T[:3, :3] = self.rotmat()
        T[:3, 3] = self.tvec
        return T


MODEL_NAMES = {0: "SIMPLE_PINHOLE", 1: "PINHOLE", 2: "SIMPLE_RADIAL", 3: "RADIAL"}


def read_cameras_binary(path: Path) -> dict[int, Camera]:
    cameras: dict[int, Camera] = {}
    with open(path, "rb") as f:
        num = struct.unpack("<Q", f.read(8))[0]
        for _ in range(num):
            camera_id = struct.unpack("<i", f.read(4))[0]
            model_id, width, height = struct.unpack("<iqq", f.read(20))
            num_params = {0: 4, 1: 4, 2: 5, 3: 5}[model_id]
            params = np.frombuffer(f.read(8 * num_params), dtype="<f8")
            cameras[camera_id] = Camera(
                camera_id, MODEL_NAMES.get(model_id, f"UNKNOWN_{model_id}"), width, height, params
            )
    return cameras


def read_images_binary(path: Path) -> dict[int, Image]:
    images: dict[int, Image] = {}
    with open(path, "rb") as f:
        num = struct.unpack("<Q", f.read(8))[0]
        for _ in range(num):
            image_id = struct.unpack("<i", f.read(4))[0]
            qvec = np.frombuffer(f.read(32), dtype="<f8")
            tvec = np.frombuffer(f.read(24), dtype="<f8")
            camera_id = struct.unpack("<i", f.read(4))[0]
            name = b""
            while True:
                c = f.read(1)
                if c == b"\x00":
                    break
                name += c
            name = name.decode()
            num_points = struct.unpack("<Q", f.read(8))[0]
            # each point2D: x (f64), y (f64), point3D_id (i64) = 24 bytes
            f.read(3 * 8 * num_points)
            images[image_id] = Image(image_id, qvec, tvec, camera_id, name)
    return images


def read_points3d_binary(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (N,3) world coordinates and (N,) track lengths."""
    with open(path, "rb") as f:
        num = struct.unpack("<Q", f.read(8))[0]
        pts = np.empty((num, 3), dtype="<f8")
        track = np.empty(num, dtype="<i8")
        for i in range(num):
            f.read(8)  # point id
            pts[i] = np.frombuffer(f.read(24), dtype="<f8")
            f.read(3)  # rgb
            f.read(8)  # error
            track_len = struct.unpack("<Q", f.read(8))[0]
            track[i] = track_len
            # track element: image_id (u32), point2D_idx (u32) = 8 bytes
            f.read(2 * 4 * track_len)
    return pts, track
