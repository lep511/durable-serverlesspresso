"""
Tests for the callback-handler Lambda function.

Verifies that EventBridge events for barista actions (ACCEPT, COMPLETE, CANCEL)
are correctly mapped to durable execution callback invocations.
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from conftest import load_lambda_module

# Load the module (boto3 creates clients lazily, safe without real creds)
callback_handler = load_lambda_module('callback_handler', 'callback-handler')

# Replace module-level AWS clients with mocks
mock_orders_table = MagicMock()
mock_lambda_client = MagicMock()
callback_handler.orders_table = mock_orders_table
callback_handler.lambda_client = mock_lambda_client


@pytest.fixture(autouse=True)
def reset_mocks():
    mock_orders_table.reset_mock()
    mock_lambda_client.reset_mock()
    yield


def _make_eventbridge_event(detail_type, detail):
    """Create an EventBridge event matching the SAM template pattern."""
    return {
        'source': 'coffee.ordering',
        'detail-type': detail_type,
        'detail': detail,
    }


def _order_record(phase='WAITING_ACCEPTANCE', active_cb='cb-active',
                   acceptance_cb='cb-accept', completion_cb=None):
    """Create a DynamoDB order record with callback IDs."""
    record = {
        'orderId': 'order-1',
        'status': 'QUEUED',
        'currentPhase': phase,
        'activeCallbackId': active_cb,
        'callbackIds': {'acceptance': acceptance_cb},
    }
    if completion_cb:
        record['callbackIds']['completion'] = completion_cb
    return record


# ========== ACCEPT TESTS ==========

class TestAcceptAction:

    def test_should_send_accept_callback(self):
        """BARISTA_ACCEPT_ORDER should invoke callback with ACCEPT action."""
        mock_orders_table.get_item.return_value = {
            'Item': _order_record()
        }

        event = _make_eventbridge_event('BARISTA_ACCEPT_ORDER', {
            'orderId': 'order-1',
            'baristaId': 'barista-123',
            'timestamp': '2025-01-01T00:00:00Z',
        })

        result = callback_handler.lambda_handler(event, None)

        assert result['statusCode'] == 200
        mock_lambda_client.send_durable_execution_callback_success.assert_called_once()
        call_kwargs = mock_lambda_client.send_durable_execution_callback_success.call_args[1]
        assert call_kwargs['CallbackId'] == 'cb-accept'
        payload = json.loads(call_kwargs['Result'])
        assert payload['action'] == 'ACCEPT'
        assert payload['baristaId'] == 'barista-123'

    def test_should_fail_when_no_acceptance_callback(self):
        """Should return 400 if order has no acceptance callback ID."""
        record = _order_record()
        record['callbackIds'] = {}
        mock_orders_table.get_item.return_value = {'Item': record}

        event = _make_eventbridge_event('BARISTA_ACCEPT_ORDER', {
            'orderId': 'order-1',
            'baristaId': 'barista-123',
            'timestamp': '2025-01-01T00:00:00Z',
        })

        result = callback_handler.lambda_handler(event, None)

        assert result['statusCode'] == 400
        mock_lambda_client.send_durable_execution_callback_success.assert_not_called()


# ========== COMPLETE TESTS ==========

class TestCompleteAction:

    def test_should_send_complete_callback(self):
        """BARISTA_COMPLETE_ORDER should invoke callback with COMPLETE action."""
        mock_orders_table.get_item.return_value = {
            'Item': _order_record(
                phase='WAITING_COMPLETION',
                completion_cb='cb-complete',
            )
        }

        event = _make_eventbridge_event('BARISTA_COMPLETE_ORDER', {
            'orderId': 'order-1',
            'baristaId': 'barista-456',
            'timestamp': '2025-01-01T00:00:00Z',
        })

        result = callback_handler.lambda_handler(event, None)

        assert result['statusCode'] == 200
        call_kwargs = mock_lambda_client.send_durable_execution_callback_success.call_args[1]
        assert call_kwargs['CallbackId'] == 'cb-complete'
        payload = json.loads(call_kwargs['Result'])
        assert payload['action'] == 'COMPLETE'
        assert payload['baristaId'] == 'barista-456'

    def test_should_fail_when_no_completion_callback(self):
        """Should return 400 if order has no completion callback ID."""
        mock_orders_table.get_item.return_value = {
            'Item': _order_record()  # no completion callback
        }

        event = _make_eventbridge_event('BARISTA_COMPLETE_ORDER', {
            'orderId': 'order-1',
            'baristaId': 'barista-456',
            'timestamp': '2025-01-01T00:00:00Z',
        })

        result = callback_handler.lambda_handler(event, None)

        assert result['statusCode'] == 400


# ========== CANCEL TESTS ==========

class TestCancelAction:

    def test_should_send_cancel_callback(self):
        """ORDER_CANCEL_REQUEST should invoke callback with CANCEL action."""
        mock_orders_table.get_item.return_value = {
            'Item': _order_record()
        }

        event = _make_eventbridge_event('ORDER_CANCEL_REQUEST', {
            'orderId': 'order-1',
            'reason': 'Customer left',
            'cancelledBy': 'attendee',
            'timestamp': '2025-01-01T00:00:00Z',
        })

        result = callback_handler.lambda_handler(event, None)

        assert result['statusCode'] == 200
        call_kwargs = mock_lambda_client.send_durable_execution_callback_success.call_args[1]
        payload = json.loads(call_kwargs['Result'])
        assert payload['action'] == 'CANCEL'
        assert payload['reason'] == 'Customer left'
        assert payload['cancelledBy'] == 'attendee'

    def test_should_fail_when_order_not_in_waiting_state(self):
        """Should return 400 if order has no active callback (not waiting)."""
        record = _order_record()
        record['activeCallbackId'] = None
        mock_orders_table.get_item.return_value = {'Item': record}

        event = _make_eventbridge_event('ORDER_CANCEL_REQUEST', {
            'orderId': 'order-1',
            'reason': 'Too late',
            'cancelledBy': 'system',
            'timestamp': '2025-01-01T00:00:00Z',
        })

        result = callback_handler.lambda_handler(event, None)

        assert result['statusCode'] == 400
        mock_lambda_client.send_durable_execution_callback_success.assert_not_called()


# ========== ERROR HANDLING TESTS ==========

class TestErrorHandling:

    def test_should_reject_unknown_event_type(self):
        """Unknown detail-type should return 400."""
        event = _make_eventbridge_event('UNKNOWN_EVENT', {
            'orderId': 'order-1',
            'timestamp': '2025-01-01T00:00:00Z',
        })

        result = callback_handler.lambda_handler(event, None)

        assert result['statusCode'] == 400

    def test_should_return_404_when_order_not_found(self):
        """Should return 404 if order doesn't exist in DynamoDB."""
        mock_orders_table.get_item.return_value = {}

        event = _make_eventbridge_event('BARISTA_ACCEPT_ORDER', {
            'orderId': 'nonexistent-order',
            'baristaId': 'barista-123',
            'timestamp': '2025-01-01T00:00:00Z',
        })

        result = callback_handler.lambda_handler(event, None)

        assert result['statusCode'] == 404

    def test_should_return_500_on_dynamodb_error(self):
        """Should return 500 if DynamoDB throws an error."""
        mock_orders_table.get_item.side_effect = Exception('DynamoDB unavailable')

        event = _make_eventbridge_event('BARISTA_ACCEPT_ORDER', {
            'orderId': 'order-1',
            'baristaId': 'barista-123',
            'timestamp': '2025-01-01T00:00:00Z',
        })

        result = callback_handler.lambda_handler(event, None)

        assert result['statusCode'] == 500
