"""
Tests for the coffee-orders durable function.

Uses DurableFunctionTestRunner for local testing as recommended by the
AWS Lambda durable functions documentation:
- Verify execution results for both success and error cases
- Inspect operation execution (steps, waits, callbacks)
- Test error handling and validation
- Mock external dependencies (DynamoDB, AppSync) to keep tests fast

Testing patterns:
- runner.run(input=json_string)      → sync, for flows that complete without callbacks
- runner.run_async(input=json_string) → async, returns execution_arn
- runner.wait_for_callback(arn)       → blocks until callback is created, returns callback_id
- runner.send_callback_success(id, result_bytes) → resolves a pending callback
- runner.wait_for_result(arn)         → blocks until execution completes
"""
import os
import sys
import json
import time
import types
import pytest
from unittest.mock import MagicMock, patch, call

from conftest import BASE_DIR, load_lambda_module

# ========== MODULE SETUP ==========
# Add coffee-orders source to path so 'utils' local import resolves
_coffee_src = os.path.join(BASE_DIR, 'src', 'coffee-orders')
if _coffee_src not in sys.path:
    sys.path.insert(0, _coffee_src)

# Create mock utils module BEFORE importing lambda_function
# (matches JS test pattern: jest.mock("./utils", ...))
_mock_publish = MagicMock()
_mock_utils = types.ModuleType('utils')
_mock_utils.parse_callback_result = lambda result, ctx, oid: (
    result if isinstance(result, dict) else json.loads(result)
)
_mock_utils.get_timestamp = lambda: '2025-01-01T00:00:00.000Z'
_mock_utils.publish_to_appsync = _mock_publish
_mock_utils.convert_decimals = lambda x: x
sys.modules['utils'] = _mock_utils

# Create mock DynamoDB tables
mock_orders_table = MagicMock()
mock_config_table = MagicMock()
_mock_dynamodb = MagicMock()
_mock_dynamodb.Table.side_effect = lambda name: {
    os.environ['ORDERS_TABLE_NAME']: mock_orders_table,
    os.environ['CONFIG_TABLE_NAME']: mock_config_table,
}.get(name, MagicMock())

# Import with mocked boto3 (module-level clients get our mocks)
with patch('boto3.resource', return_value=_mock_dynamodb):
    coffee_orders = load_lambda_module('coffee_orders', 'coffee-orders')

from aws_durable_execution_sdk_python_testing.runner import DurableFunctionTestRunner
from aws_durable_execution_sdk_python.execution import InvocationStatus


# ========== FIXTURES ==========

