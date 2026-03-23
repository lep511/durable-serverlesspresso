"""
Tests for the event-publisher Lambda function.

Verifies that EventBridge events are correctly:
1. Persisted to DynamoDB (display state updates)
2. Published to AppSync Events channels (real-time frontend updates)
"""
import json
import pytest
from unittest.mock import MagicMock, patch, call

from conftest import load_lambda_module

# Load the module
event_publisher = load_lambda_module('event_publisher', 'event-publisher')

# Replace module-level AWS clients with mocks
mock_orders_table = MagicMock()
mock_config_table = MagicMock()
event_publisher.orders_table = mock_orders_table
event_publisher.config_table = mock_config_table

# Mock the AppSync publish function
mock_publish_to_channel = MagicMock()
event_publisher.publish_to_channel = mock_publish_to_channel


@pytest.fixture(autouse=True)
def reset_mocks():
    mock_orders_table.reset_mock()
    mock_config_table.reset_mock()
    mock_publish_to_channel.reset_mock()
    yield


def _make_event(detail_type, detail):
    return {
        'source': 'coffee.ordering',
        'detail-type': detail_type,
        'detail': detail,
    }


# ========== ORDER STATUS UPDATE TESTS ==========

class TestOrderQueued:

    def test_should_update_status_to_queued(self):
        """ORDER_QUEUED should set status=QUEUED and timestamps.queued."""
        event = _make_event('ORDER_QUEUED', {
            'orderId': 'order-1',
            'attendeeId': 'att-1',
        })

        event_publisher.lambda_handler(event, None)

        mock_orders_table.update_item.assert_called_once()
        kwargs = mock_orders_table.update_item.call_args[1]
        assert kwargs['ExpressionAttributeValues'][':status'] == 'QUEUED'

    def test_should_publish_to_barista_queue(self):
        """ORDER_QUEUED should publish to the barista queue channel."""
        event = _make_event('ORDER_QUEUED', {
            'orderId': 'order-1',
            'attendeeId': 'att-1',
        })

        event_publisher.lambda_handler(event, None)

        channels = [c[0][0] for c in mock_publish_to_channel.call_args_list]
        assert any('barista/queue' in ch for ch in channels)


class TestOrderAccepted:

    def test_should_update_status_to_accepted_with_barista(self):
        """ORDER_ACCEPTED should set status=ACCEPTED and baristaId."""
        event = _make_event('ORDER_ACCEPTED', {
            'orderId': 'order-1',
            'attendeeId': 'att-1',
            'baristaId': 'barista-123',
        })

        event_publisher.lambda_handler(event, None)

        kwargs = mock_orders_table.update_item.call_args[1]
        assert kwargs['ExpressionAttributeValues'][':status'] == 'ACCEPTED'
        assert kwargs['ExpressionAttributeValues'][':baristaId'] == 'barista-123'


class TestOrderCompleted:

    def test_should_update_status_to_completed(self):
        """ORDER_COMPLETED should set status=COMPLETED and clear callback state."""
        event = _make_event('ORDER_COMPLETED', {
            'orderId': 'order-1',
            'attendeeId': 'att-1',
            'baristaId': 'barista-123',
        })

        event_publisher.lambda_handler(event, None)

        kwargs = mock_orders_table.update_item.call_args[1]
        assert kwargs['ExpressionAttributeValues'][':status'] == 'COMPLETED'
        assert kwargs['ExpressionAttributeValues'][':phase'] == 'COMPLETED'
        assert kwargs['ExpressionAttributeValues'][':null'] is None


class TestOrderCancelled:

    def test_should_update_status_to_cancelled_with_reason(self):
        """ORDER_CANCELLED should set status=CANCELLED with reason and cancelledBy."""
        event = _make_event('ORDER_CANCELLED', {
            'orderId': 'order-1',
            'attendeeId': 'att-1',
            'reason': 'Timeout',
            'cancelledBy': 'system',
        })

        event_publisher.lambda_handler(event, None)

        kwargs = mock_orders_table.update_item.call_args[1]
        assert kwargs['ExpressionAttributeValues'][':status'] == 'CANCELLED'
        assert kwargs['ExpressionAttributeValues'][':reason'] == 'Timeout'
        assert kwargs['ExpressionAttributeValues'][':cancelledBy'] == 'system'

    def test_should_default_reason_and_cancelled_by(self):
        """Should use defaults when reason/cancelledBy are missing."""
        event = _make_event('ORDER_CANCELLED', {
            'orderId': 'order-1',
            'attendeeId': 'att-1',
        })

        event_publisher.lambda_handler(event, None)

        kwargs = mock_orders_table.update_item.call_args[1]
        assert kwargs['ExpressionAttributeValues'][':reason'] == 'Unknown'
        assert kwargs['ExpressionAttributeValues'][':cancelledBy'] == 'unknown'


