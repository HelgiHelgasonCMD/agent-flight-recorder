import json


def load(path="config.json"):
    with open(path) as fh:
        return json.load(fh)