@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset all mocks before each test for isolation."""
    mock_orders_table.reset_mock()
    mock_config_table.reset_mock()
    _mock_publish.reset_mock()
    yield


def _setup_dynamo_for_validation(config_item, previous_orders=None):
    """Configure DynamoDB mocks for the validation phase."""
    mock_orders_table.put_item.return_value = {}
    mock_config_table.get_item.return_value = (
        {'Item': config_item} if config_item else {}
    )
    mock_orders_table.query.return_value = {
        'Items': previous_orders or []
    }
    mock_orders_table.update_item.return_value = {}


def _json_input(event):
    """runner.run() expects a JSON string as input."""
    return json.dumps(event)


def _parse_result(result):
    """Test runner returns result as JSON string; parse to dict."""
    return json.loads(result.result) if isinstance(result.result, str) else result.result


# ========== EXECUTION TESTS ==========

class TestExecution:
    """Verify the durable function creates proper operations."""

    def test_should_initialize_and_create_operations(self, order_event):
        """Function should start, create steps, and produce operations."""
        _setup_dynamo_for_validation(None)

        with DurableFunctionTestRunner(handler=coffee_orders.lambda_handler) as runner:
            result = runner.run(input=_json_input(order_event), timeout=10)

        assert result is not None
        assert len(result.get_all_operations()) > 0


# ========== VALIDATION TESTS ==========

class TestValidation:
    """
    Test order validation: store status and daily limits.
    These tests complete without callbacks (validation fails early → CANCELLED).
    """

    def test_should_cancel_when_store_is_closed(self, order_event, closed_store_config):
        """Order must be CANCELLED when the store is closed."""
        _setup_dynamo_for_validation(closed_store_config)

        with DurableFunctionTestRunner(handler=coffee_orders.lambda_handler) as runner:
            result = runner.run(input=_json_input(order_event), timeout=10)

        parsed = _parse_result(result)
        assert result.status is InvocationStatus.SUCCEEDED
        assert parsed['status'] == 'CANCELLED'
        assert 'closed' in parsed['reason']

    def test_should_cancel_when_daily_limit_exceeded(self, order_event, open_store_config):
        """Order must be CANCELLED when attendee exceeds their daily limit."""
        open_store_config['maxOrdersPerAttendee'] = 2
        _setup_dynamo_for_validation(
            open_store_config,
            previous_orders=[
                {'status': 'COMPLETED'},
                {'status': 'COMPLETED'},
            ],
        )

        with DurableFunctionTestRunner(handler=coffee_orders.lambda_handler) as runner:
            result = runner.run(input=_json_input(order_event), timeout=10)

        parsed = _parse_result(result)
        assert result.status is InvocationStatus.SUCCEEDED
        assert parsed['status'] == 'CANCELLED'
        assert 'limit' in parsed['reason']

    def test_should_cancel_when_event_not_found(self, order_event):
        """Order must be CANCELLED when event config doesn't exist."""
        _setup_dynamo_for_validation(None)

        with DurableFunctionTestRunner(handler=coffee_orders.lambda_handler) as runner:
            result = runner.run(input=_json_input(order_event), timeout=10)

        parsed = _parse_result(result)
        assert result.status is InvocationStatus.SUCCEEDED
        assert parsed['status'] == 'CANCELLED'
        assert 'does not exist' in parsed['reason']

    def test_cancelled_orders_should_not_count_toward_limit(self, order_event, open_store_config):
        """Cancelled orders must be excluded from the daily limit count."""
        open_store_config['maxOrdersPerAttendee'] = 2
        _setup_dynamo_for_validation(
            open_store_config,
            previous_orders=[
                {'status': 'CANCELLED'},
                {'status': 'CANCELLED'},
                {'status': 'COMPLETED'},
            ],
        )

        # Only 1 non-cancelled order (limit=2) → passes validation → reaches callbacks
        with DurableFunctionTestRunner(handler=coffee_orders.lambda_handler) as runner:
            arn = runner.run_async(input=_json_input(order_event), timeout=30)
            # Execution reaches callback — cancel it to end cleanly
            cb_id = runner.wait_for_callback(arn, timeout=10)
            runner.send_callback_success(cb_id, json.dumps({
                'action': 'CANCEL', 'reason': 'test cleanup', 'cancelledBy': 'system',
            }).encode())
            result = runner.wait_for_result(arn, timeout=10)

        # Verify order was initialized (passed validation)
        mock_orders_table.put_item.assert_called_once()


# ========== CALLBACK TESTS ==========

class TestCallbacks:
    """
    Test the callback-based barista workflow using run_async + callback API.
    Pattern: run_async → wait_for_callback → send_callback_success → wait_for_result
    """

    def test_should_complete_order_with_accept_and_complete_callbacks(
        self, order_event, open_store_config
    ):
        """Full happy path: order placed → accepted → completed."""
        _setup_dynamo_for_validation(open_store_config)

        with DurableFunctionTestRunner(handler=coffee_orders.lambda_handler) as runner:
            arn = runner.run_async(input=_json_input(order_event), timeout=30)

            # Handle acceptance callback
            accept_cb = runner.wait_for_callback(arn, timeout=10)
            runner.send_callback_success(accept_cb, json.dumps({
                'action': 'ACCEPT',
                'baristaId': 'barista-123',
            }).encode())

            # Brief pause for execution to process and create next callback
            time.sleep(1)

            # Handle completion callback
            complete_cb = runner.wait_for_callback(arn, timeout=10)
            runner.send_callback_success(complete_cb, json.dumps({
                'action': 'COMPLETE',
                'baristaId': 'barista-456',
            }).encode())

            result = runner.wait_for_result(arn, timeout=10)

        parsed = _parse_result(result)
        assert result.status is InvocationStatus.SUCCEEDED
        assert parsed['status'] == 'COMPLETED'
        assert parsed['orderId'] == 'test-order-123'

    def test_should_cancel_order_on_cancel_callback_during_acceptance(
        self, order_event, open_store_config
    ):
        """Order is cancelled when a CANCEL callback arrives during acceptance wait."""
        _setup_dynamo_for_validation(open_store_config)

        with DurableFunctionTestRunner(handler=coffee_orders.lambda_handler) as runner:
            arn = runner.run_async(input=_json_input(order_event), timeout=30)

            accept_cb = runner.wait_for_callback(arn, timeout=10)
            runner.send_callback_success(accept_cb, json.dumps({
                'action': 'CANCEL',
                'reason': 'Customer changed mind',
                'cancelledBy': 'attendee',
            }).encode())

            result = runner.wait_for_result(arn, timeout=10)

        parsed = _parse_result(result)
        assert result.status is InvocationStatus.SUCCEEDED
        assert parsed['status'] == 'CANCELLED'
        assert parsed['cancelledBy'] == 'attendee'

    def test_should_cancel_order_on_cancel_callback_during_completion(
        self, order_event, open_store_config
    ):
        """Order is cancelled when a CANCEL callback arrives during completion wait."""
        _setup_dynamo_for_validation(open_store_config)

        with DurableFunctionTestRunner(handler=coffee_orders.lambda_handler) as runner:
            arn = runner.run_async(input=_json_input(order_event), timeout=30)

            # Accept first
            accept_cb = runner.wait_for_callback(arn, timeout=10)
            runner.send_callback_success(accept_cb, json.dumps({
                'action': 'ACCEPT',
                'baristaId': 'barista-123',
            }).encode())

            time.sleep(1)

            # Cancel during completion wait
            complete_cb = runner.wait_for_callback(arn, timeout=10)
            runner.send_callback_success(complete_cb, json.dumps({
                'action': 'CANCEL',
                'reason': 'Barista cancelled',
                'cancelledBy': 'barista',
            }).encode())

            result = runner.wait_for_result(arn, timeout=10)

        parsed = _parse_result(result)
        assert result.status is InvocationStatus.SUCCEEDED
        assert parsed['status'] == 'CANCELLED'
        assert parsed['cancelledBy'] == 'barista'


