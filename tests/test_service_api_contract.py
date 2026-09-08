from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from server.layers import SERVICE_API_VERSION  # noqa: E402
from server.mcp_server import KnowledgeService, handle_message  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
TODAY = dt.date(2026, 9, 7)


def service(**kwargs) -> KnowledgeService:
    return KnowledgeService(config=support.build_config(**kwargs), today=TODAY)


class ServiceApiContractTests(unittest.TestCase):
    def test_service_api_json_matches_advertised_version(self) -> None:
        contract = json.loads((ROOT / "service-api.json").read_text())
        advertised = int(float(SERVICE_API_VERSION))
        self.assertEqual(contract["schema_version"], 1)
        self.assertEqual(contract["name"], "vaws-knowledge")
        self.assertIn(contract["service_api_version"], contract["supports"])
        self.assertEqual(contract["service_api_version"], advertised)

        result = handle_message(service(), {"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]
        self.assertEqual(
            result["capabilities"]["experimental"]["vaws-knowledge"]["service_api_version"],
            advertised,
        )


if __name__ == "__main__":
    unittest.main()
