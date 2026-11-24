# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test cases for the generic googleads mutate tool."""

import json
import unittest
from unittest.mock import MagicMock, create_autospec, patch

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.v25.services.services.google_ads_service import (
    GoogleAdsServiceClient,
)
from google.auth.credentials import AnonymousCredentials

from ads_mcp.tools import mutate as mutate_tool

BUDGET_UPDATE = {
    "campaignBudgetOperation": {
        "update": {
            "resourceName": "customers/1234567890/campaignBudgets/111",
            "amountMicros": "35000000",
        },
        "updateMask": "amountMicros",
    }
}
BUDGET_UPDATE_SNAKE = {
    "campaign_budget_operation": {
        "update": {
            "resource_name": "customers/1234567890/campaignBudgets/111",
            "amount_micros": 35000000,
        },
        "update_mask": "amountMicros",
    }
}
PAUSE_CAMPAIGN = {
    "campaignOperation": {
        "update": {
            "resourceName": "customers/1234567890/campaigns/222",
            "status": "PAUSED",
        },
        "updateMask": "status",
    }
}
NEGATIVE_KEYWORD = {
    "campaignCriterionOperation": {
        "create": {
            "campaign": "customers/1234567890/campaigns/222",
            "negative": True,
            "keyword": {"text": "free cars", "matchType": "PHRASE"},
        }
    }
}
REMOVE_AD = {
    "adGroupAdOperation": {"remove": "customers/1234567890/adGroupAds/333~444"}
}


