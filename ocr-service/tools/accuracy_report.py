"""Per-field accuracy of the field extractor against samples/manifest.json, using the recorded OCR fixtures.

Run from ocr-service/:   python tools/accuracy_report.py
Exit code: 0 every compared field matched, 1 at least one mismatch, 2 the comparison could not be performed
(manifest or fixtures missing). Nothing is invented: without a manifest and fixtures it only says what is missing.
Paths: SAMPLES_MANIFEST (default samples/manifest.json, samples/dev/manifest.json), OCR_FIXTURES_DIR
(default contracts/fixtures/ocr).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import extract_report as rep  # noqa: E402
from app.ocr_fixtures import fixtures_dir  # noqa: E402


def main() -> int:
    manifest, fixtures = rep.find_manifest(), fixtures_dir()
    print(f"manifest: {manifest or 'NOT FOUND'}\nfixtures: {fixtures or 'NOT FOUND'}\n")
    comparison, blockers = rep.accuracy_from_manifest(manifest, fixtures)
    if comparison is None:
        print("ACCURACY NOT COMPUTED:")
        for b in blockers:
            print(f"  - {b}")
        return 2
    print(rep.format_report(comparison, "ACCURACY vs samples/manifest.json", blockers))
    return 0 if comparison.rows and all(r.match for r in comparison.rows) else 1


if __name__ == "__main__":
    sys.exit(main())
