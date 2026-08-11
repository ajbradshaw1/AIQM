"""Tests for repository-relative experimental DummyCamera presets."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from drivers.rheed_camera import DummyCamera
from gui.workers import RheedCameraWorker


class DummyCameraPresetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary.name)
        values = {
            "1x1_1.bmp": 60,
            "c6x2_1.bmp": 130,
            "Twinned2x1_1.bmp": 210,
        }
        for name, value in values.items():
            image = np.full((24, 32), value, dtype=np.uint8)
            Image.fromarray(image).save(self.data_root / name)
        rt13 = np.zeros((24, 32), dtype=np.uint8)
        rt13[5:19, 13:19] = 180
        Image.fromarray(rt13).save(self.data_root / "RT13_20.png")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_each_preset_loads_a_stable_experimental_frame(self) -> None:
        means = {}
        for preset in DummyCamera.PRESETS:
            camera = DummyCamera(
                width=64,
                height=48,
                preset=preset,
                data_root=self.data_root,
            )
            camera.connect()
            first = camera.read_frame()
            second = camera.read_frame()
            self.assertEqual(first.shape, (48, 64, 3))
            self.assertEqual(first.dtype, np.uint8)
            np.testing.assert_array_equal(first, second)
            self.assertIsNotNone(camera.source_path)
            means[preset] = float(first[:, :, 1].mean())
        self.assertEqual(len(set(means.values())), len(means))

    def test_default_data_root_is_workspace_relative(self) -> None:
        camera = DummyCamera()
        expected = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "dummy_camera"
        )
        self.assertEqual(camera._data_root, expected)

    def test_packaged_assets_match_registered_sha256(self) -> None:
        expected = {
            "1x1_1.bmp": "c99f26377b19306a449874fdc062a2e58bc31f27c11388d116f709acd95b6657",
            "c6x2_1.bmp": "cf059f8530f0aee93df928425d498a0aa96e34f4303ee1ba6664c579e263c8c1",
            "Twinned2x1_1.bmp": "cb9b108583dabc19a59c8b62c264d2345e84afa2b5c9b789fabbaf36db7bcfac",
            "RT13_20.png": "322004dbb7324cfc85066a671857d1860098b26bcda6afd2558f97f58c26ba6d",
        }
        data_root = Path(__file__).resolve().parents[1] / "data" / "dummy_camera"
        for filename, expected_sha in expected.items():
            path = data_root / filename
            self.assertTrue(path.is_file(), path)
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(), expected_sha
            )
            with Image.open(path) as image:
                self.assertEqual(image.size, (656, 492))

    def test_packaged_presets_produce_distinct_experimental_frames(self) -> None:
        frames = []
        for preset, filename in DummyCamera.SOURCE_FILENAMES.items():
            camera = DummyCamera(preset=preset)
            camera.connect()
            frames.append(hashlib.sha256(camera.read_frame()).hexdigest())
            self.assertEqual(camera.source_path.name, filename)
        self.assertEqual(len(set(frames)), len(DummyCamera.PRESETS))

    def test_missing_asset_fails_closed_without_bright_spot_fallback(self) -> None:
        empty_root = self.data_root / "missing"
        empty_root.mkdir()
        camera = DummyCamera(preset="dummy", data_root=empty_root)
        with self.assertRaisesRegex(FileNotFoundError, "asset missing"):
            camera.connect()
        self.assertFalse(camera.connected)

    def test_worker_forwards_dummy_preset(self) -> None:
        for mode in DummyCamera.PRESETS:
            camera = RheedCameraWorker(mode=mode)._create_camera()
            self.assertIsInstance(camera, DummyCamera)
            self.assertEqual(camera.preset, mode)

    def test_rt13_demo_uses_named_source_and_fixed_rotation(self) -> None:
        camera = DummyCamera(
            width=64,
            height=48,
            preset="dummy_rt13_tilted",
            data_root=self.data_root,
        )
        camera.connect()
        frame = camera.read_frame()
        self.assertEqual(camera.source_path.name, "RT13_20.png")
        self.assertEqual(
            DummyCamera.ROTATION_DEGREES["dummy_rt13_tilted"],
            15.0,
        )
        self.assertGreater(int(frame[:, :, 1].max()), 0)


if __name__ == "__main__":
    unittest.main()