class TestMutate(unittest.TestCase):
    """Test cases for googleads_mutate."""

    @classmethod
    def setUpClass(cls):
        cls.real_ga_client = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="fake-token",
            use_proto_plus=True,
        )

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_service = create_autospec(
            GoogleAdsServiceClient, instance=True
        )
        self.mock_client.get_service.return_value = self.mock_service
        self.mock_client.get_type.side_effect = self.real_ga_client.get_type

        response = self.real_ga_client.get_type("MutateGoogleAdsResponse")
        r1 = self.real_ga_client.get_type("MutateOperationResponse")
        r1.campaign_budget_result.resource_name = (
            "customers/1234567890/campaignBudgets/111"
        )
        response.mutate_operation_responses.append(r1)
        self.mock_service.mutate.return_value = response

        patcher = patch("ads_mcp.utils.get_googleads_client")
        self.mock_get_client = patcher.start()
        self.mock_get_client.return_value = self.mock_client
        self.addCleanup(patcher.stop)

    def _last_request(self):
        return self.mock_service.mutate.call_args.kwargs["request"]

    def test_propose_then_commit(self):
        """Propose validates via the API; commit needs the checksum and applies."""
        proposal = json.loads(
            mutate_tool.mutate(
                customer_id="123-456-7890", operations=[BUDGET_UPDATE]
            )
        )
        self.assertEqual(proposal["status"], "PROPOSED")
        self.assertEqual(proposal["customer_id"], "1234567890")
        item = proposal["change_items"][0]
        self.assertEqual(item["operation"], "UPDATE")
        self.assertEqual(item["entity_readable"], "CampaignBudget")
        self.assertEqual(
            item["primary_key_readable"],
            "customers/1234567890/campaignBudgets/111",
        )
        self.assertEqual(item["field_readable"], "amount_micros")
        self.assertEqual(item["value_readable"], "35000000")

        request = self._last_request()
        self.assertTrue(request.validate_only)
        self.assertEqual(request.customer_id, "1234567890")
        self.assertEqual(
            request.mutate_operations[
                0
            ].campaign_budget_operation.update.amount_micros,
            35000000,
        )

        committed = json.loads(
            mutate_tool.mutate(
                customer_id="1234567890",
                operations=[BUDGET_UPDATE],
                validate_only=False,
                checksum=proposal["checksum"],
            )
        )
        self.assertEqual(committed["status"], "COMMITTED")
        self.assertEqual(
            committed["results"][0],
            {
                "result": "CampaignBudget",
                "resource_name": "customers/1234567890/campaignBudgets/111",
            },
        )
        self.assertFalse(self._last_request().validate_only)

    def test_commit_requires_matching_checksum(self):
        """Commit is refused without a checksum or with one for different operations."""
        proposal = json.loads(
            mutate_tool.mutate(
                customer_id="1234567890", operations=[BUDGET_UPDATE]
            )
        )
        self.mock_service.mutate.reset_mock()

        with self.assertRaisesRegex(ToolError, "checksum"):
            mutate_tool.mutate(
                customer_id="1234567890",
                operations=[BUDGET_UPDATE],
                validate_only=False,
            )
        with self.assertRaisesRegex(ToolError, "mismatched checksum"):
            mutate_tool.mutate(
                customer_id="1234567890",
                operations=[PAUSE_CAMPAIGN],
                validate_only=False,
                checksum=proposal["checksum"],
            )
        with self.assertRaisesRegex(ToolError, "mismatched checksum"):
            mutate_tool.mutate(
                customer_id="1234567890",
                operations=[BUDGET_UPDATE],
                partial_failure=True,
                validate_only=False,
                checksum=proposal["checksum"],
            )
        self.mock_service.mutate.assert_not_called()

    def test_checksum_ignores_json_spelling(self):
        """camelCase and snake_case spellings of the same change share a checksum."""
        a = json.loads(
            mutate_tool.mutate(
                customer_id="1234567890", operations=[BUDGET_UPDATE]
            )
        )
        b = json.loads(
            mutate_tool.mutate(
                customer_id="1234567890", operations=[BUDGET_UPDATE_SNAKE]
            )
        )
        self.assertEqual(a["checksum"], b["checksum"])

    def test_change_items_for_create_update_enum_and_remove(self):
        """Readable items for creates, enum updates and removes."""
        proposal = json.loads(
            mutate_tool.mutate(
                customer_id="1234567890",
                operations=[PAUSE_CAMPAIGN, NEGATIVE_KEYWORD, REMOVE_AD],
            )
        )
        items = proposal["change_items"]
        pause = items[0]
        self.assertEqual(
            (
                pause["operation"],
                pause["entity_readable"],
                pause["value_readable"],
            ),
            ("UPDATE", "Campaign", "PAUSED"),
        )
        creates = [i for i in items if i["operation"] == "CREATE"]
        self.assertTrue(
            all(i["entity_readable"] == "CampaignCriterion" for i in creates)
        )
        self.assertIn("keyword", [i["field_readable"] for i in creates])
        self.assertEqual(creates[0]["primary_key_readable"], "(new)")
        remove = items[-1]
        self.assertEqual(
            (
                remove["operation"],
                remove["entity_readable"],
                remove["primary_key_readable"],
            ),
            ("REMOVE", "AdGroupAd", "customers/1234567890/adGroupAds/333~444"),
        )

    def test_rejects_bad_operations(self):
        """Invalid payloads fail before any API call."""
        cases = [
            ([], "at least one"),
            ([{"noSuchOperation": {}}], "not a valid MutateOperation"),
            ([{}], "exactly one resource operation"),
            (
                [
                    {
                        "campaignOperation": {
                            "update": {
                                "resourceName": "customers/1/campaigns/2",
                                "name": "x",
                            }
                        }
                    }
                ],
                "updateMask",
            ),
            (["not an object"], "must be a JSON object"),
        ]
        for operations, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, message):
                    mutate_tool.mutate(
                        customer_id="1234567890", operations=operations
                    )
        self.mock_service.mutate.assert_not_called()

    def test_api_validation_error_blocks_proposal(self):
        """When the API rejects the dry run, no checksum is issued."""
        self.mock_service.mutate.side_effect = RuntimeError("bad budget")
        with self.assertRaisesRegex(ToolError, "Failed to mutate: bad budget"):
            mutate_tool.mutate(
                customer_id="1234567890", operations=[BUDGET_UPDATE]
            )


if __name__ == "__main__":
    unittest.main()
