"""Real-D1 accuracy: run the production pipeline on the D1 source files and compare with samples/manifest.json.

Run from ocr-service/:
    python tools/d1_accuracy_report.py [--manifest ../samples/manifest.json] [--dpi 150] [--timeout 120]
                                       [--json report.json] [--keep-workdir]

Every file in the manifest is processed independently (digital and scan variants share a doc_id but get unique runtime ids)
through the existing render -> OCR -> classifier -> field-extractor code. Source files and the manifest are only read.
Recorded C1 fixtures are never used here. Nothing is claimed for a sample that failed; unsupported fields are reported as
NOT EVALUATED, not as mistakes.

Exit code: 0 every expected field matched; 1 some mismatch / missing / not evaluated field;
2 manifest missing or unusable (nothing was run); 3 at least one sample failed (ERROR / UNAVAILABLE).
OCR engine choice follows OCR_ENGINE / TESSERACT_CMD like the service (see README).
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import d1_eval  # noqa: E402
from app.config import REPO_ROOT  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", default=os.environ.get("D1_MANIFEST") or str(REPO_ROOT / "samples" / "manifest.json"))
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--timeout", type=float, default=120.0, help="per-analyzer timeout in seconds (first PaddleOCR use is slow)")
    ap.add_argument("--json", help="also write the full result as JSON to this path")
    ap.add_argument("--keep-workdir", action="store_true", help="keep the temporary evidence/derived folders for inspection")
    args = ap.parse_args(argv)

    path = Path(args.manifest)
    print(f"manifest: {path.resolve()}")
    if not path.is_file():
        print("D1 ACCURACY NOT COMPUTED: the manifest was not found (use --manifest or D1_MANIFEST). Nothing was run.")
        return 2
    manifest = d1_eval.parse_d1_manifest(path)
    if not manifest.samples:
        print("D1 ACCURACY NOT COMPUTED: the manifest lists no usable samples.")
        for p in manifest.problems:
            print(f"  - {p}")
        return 2
    print(f"samples: {len(manifest.samples)}   OCR_ENGINE={os.environ.get('OCR_ENGINE', 'auto')}   dpi={args.dpi}\n")
    work = tempfile.mkdtemp(prefix="d1-eval-") if args.keep_workdir else None
    with tempfile.TemporaryDirectory(prefix="d1-eval-") as tmp:
        folder = Path(work or tmp)
        results = d1_eval.evaluate(manifest, folder, args.dpi, args.timeout)
        if work:
            print(f"work directory kept: {work}\n")
    print(d1_eval.format_report(manifest, results))
    ok = sum(r.status == d1_eval.STATUS_OK for r in results)
    print(f"\nSAMPLES PROCESSED SUCCESSFULLY: {ok} of {len(results)}")
    if args.json:
        Path(args.json).write_text(json.dumps(d1_eval.to_jsonable(manifest, results), indent=2), encoding="utf-8")
        print(f"JSON written to {args.json}")
    if ok < len(results):
        return 3
    t = d1_eval.tally(results)
    return 0 if t.expected and t.matched == t.expected else 1


if __name__ == "__main__":
    sys.exit(main())