# ========== STORE STATUS TESTS ==========

class TestStoreStatusChanged:

    def test_should_update_config_table(self):
        """STORE_STATUS_CHANGED should update storeOpen in the config table."""
        event = _make_event('STORE_STATUS_CHANGED', {
            'eventId': 'coffee-shop',
            'storeOpen': False,
        })

        event_publisher.lambda_handler(event, None)

        mock_config_table.update_item.assert_called_once()
        kwargs = mock_config_table.update_item.call_args[1]
        assert kwargs['Key'] == {'eventId': 'coffee-shop'}
        assert kwargs['ExpressionAttributeValues'][':storeOpen'] is False

    def test_should_publish_to_store_channel(self):
        """STORE_STATUS_CHANGED should publish to the store channel."""
        event = _make_event('STORE_STATUS_CHANGED', {
            'eventId': 'coffee-shop',
            'storeOpen': True,
        })

        event_publisher.lambda_handler(event, None)

        channels = [c[0][0] for c in mock_publish_to_channel.call_args_list]
        assert any('store/coffee-shop' in ch for ch in channels)

    def test_should_fail_when_event_id_missing(self):
        """Should raise error when eventId is missing."""
        event = _make_event('STORE_STATUS_CHANGED', {
            'storeOpen': True,
        })

        with pytest.raises(ValueError, match='eventId'):
            event_publisher.lambda_handler(event, None)


# ========== APPSYNC PUBLISHING TESTS ==========

class TestAppSyncPublishing:

    def test_should_publish_to_order_channel(self):
        """Every order event should publish to its order-specific channel."""
        event = _make_event('ORDER_ACCEPTED', {
            'orderId': 'order-1',
            'attendeeId': 'att-1',
            'baristaId': 'barista-123',
        })

        event_publisher.lambda_handler(event, None)

        channels = [c[0][0] for c in mock_publish_to_channel.call_args_list]
        assert any('orders/order-1' in ch for ch in channels)

    def test_should_publish_to_attendee_channel(self):
        """Events with attendeeId should also publish to attendee channel."""
        event = _make_event('ORDER_ACCEPTED', {
            'orderId': 'order-1',
            'attendeeId': 'att-1',
            'baristaId': 'barista-123',
        })

        event_publisher.lambda_handler(event, None)

        channels = [c[0][0] for c in mock_publish_to_channel.call_args_list]
        assert any('attendee/att-1' in ch for ch in channels)

    def test_should_continue_on_publish_failure(self):
        """A failed AppSync publish should not block other channels."""
        mock_publish_to_channel.side_effect = [
            RuntimeError('Connection failed'),  # first channel fails
            None,  # second channel succeeds
            None,  # third channel succeeds
        ]

        event = _make_event('ORDER_QUEUED', {
            'orderId': 'order-1',
            'attendeeId': 'att-1',
        })

        # Should not raise despite the first publish failing
        event_publisher.lambda_handler(event, None)

        assert mock_publish_to_channel.call_count >= 2


# ========== ERROR HANDLING TESTS ==========

class TestErrorHandling:

    def test_should_raise_when_order_id_missing_for_order_event(self):
        """Order events without orderId should raise an error."""
        event = _make_event('ORDER_QUEUED', {
            'attendeeId': 'att-1',
        })

        with pytest.raises(ValueError, match='orderId'):
            event_publisher.lambda_handler(event, None)

    def test_should_skip_dynamodb_for_unknown_event_type(self):
        """Unknown event types should not update DynamoDB."""
        event = _make_event('ORDER_PLACED', {
            'orderId': 'order-1',
            'attendeeId': 'att-1',
        })

        event_publisher.lambda_handler(event, None)

        mock_orders_table.update_item.assert_not_called()
