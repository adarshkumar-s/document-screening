import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "ocr_benchmark.py"
SPEC = importlib.util.spec_from_file_location("ocr_benchmark", MODULE_PATH)
ocr_benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ocr_benchmark)


class OCRBenchmarkTests(unittest.TestCase):
    def test_normalization_handles_spacing_and_unicode_compatibility(self):
        self.assertEqual(ocr_benchmark.normalize("  SURVEY   １２/３ "), "survey 12/3")

    def test_missing_fields_reduce_coverage_and_accuracy(self):
        report = ocr_benchmark.evaluate([{
            "document_id": "synthetic-1",
            "expected": {"survey_number": "12/3", "village": "Rampur"},
            "predicted": {"survey_number": "12/3"},
        }])
        self.assertEqual(report["documents"], 1)
        self.assertEqual(report["field_coverage"], 0.5)
        self.assertEqual(report["exact_match_accuracy"], 0.5)
        self.assertEqual(len(report["mismatches"]), 1)

    def test_indic_text_and_character_similarity(self):
        self.assertEqual(ocr_benchmark.normalize(" रामपुर "), "रामपुर")
        self.assertEqual(ocr_benchmark.similarity("रामपुर", "रामपुर"), 1.0)
        self.assertLess(ocr_benchmark.similarity("रामपुर", "रामपुरा"), 1.0)

    def test_invalid_row_is_rejected(self):
        with self.assertRaises(ValueError):
            ocr_benchmark.evaluate([{"expected": [], "predicted": {}}])


if __name__ == "__main__":
    unittest.main()
