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

"""Generic GoogleAdsService.Mutate tool guarded by the propose-and-confirm workflow.

Covers changes the purpose-built tools don't (budgets, bidding, campaign and
ad group settings, ads, keywords, negatives, targeting, asset links, and more)
by accepting MutateOperation payloads in the Google Ads REST JSON format.
"""

import json
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.protobuf import json_format
from google.protobuf.message import Message as PbMessage
from mcp.types import ToolAnnotations

from ads_mcp import propose_and_confirm as pac
import ads_mcp.utils as utils

googleads_mcp = FastMCP("googleads")

MAX_OPERATIONS = 1000

_ACTIONS = {"create": "CREATE", "update": "UPDATE", "remove": "REMOVE"}


def _entity_name(operation_field: str) -> str:
    """'campaign_budget_operation' -> 'CampaignBudget'."""
    base = operation_field.removesuffix("_operation")
    return "".join(part.capitalize() for part in base.split("_"))


def _to_readable(value: Any) -> Any:
    if isinstance(value, PbMessage):
        return json_format.MessageToDict(
            value, preserving_proto_field_name=True
        )
    if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
        return [_to_readable(v) for v in value]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def _field_value(message: PbMessage, path: str) -> Any:
    """Reads a dotted snake_case field path from a protobuf message."""
    current: Any = message
    for part in path.split("."):
        try:
            current = getattr(current, part)
        except AttributeError:
            return None
    if isinstance(current, PbMessage) or (
        hasattr(current, "DESCRIPTOR") and hasattr(current, "ListFields")
    ):
        return _to_readable(current)
    if isinstance(current, int) and not isinstance(current, bool):
        # Enum fields come back as ints; resolve the name when possible.
        return _enum_name(message, path, current)
    return _to_readable(current)


def _enum_name(message: PbMessage, path: str, value: int) -> Any:
    descriptor = message.DESCRIPTOR
    field = None
    for part in path.split("."):
        field = descriptor.fields_by_name.get(part) if descriptor else None
        if field is None:
            return value
        descriptor = field.message_type
    if field is not None and field.enum_type is not None:
        enum_value = field.enum_type.values_by_number.get(value)
        if enum_value is not None:
            return enum_value.name
    return value


def _describe_operation(mutate_op_pb: PbMessage) -> list[pac.ChangeItem]:
    """Builds human-readable change items for one MutateOperation."""
    op_field = mutate_op_pb.WhichOneof("operation")
    if not op_field:
        raise ToolError(
            "Error: each operation must set exactly one resource operation, "
            'e.g. {"campaignBudgetOperation": {...}}.'
        )
    entity = _entity_name(op_field)
    resource_op = getattr(mutate_op_pb, op_field)
    action_field = resource_op.WhichOneof("operation")
    if action_field not in _ACTIONS:
        raise ToolError(
            f"Error: {op_field} must contain one of 'create', 'update' or 'remove'."
        )
    action = _ACTIONS[action_field]

    if action == "REMOVE":
        return pac.build_change_items(
            operation=action,
            entity_readable=entity,
            primary_key_readable=resource_op.remove,
            fields={"Resource Name": resource_op.remove},
        )

    target = getattr(resource_op, action_field)
    resource_name = getattr(target, "resource_name", "") or ""

    if action == "UPDATE":
        paths = list(resource_op.update_mask.paths)
        if not paths:
            raise ToolError(
                f"Error: the {entity} update needs an updateMask listing the "
                "fields to change (comma-separated camelCase paths)."
            )
        fields = {path: _field_value(target, path) for path in paths}
        # build_change_items skips None values; keep cleared fields visible.
        fields = {k: ("" if v is None else v) for k, v in fields.items()}
    else:
        fields = json_format.MessageToDict(
            target, preserving_proto_field_name=True
        )

    return pac.build_change_items(
        operation=action,
        entity_readable=entity,
        primary_key_readable=resource_name or "(new)",
        fields=fields,
    )


def _response_summary(response: Any) -> dict[str, Any]:
    results = []
    for item in response.mutate_operation_responses:
        item_pb = (
            type(item).pb(item) if not isinstance(item, PbMessage) else item
        )
        which = item_pb.WhichOneof("response")
        resource_name = (
            getattr(getattr(item_pb, which), "resource_name", "")
            if which
            else ""
        )
        results.append(
            {
                "result": (
                    _entity_name(which.removesuffix("_result"))
                    if which
                    else None
                ),
                "resource_name": resource_name or None,
            }
        )
    summary: dict[str, Any] = {"status": "COMMITTED", "results": results}
    partial = getattr(response, "partial_failure_error", None)
    if partial is not None and getattr(partial, "code", 0):
        summary["partial_failure_error"] = getattr(partial, "message", "")
    return summary


