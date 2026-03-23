import json
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session as BotocoreSession

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')

# Environment variables
ORDERS_TABLE_NAME = os.environ['ORDERS_TABLE_NAME']
CONFIG_TABLE_NAME = os.environ['CONFIG_TABLE_NAME']
APPSYNC_EVENTS_API_URL = os.environ['APPSYNC_EVENTS_API_URL']
APPSYNC_EVENTS_API_KEY = os.environ['APPSYNC_EVENTS_API_KEY']

orders_table = dynamodb.Table(ORDERS_TABLE_NAME)
config_table = dynamodb.Table(CONFIG_TABLE_NAME)


def lambda_handler(event, context):
    """
    Event Publisher Lambda Function

    This function is the single source of truth for customer-facing order status.
    It handles two responsibilities:
    1. Update DynamoDB with display state (status, timestamps, cancellation details)
    2. Publish real-time events to AppSync Events for frontend updates

    Hybrid State Management Approach:
    - Durable Function writes: callback IDs, currentPhase, activeCallbackId (orchestration state)
    - Event Publisher writes: status, timestamps, cancellation details (display state)
    """
    print("Event Publisher received event:", json.dumps(event, indent=2))

    event_type = event.get('detail-type', '')
    detail = event.get('detail', {})
    order_id = detail.get('orderId')
    attendee_id = detail.get('attendeeId')
    order_details = detail.get('orderDetails')
    barista_id = detail.get('baristaId')
    reason = detail.get('reason')
    cancelled_by = detail.get('cancelledBy')
    event_id = detail.get('eventId')
    store_open = detail.get('storeOpen')
    now = datetime.now(timezone.utc)
    timestamp = now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'

    try:
        # Handle store status events
        if event_type == 'STORE_STATUS_CHANGED':
            if not event_id or store_open is None:
                raise ValueError("eventId and storeOpen are required for STORE_STATUS_CHANGED events")

            # Update config table with new store status (source of truth)
            update_store_status(event_id, store_open, timestamp)

            # Publish to AppSync Events for real-time frontend updates
            publish_to_appsync_events(event_type, order_id, attendee_id, {
                'timestamp': timestamp,
                'eventId': event_id,
                'storeOpen': store_open,
            })

            print(f"Successfully processed {event_type} event for event {event_id}")
            return

        # Handle order events
        if not order_id:
            raise ValueError("orderId is required for order events")

        # Update DynamoDB with display state based on event type
        update_order_display_state(event_type, order_id, timestamp, {
            'baristaId': barista_id,
            'reason': reason,
            'cancelledBy': cancelled_by,
        })

        # Publish to AppSync Events for real-time frontend updates
        publish_to_appsync_events(event_type, order_id, attendee_id, {
            'orderDetails': order_details,
            'baristaId': barista_id,
            'reason': reason,
            'cancelledBy': cancelled_by,
            'timestamp': timestamp,
        })

        print(f"Successfully processed {event_type} event for order {order_id}")

    except Exception as error:
        print(f"Error processing event: {error}")
        raise


def update_store_status(event_id, store_open, timestamp):
    """Update store status in config table (source of truth)"""
    config_table.update_item(
        Key={'eventId': event_id},
        UpdateExpression='SET storeOpen = :storeOpen, updatedAt = :timestamp',
        ExpressionAttributeValues={
            ':storeOpen': store_open,
            ':timestamp': timestamp,
        },
    )
    print(f"Updated store status for event {event_id} to {'OPEN' if store_open else 'CLOSED'}")


