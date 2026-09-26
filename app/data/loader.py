import json
from pathlib import Path


class DataLoader:
    """
    Loads the challenge dataset.

    Supports both layouts shipped with the challenge:
      - seed layout:     merchants_seed.json / customers_seed.json / triggers_seed.json
      - expanded layout: merchants/*.json, customers/*.json, triggers/*.json
                         (output of dataset/generate_dataset.py)
    Categories are always categories/<slug>.json.
    """

    def __init__(self, dataset_dir=None):
        self.root = Path(__file__).resolve().parents[2]
        self.dataset_path = Path(dataset_dir) if dataset_dir else self.root / "dataset"
        self._category_cache = {}

    def load_json(self, filename):
        path = self.dataset_path / filename
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    def _load_collection(self, seed_file, key, folder):
        folder_path = self.dataset_path / folder
        if folder_path.is_dir():
            items = []
            for path in sorted(folder_path.glob("*.json")):
                with open(path, "r", encoding="utf-8") as file:
                    items.append(json.load(file))
            return items
        return self.load_json(seed_file)[key]

    def load_customers(self):
        return self._load_collection("customers_seed.json", "customers", "customers")

    def load_merchants(self):
        return self._load_collection("merchants_seed.json", "merchants", "merchants")

    def load_triggers(self):
        return self._load_collection("triggers_seed.json", "triggers", "triggers")

    def load_category(self, category_slug):
        if category_slug not in self._category_cache:
            path = self.dataset_path / "categories" / f"{category_slug}.json"
            with open(path, "r", encoding="utf-8") as file:
                self._category_cache[category_slug] = json.load(file)
        return self._category_cache[category_slug]
