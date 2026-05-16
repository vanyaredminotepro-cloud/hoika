from pathlib import Path

try:
    from PIL import Image, ImageFilter
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Pillow is required. Install with: pip install pillow"
    ) from exc

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "sources" / "malokonstantinovka_reference.png"
MAP = ROOT / "map"
MAP.mkdir(exist_ok=True)

if not SRC.exists():
    raise SystemExit(f"Missing source image: {SRC}")

img = Image.open(SRC).convert("RGB")
prov = img.filter(ImageFilter.SHARPEN)
prov.save(MAP / "provinces.bmp")
terrain = img.convert("P", palette=Image.ADAPTIVE, colors=16).convert("RGB")
terrain.save(MAP / "terrain.bmp")
height = img.convert("L")
height.save(MAP / "heightmap.bmp")
rivers = Image.new("RGB", img.size, (0, 0, 0))
rivers.save(MAP / "rivers.bmp")
print("Generated map assets in", MAP)