def update_order_display_state(event_type, order_id, timestamp, data):
    """Update DynamoDB with display state based on event type"""
    if event_type == 'ORDER_QUEUED':
        orders_table.update_item(
            Key={'orderId': order_id},
            UpdateExpression='SET #status = :status, #timestamps.queued = :timestamp, updatedAt = :timestamp',
            ExpressionAttributeNames={
                '#status': 'status',
                '#timestamps': 'timestamps',
            },
            ExpressionAttributeValues={
                ':status': 'QUEUED',
                ':timestamp': timestamp,
            },
        )
        print(f"Updated order {order_id} status to QUEUED")

    elif event_type == 'ORDER_ACCEPTED':
        orders_table.update_item(
            Key={'orderId': order_id},
            UpdateExpression='SET #status = :status, #timestamps.accepted = :timestamp, baristaId = :baristaId, updatedAt = :timestamp',
            ExpressionAttributeNames={
                '#status': 'status',
                '#timestamps': 'timestamps',
            },
            ExpressionAttributeValues={
                ':status': 'ACCEPTED',
                ':timestamp': timestamp,
                ':baristaId': data.get('baristaId') or 'unknown',
            },
        )
        print(f"Updated order {order_id} status to ACCEPTED")

    elif event_type == 'ORDER_COMPLETED':
        orders_table.update_item(
            Key={'orderId': order_id},
            UpdateExpression='SET #status = :status, currentPhase = :phase, activeCallbackId = :null, #timestamps.completed = :timestamp, updatedAt = :timestamp',
            ExpressionAttributeNames={
                '#status': 'status',
                '#timestamps': 'timestamps',
            },
            ExpressionAttributeValues={
                ':status': 'COMPLETED',
                ':phase': 'COMPLETED',
                ':null': None,
                ':timestamp': timestamp,
            },
        )
        print(f"Updated order {order_id} status to COMPLETED")

    elif event_type == 'ORDER_CANCELLED':
        orders_table.update_item(
            Key={'orderId': order_id},
            UpdateExpression='SET #status = :status, currentPhase = :phase, activeCallbackId = :null, cancellationReason = :reason, cancelledBy = :cancelledBy, #timestamps.cancelled = :timestamp, updatedAt = :timestamp',
            ExpressionAttributeNames={
                '#status': 'status',
                '#timestamps': 'timestamps',
            },
            ExpressionAttributeValues={
                ':status': 'CANCELLED',
                ':phase': 'CANCELLED',
                ':null': None,
                ':reason': data.get('reason') or 'Unknown',
                ':cancelledBy': data.get('cancelledBy') or 'unknown',
                ':timestamp': timestamp,
            },
        )
        print(f"Updated order {order_id} status to CANCELLED")

    else:
        print(f"No DynamoDB update needed for event type: {event_type}")


def publish_to_appsync_events(event_type, order_id, attendee_id, data):
    """Publish events to AppSync Events for real-time frontend updates"""
    channels = []

    # Handle store status events
    if event_type == 'STORE_STATUS_CHANGED':
        store_event_data = {
            'type': event_type,
            'eventId': data.get('eventId'),
            'timestamp': data.get('timestamp'),
            'data': {
                'storeOpen': data.get('storeOpen'),
            },
        }

        # Publish to store channel
        store_channel = f"coffee-ordering/store/{data.get('eventId')}"
        try:
            publish_to_channel(store_channel, store_event_data)
            print(f"Published {event_type} to channel: {store_channel}")
        except Exception as error:
            print(f"Failed to publish to channel {store_channel}: {error}")
        return

    # Handle order events
    event_data = {
        'type': event_type,
        'orderId': order_id,
        'timestamp': data.get('timestamp'),
        'data': {
            'baristaId': data.get('baristaId'),
            'reason': data.get('reason'),
            'cancelledBy': data.get('cancelledBy'),
            'orderDetails': data.get('orderDetails'),
        },
    }

    # Always publish to order-specific channel
    # For publishing: format is namespace/channel-path (NO leading slash)
    # For subscribing: format is /namespace/channel-path (WITH leading slash)
    # Our namespace is "coffee-ordering"
    channels.append(f"coffee-ordering/orders/{order_id}")

    # Publish to barista queue for new orders
    if event_type == 'ORDER_QUEUED':
        channels.append('coffee-ordering/barista/queue')

    # Publish to attendee channel if attendeeId is available
    if attendee_id:
        channels.append(f"coffee-ordering/attendee/{attendee_id}")

    # Publish to each channel
    for channel in channels:
        try:
            publish_to_channel(channel, event_data)
            print(f"Published {event_type} to channel: {channel}")
        except Exception as error:
            print(f"Failed to publish to channel {channel}: {error}")
            # Continue with other channels even if one fails


def publish_to_channel(channel, event_data):
    """Publish a single event to an AppSync Events channel using IAM authentication"""
    body = json.dumps({
        'channel': channel,
        'events': [json.dumps(event_data, default=str)],
    })

    # Sign the request with SigV4
    session = BotocoreSession()
    credentials = session.get_credentials().get_frozen_credentials()
    region = os.environ.get('AWS_REGION', 'us-east-1')

    aws_request = AWSRequest(
        method='POST',
        url=APPSYNC_EVENTS_API_URL,
        data=body,
        headers={'Content-Type': 'application/json'}
    )
    SigV4Auth(credentials, 'appsync', region).add_auth(aws_request)

    req = Request(
        APPSYNC_EVENTS_API_URL,
        data=body.encode('utf-8'),
        headers=dict(aws_request.headers),
        method='POST'
    )

    try:
        with urlopen(req) as response:
            if response.status != 200:
                error_text = response.read().decode('utf-8')
                raise RuntimeError(
                    f"AppSync Events publish failed: {response.status} - {error_text}"
                )
    except HTTPError as e:
        error_text = e.read().decode('utf-8')
        raise RuntimeError(
            f"AppSync Events publish failed: {e.code} {e.reason} - {error_text}"
        )

    print(f"Successfully published to channel {channel}")
