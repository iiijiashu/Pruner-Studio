import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

from pruner_studio.errors import AppError
from pruner_studio.storage import JsonStore, SettingsStore

ROOT = Path(__file__).resolve().parents[2]


class Phase3Tests(unittest.TestCase):
    def test_manifest_and_early_dpi(self):
        root = ET.parse(ROOT / "build/Pruner-Studio.exe.manifest").getroot()
        self.assertEqual(root.findtext(".//{http://schemas.microsoft.com/SMI/2016/WindowsSettings}dpiAwareness"), "PerMonitorV2")
        self.assertEqual(root.findtext(".//{http://schemas.microsoft.com/SMI/2005/WindowsSettings}dpiAware"), "true/pm")
        self.assertEqual(root.find(".//{*}supportedOS").get("Id").lower(), "{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}")
        spec = (ROOT / "Pruner-Studio.spec").read_text(encoding="utf-8")
        self.assertIn("manifest=str(project_root / 'build' / 'Pruner-Studio.exe.manifest')", spec)
        entry = (ROOT / "scripts/pruner_studio_entry.py").read_text(encoding="utf-8")
        self.assertLess(entry.index("\n    _configure_dpi()"), entry.index("from pruner_studio.app import"))

    def test_icon_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "cover.png"
            out = Path(tmp) / "assets"
            Image.new("RGB", (100, 60), "red").save(source)
            subprocess.run([sys.executable, str(ROOT / "scripts/build-icons.py"), "--source", str(source),
                            "--output-dir", str(out)], check=True)
            for name, size in [("icon.png", 512), ("favicon-32.png", 32), ("favicon-64.png", 64)]:
                with Image.open(out / name) as img:
                    self.assertEqual(img.size, (size, size))
                    self.assertEqual(img.getpixel((0, 0))[3], 0)
                    self.assertEqual(img.getpixel((size//2, size//2)), (255, 0, 0, 255))
            with Image.open(out / "icon.ico") as ico:
                self.assertEqual(ico.ico.sizes(), {(n,n) for n in (16,24,32,48,64,128,256)})
                for size in ico.ico.sizes():
                    self.assertEqual(ico.ico.getimage(size).size, size)

    def test_theme_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SettingsStore(JsonStore(Path(tmp)))
            for key, choices in [("appearance", "dark midnight light nordic espresso emerald dynamic auto"),
                                 ("stage_theme", "fluid vinyl poster visualizer"), ("visualizer_mode", "bars ring wave3d"),
                                 ("gpu_performance_mode", "eco balanced high")]:
                for value in choices.split():
                    obj = {"theme": value} if key == "appearance" else value
                    store.save({key: obj})
                    persisted = store.load()[key]
                    self.assertEqual(persisted["theme"] if key == "appearance" else persisted, value)
                with self.assertRaises(AppError):
                    store.save({key: {"theme": "missing"} if key == "appearance" else "missing"})
