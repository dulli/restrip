#!/usr/bin/env python3
import json
import operator
import os
import time
from collections import defaultdict
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
_MAXAGE_DEF = 86400

# Magic keys
_MAXAGE_KEY = "maxage"

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


def prepare(request):
    def eval_jq(v):
        if isinstance(v, str) and v.startswith(_MAGIC_JQ):
            v = jq.all(v[len(_MAGIC_JQ) :].strip(), data)
            if len(v) == 1:
                return v[0]
            return v
        return v

    prepared = deepcopy(request)
    map_nested(prepared, eval_jq)
    return prepared


# Load configured units
def init(targets=[]):
    secrets = load_secrets()
    for filename in targets or os.listdir(_CONFIG_DIR):
        if filename.endswith(_CONFIG_EXT) and _SECRET_EXT not in filename:
            filepath = os.path.join(_CONFIG_DIR, filename)
            if not os.path.isfile(filepath):
                continue
            unitname = filename.replace(_CONFIG_EXT, "")
            unitdata = load_unit(filepath)
            yield unitname, reveal(unitdata, secrets)


# Fetch API endpoint
def fetch(name, api, action):
    global data

    # Construct request
    request = defaultdict(dict)
    request["url"] = urljoin(api["base"], action["endpoint"])
    if "params" in api:
        request["params"] = api["params"]
    if "headers" in api:
        request["headers"] = api["headers"]

    if "params" in action:
        request["params"].update(action["params"])
    if "headers" in action:
        request["headers"].update(action["headers"])

    if "json" in action:
        request["json"] = action["json"]

    page_index = 0
    while True:
        # Run pre-processing steps
        request = prepare(request)

        # Send request
        if action["method"] == "post":
            response = httpx.post(**request)
        else:
            response = httpx.get(**request)

        # Handle response
        response.raise_for_status()  # TODO error handling
        result = response.json()

        # Handle pagination
        if page_index == 0:
            data[name] = result
        if "paginate" not in action:
            break
        else:
            pagination = prepare(action["paginate"])
            print(pagination)

            if page_index > 0:
                merge = pagination["merge"]
                result[merge] = data[name][merge] + result[merge]
                data[name].update(result)
            if "increment" in pagination:
                page_index = page_index + pagination["increment"]
            else:
                page_index = page_index + 1
            if page_index >= pagination["max"]:
                break
            request["params"][pagination["param"]] = page_index
            print(f"Pagination: {page_index}/{pagination['max']}")

    return data[name]


# Retrieve API endpoint data from cache
def restore(name, cachefile):
    global data

    with open(cachefile, "r") as fd:
        data[name] = json.load(fd)
    return data[name]


# Run
def run(args):
    args = [f"{target}.toml" for target in args] if args else []
    for name, unit in init(args):
        actions = unit["api"]["flow"]
        data_dir = os.path.join(_RESULT_DIR, name)

        for idx, action_id in enumerate(actions):
            action = unit["action"][action_id]
            outfile = os.path.join(data_dir, f"{action_id}{_RESULT_EXT}")

            # Check for cached results for this endpoint
            max_age = action[_MAXAGE_KEY] if _MAXAGE_KEY in action else _MAXAGE_DEF
            if os.path.isfile(outfile):
                cache_age = time.time() - os.path.getmtime(outfile)
                if cache_age <= max_age:
                    if idx == len(actions):
                        continue  # move on directly if this is the last action

                    print(f"Restore {name}: {action_id} ({cache_age}/{max_age})")
                    response = restore(action_id, outfile)
                    continue
                else:
                    print(f"Outdated {name}: {action_id} ({cache_age}/{max_age})")

            # If we didn't continue with a valid cache above, create one
            print(f"Fetch {name}: {action_id}")
            response = fetch(action_id, unit["api"], action)

            # Save results as a static json file
            if not os.path.exists(data_dir):
                os.makedirs(data_dir)
            with open(outfile, "w") as fd:
                print(f"Cache {name}: {action_id}")
                json.dump(response, fd, indent=4, sort_keys=True)


if __name__ == "__main__":
    import sys

    run(sys.argv[1:])