@googleads_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def mutate(
    customer_id: str | int,
    operations: list[dict[str, Any]],
    partial_failure: bool = False,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates, updates, or removes any Google Ads resources in one request (GoogleAdsService.Mutate).

    Use this for changes the purpose-built tools don't cover: budgets, bidding
    strategies, campaign and ad group settings and status, ads, keywords and
    negative keywords, shared negative lists, location and other targeting
    criteria, asset links (customer/campaign/ad group/asset group assets),
    conversion actions, and so on. Operations are applied atomically unless
    `partial_failure` is True.

    Each item of `operations` is a MutateOperation in Google Ads REST JSON
    (camelCase or snake_case field names), keyed by exactly one resource
    operation that holds `create`, `update` (plus `updateMask`), or `remove`.
    Examples:
      {"campaignBudgetOperation": {"update": {"resourceName": "customers/1234567890/campaignBudgets/111", "amountMicros": "35000000"}, "updateMask": "amountMicros"}}
      {"campaignOperation": {"update": {"resourceName": "customers/1234567890/campaigns/222", "status": "PAUSED"}, "updateMask": "status"}}
      {"adGroupCriterionOperation": {"create": {"adGroup": "customers/1234567890/adGroups/333", "keyword": {"text": "used cars", "matchType": "PHRASE"}, "status": "ENABLED"}}}
      {"campaignCriterionOperation": {"create": {"campaign": "customers/1234567890/campaigns/222", "negative": true, "keyword": {"text": "free", "matchType": "BROAD"}}}}
      {"adGroupAdOperation": {"remove": "customers/1234567890/adGroupAds/333~444"}}
    Money values are in micros (1,000,000 = 1 unit of account currency) and
    int64 values may be given as strings. `updateMask` is a comma-separated
    list of camelCase field paths. Temporary IDs (negative numbers in resource
    names, e.g. 'customers/1234567890/campaignBudgets/-1') let new resources
    reference each other within one request.

    Propose & Confirm Workflow (`validate_only` and `checksum`):
    1. Call `mutate` with `validate_only=True` (the default). The Google Ads API
       validates the request without applying it, and the tool returns
       structured `change_items` and a `checksum`.
    2. Present the `change_items` to the user and wait for explicit user approval.
    3. Only after user confirmation, call `mutate` with `validate_only=False`
       along with the exact same parameters and the `checksum` returned by the
       previous `validate_only=True` call.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated, e.g. '1234567890').
        operations: MutateOperation objects in REST JSON form (1 to 1000).
        partial_failure: If True, valid operations are applied even if others fail. Default False (all or nothing).
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
        validate_only: If True (default), validates the request and returns proposed `change_items` and a `checksum` without changing the account. If False, requires `checksum` from a prior `validate_only=True` call to commit the mutation.
        checksum: Checksum of the validated request returned by a prior `validate_only=True` call. Required when `validate_only=False`.
    """
    cleaned_customer_id = utils.clean_customer_id(customer_id)
    if not cleaned_customer_id:
        raise ToolError("Error: 'customer_id' must contain digits.")
    if not operations:
        raise ToolError(
            "Error: 'operations' must contain at least one operation."
        )
    if len(operations) > MAX_OPERATIONS:
        raise ToolError(
            f"Error: at most {MAX_OPERATIONS} operations per call; split the request."
        )

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    googleads_service = utils.get_googleads_service_from_client(
        ga_client, "GoogleAdsService"
    )

    mutate_ops = []
    change_items: list[pac.ChangeItem] = []
    for index, raw in enumerate(operations):
        if not isinstance(raw, dict):
            raise ToolError(
                f"Error: operations[{index}] must be a JSON object."
            )
        mutate_op = ga_client.get_type("MutateOperation")
        mutate_op_pb = type(mutate_op).pb(mutate_op)
        try:
            json_format.ParseDict(raw, mutate_op_pb)
        except json_format.ParseError as exc:
            raise ToolError(
                f"Error: operations[{index}] is not a valid MutateOperation: {exc}"
            ) from exc
        change_items.extend(_describe_operation(mutate_op_pb))
        mutate_ops.append(mutate_op)

    request = ga_client.get_type("MutateGoogleAdsRequest")
    request.customer_id = cleaned_customer_id
    request.mutate_operations.extend(mutate_ops)
    request.partial_failure = partial_failure

    # Bound to the parsed protos, so camelCase vs snake_case spellings of the
    # same change produce the same checksum, while any real difference doesn't.
    operation_payload = [
        {"tool": "mutate", "partial_failure": partial_failure},
        *mutate_ops,
    ]

    def _validate() -> None:
        request.validate_only = True
        googleads_service.mutate(request=request)

    def _commit() -> str:
        request.validate_only = False
        response = googleads_service.mutate(request=request)
        return json.dumps(_response_summary(response), indent=2)

    return pac.propose_and_confirm(
        customer_id=cleaned_customer_id,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=operation_payload,
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to mutate",
    )
