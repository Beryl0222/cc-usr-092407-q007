import json
import unittest
from pathlib import Path

from src.exhibition_product_sunset import validate_event

class ContractTest(unittest.TestCase):
    def test_sample_matches_domain_contract(self):
        record = json.loads((Path(__file__).parents[1] / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(record), [])

    def test_all_samples_match_domain_contract(self):
        data_dir = Path(__file__).parents[1] / "data"
        for path in sorted(data_dir.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(validate_event(record), [], path.name)

if __name__ == "__main__":
    unittest.main()
