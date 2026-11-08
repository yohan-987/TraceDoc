"""PaddleOCR diagnostic. Run from ocr-service/:   python tools/paddle_check.py

Prints the installed versions, then runs the SERVICE's own PaddleOCR adapter (app/ocr_paddle.py) on the three
generated sample documents with oneDNN off (the service default) and on, reporting for each: the exact error
if inference fails, the known strings found, and the detection rate (the tests require >= 80%).
Exit code 0 only if the default configuration detects >= 80% on all three samples.
"""
import importlib.metadata as md
import os
import platform
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import ocr_paddle  # noqa: E402
from tests import samples  # noqa: E402

THRESHOLD = 0.8


def versions() -> None:
    print(f"python {platform.python_version()} on {platform.platform()}")
    for pkg in ("paddlepaddle", "paddleocr", "paddlex", "numpy", "opencv-python", "opencv-contrib-python", "pillow"):
        try:
            print(f"  {pkg}=={md.version(pkg)}")
        except md.PackageNotFoundError:
            print(f"  {pkg}: not installed")


def run(label: str, mkldnn: bool, folder: Path) -> bool:
    print(f"\n=== {label} (PADDLE_ENABLE_MKLDNN={int(mkldnn)}) ===")
    os.environ["PADDLE_ENABLE_MKLDNN"] = "1" if mkldnn else "0"
    ocr_paddle.reset_paddle()
    _, status = ocr_paddle.get_paddle()
    print("init:", "OK, PaddleOCR " + str(status.version) if status.available else f"FAILED: {status.reason}")
    if not status.available:
        return False
    ok_all = True
    for name, make in (("id", samples.id_card), ("form", samples.form), ("statement", samples.statement)):
        data, known = make()
        path = folder / f"{name}.png"
        path.write_bytes(data)
        try:
            page = ocr_paddle.ocr_image_file(path)
        except Exception as exc:  # noqa: BLE001 - report everything
            print(f"{name}: inference FAILED -> {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=4)
            ok_all = False
            continue
        texts = [w["text"] for w in page["words"]]
        rate = samples.detection_rate(texts, known)
        haystack = "".join(texts).lower().replace(" ", "")
        missing = [k for k in known if k.lower().replace(" ", "") not in haystack]
        print(f"{name}: {rate:.0%} ({len(page['words'])} words, {len(page['lines'])} lines) missing={missing}")
        ok_all = ok_all and rate >= THRESHOLD
    return ok_all


if __name__ == "__main__":
    versions()
    with tempfile.TemporaryDirectory() as tmp:
        default_ok = run("service default", False, Path(tmp))
        run("oneDNN enabled (expected to FAIL on PaddlePaddle 3.3.x, x86-64 CPU)", True, Path(tmp))
    print("\nRESULT:", "PaddleOCR (oneDNN off) reaches the threshold on all samples" if default_ok
          else "PaddleOCR (oneDNN off) does NOT reach the threshold: send this output")
    sys.exit(0 if default_ok else 1)
