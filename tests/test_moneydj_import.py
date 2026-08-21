import importlib.util
import tempfile
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).parents[1] / "src" / "import_moneydj.py"
SPEC = importlib.util.spec_from_file_location("import_moneydj", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MoneyDJImportTest(unittest.TestCase):
    def test_csv_normalizes_dates_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nav.csv"
            path.write_text("基金名稱,測試基金\n淨值日期,基金淨值\n2026/08/11,10.5\n2026/08/12,10.7\n2026/08/12,10.8\n", encoding="utf-8")
            rows = MODULE.normalize_nav(path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1][1], 10.8)

    def test_missing_headers_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.csv"
            path.write_text("foo,bar\n1,2\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                MODULE.normalize_nav(path)


if __name__ == "__main__":
    unittest.main()
