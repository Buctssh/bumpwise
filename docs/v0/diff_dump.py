"""Parse every documented example response through the client and dump the parsed models.

Run once with the v1 checkout and once with the v2 checkout, then compare the two JSON files.
Usage: python diff_dump.py <repo_root> <out.json>
"""
import json
import sys
from pathlib import Path

repo, out = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(repo))

import requests_mock  # noqa: E402
from pydantic import BaseModel, VERSION  # noqa: E402

from cortex_xdr_client.api.authentication import Authentication  # noqa: E402
from cortex_xdr_client.client import CortexXDRClient  # noqa: E402
import tests.test_versions as tv  # noqa: E402

params = next(m for m in tv.test_current_core_examples.pytestmark if m.name == "parametrize").args[1]


def dump(obj):
    if isinstance(obj, BaseModel):
        data = obj.model_dump() if VERSION.startswith("2") else obj.dict()
    else:
        data = obj
    return json.loads(json.dumps(data, default=str, sort_keys=True))


results = {}
for version in (3, 5):
    client = CortexXDRClient(Authentication(api_key="k", api_key_id=1), "tenant.example.com", api_version=version)
    for api, method, call, args, _ in params:
        path = "/public_api/v1/" + api + "/" + call
        for example, response in tv.RESPONSES[str(version)].get(path, {}).items():
            key = f"v{version} {api}.{method} [{example}]"
            with requests_mock.Mocker() as m:
                m.post(tv.ORIGIN + path, json=response)
                try:
                    results[key] = {"ok": dump(getattr(getattr(client, api + "_api"), method)(*args))}
                except Exception as e:  # record, don't stop
                    results[key] = {"error": f"{type(e).__name__}: {str(e).splitlines()[0]}"}

out.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"pydantic {VERSION}: {len(results)} examples, {sum('error' in r for r in results.values())} errors -> {out}")
