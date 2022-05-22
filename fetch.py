#!/usr/bin/env python3
import json
import operator
import os
import re
from copy import deepcopy
from functools import reduce
from urllib.parse import urljoin

import httpx
import jq
import toml

# App defines
_CONFIG_DIR = "config"
_CONFIG_EXT = ".toml"
_SECRET_EXT = ".secrets"
_RESULT_DIR = "data"
_RESULT_EXT = ".json"

# Magic strings
_MAGIC_JQ = "!jq"
_MAGIC_SECRET = "!secret"

# Global state
data = {}


def map_nested(obj, f):
    if isinstance(obj, dict):
        iterator = obj.items()
    elif isinstance(obj, list):
        iterator = enumerate(obj)
    for k, v in iterator:
        if isinstance(v, dict) or isinstance(v, list):
            map_nested(v, f)
        else:
            obj[k] = f(v)


def find(element, d):
    return reduce(operator.getitem, element.split("."), d)


def load_secrets():
    secrets = {}
    secrets_file = os.path.join(_CONFIG_DIR, f"{_SECRET_EXT}{_CONFIG_EXT}")
    if os.path.isfile(secrets_file):
        with open(secrets_file) as fd:
            secrets = toml.load(fd)
    return secrets


def load_unit(filepath):
    with open(filepath, "r") as fd:
        unit = toml.load(fd)
        if not "headers" in unit["api"]:
            unit["api"]["headers"] = {"content-type": "application/json"}
        return unit


# Uncover secrets
def reveal(unit, secrets):
    def replace_secret(v):
        if isinstance(v, str) and _MAGIC_SECRET in v:
            key = v.replace(_MAGIC_SECRET, "").strip()
            return find(key, secrets)
        return v

    revealed = deepcopy(unit)
    map_nested(revealed, replace_secret)
    return revealed


# Load configured units
def init():
    secrets = load_secrets()
    for filename in os.listdir(_CONFIG_DIR):
        if filename.endswith(_CONFIG_EXT) and _SECRET_EXT not in filename:
            filepath = os.path.join(_CONFIG_DIR, filename)
            unitname = filename.replace(_CONFIG_EXT, "")
            unitdata = load_unit(filepath)
            yield unitname, reveal(unitdata, secrets)


# Fetch API endpoint
def fetch(name, api, action):
    global data
    # TODO check if data is already cached

    # Construct request
    request = {"url": urljoin(api["base"], action["endpoint"])}
    if "params" in api:
        request["params"] = api["params"]
    if "headers" in api:
        request["headers"] = api["headers"]

    # Send request
    if action["method"] == "post":
        # TODO refactor preprocessing into own function, apply also to params and headers
        if isinstance(action["json"], str) and action["json"].startswith(_MAGIC_JQ):
            action["json"] = jq.all(action["json"][len(_MAGIC_JQ) :].strip(), data)
        response = httpx.post(json=action["json"], **request)
    else:
        response = httpx.get(**request)

    # Handle response
    response.raise_for_status()
    data[name] = response.json()
    return data[name]


# Run
def main():
    for name, unit in init():
        for action_id in unit["api"]["flow"]:
            action = unit["action"][action_id]
            response = fetch(action_id, unit["api"], action)

            # Save results as a static json file
            data_dir = os.path.join(_RESULT_DIR, name)
            if not os.path.exists(data_dir):
                os.makedirs(data_dir)
            with open(os.path.join(data_dir, f"{action_id}{_RESULT_EXT}"), "w") as fd:
                print(f"{name}: {action_id}")
                json.dump(response, fd, indent=4, sort_keys=True)


if __name__ == "__main__":
    main()