# ========== HELPER FUNCTION UNIT TESTS ==========

class TestUpdateStatusAndPublish:
    """Unit tests for the update_status_and_publish helper."""

    def test_should_update_dynamodb_with_status(self):
        coffee_orders.update_status_and_publish(
            'order-1', 'QUEUED', 'ORDER_QUEUED',
            {'attendeeId': 'att-1'},
            '2025-01-01T00:00:00.000Z',
        )

        mock_orders_table.update_item.assert_called_once()
        call_kwargs = mock_orders_table.update_item.call_args[1]
        assert call_kwargs['Key'] == {'orderId': 'order-1'}
        assert call_kwargs['ExpressionAttributeValues'][':status'] == 'QUEUED'

    def test_should_publish_to_appsync_channels(self):
        coffee_orders.update_status_and_publish(
            'order-1', 'ACCEPTED', 'ORDER_ACCEPTED',
            {'attendeeId': 'att-1', 'baristaId': 'bar-1'},
            '2025-01-01T00:00:00.000Z',
        )
        assert _mock_publish.call_count >= 2

    def test_should_publish_to_barista_queue_when_queued(self):
        coffee_orders.update_status_and_publish(
            'order-1', 'QUEUED', 'ORDER_QUEUED',
            {'attendeeId': 'att-1'},
            '2025-01-01T00:00:00.000Z',
        )
        channels = [c[0][0] for c in _mock_publish.call_args_list]
        assert any('barista/queue' in ch for ch in channels)

    def test_should_append_custom_update_expression(self):
        coffee_orders.update_status_and_publish(
            'order-1', 'CANCELLED', 'ORDER_CANCELLED',
            {'attendeeId': 'att-1'},
            '2025-01-01T00:00:00.000Z',
            'cancellationReason = :reason',
            {':reason': 'timeout'},
        )
        call_kwargs = mock_orders_table.update_item.call_args[1]
        assert 'cancellationReason' in call_kwargs['UpdateExpression']
        assert call_kwargs['ExpressionAttributeValues'][':reason'] == 'timeout'


class TestUpdatePhaseAndCallback:
    """Unit tests for update_phase_and_callback helper."""

    def test_should_set_acceptance_phase(self):
        coffee_orders.update_phase_and_callback('order-1', 'WAITING_ACCEPTANCE', 'cb-123')

        call_kwargs = mock_orders_table.update_item.call_args[1]
        assert call_kwargs['ExpressionAttributeValues'][':phase'] == 'WAITING_ACCEPTANCE'
        assert call_kwargs['ExpressionAttributeValues'][':callbackId'] == 'cb-123'
        assert call_kwargs['ExpressionAttributeValues'][':callbackIds'] == {'acceptance': 'cb-123'}

    def test_should_set_completion_phase(self):
        coffee_orders.update_phase_and_callback('order-1', 'WAITING_COMPLETION', 'cb-456')

        call_kwargs = mock_orders_table.update_item.call_args[1]
        assert call_kwargs['ExpressionAttributeValues'][':phase'] == 'WAITING_COMPLETION'
        assert 'callbackIds.completion' in call_kwargs['UpdateExpression']


class TestEventParsing:
    """Test that string events are handled correctly."""

    def test_should_parse_string_event(self, order_event, closed_store_config):
        """Handler should accept a JSON string event (not just dict)."""
        _setup_dynamo_for_validation(closed_store_config)

        with DurableFunctionTestRunner(handler=coffee_orders.lambda_handler) as runner:
            result = runner.run(input=_json_input(order_event), timeout=10)

        parsed = _parse_result(result)
        assert result.status is InvocationStatus.SUCCEEDED
        assert parsed['status'] == 'CANCELLED'
