import json
import os
import uuid

import boto3
from aws_durable_execution_sdk_python.context import StepContext, durable_step
from aws_durable_execution_sdk_python import durable_execution, DurableContext
from aws_durable_execution_sdk_python.concurrency.models import BatchResult
from aws_durable_execution_sdk_python.config import (
    Duration,
    WaitForCallbackConfig,
    StepConfig,
    ParallelConfig,
)
from aws_durable_execution_sdk_python.retries import (
    create_retry_strategy,
    RetryStrategyConfig,
)
from utils import parse_callback_result, get_timestamp, publish_to_appsync, convert_decimals

# ========== AWS CLIENTS ==========
dynamodb = boto3.resource('dynamodb')

# ========== ENVIRONMENT VARIABLES ==========
ORDERS_TABLE_NAME = os.environ['ORDERS_TABLE_NAME']
CONFIG_TABLE_NAME = os.environ['CONFIG_TABLE_NAME']
APPSYNC_HTTP_ENDPOINT = os.environ['APPSYNC_HTTP_ENDPOINT']

orders_table = dynamodb.Table(ORDERS_TABLE_NAME)
config_table = dynamodb.Table(CONFIG_TABLE_NAME)

# ========== CONSTANTS ==========
TIMEOUTS = {
    'ACCEPTANCE': 120,  # 2 minutes
    'COMPLETION': 120,  # 2 minutes
}

CANCELLATION_REASONS = {
    'ACCEPTANCE_TIMEOUT': 'Acceptance timeout - no barista accepted within 2 minutes',
    'COMPLETION_TIMEOUT': 'Completion timeout - order not completed within 2 minutes',
    'VALIDATION_FAILED': 'Validation failed',
    'USER_CANCELLED': 'Order cancelled',
}

# ========== RETRY STRATEGY ==========
retry_strategy = create_retry_strategy(
    RetryStrategyConfig(
        max_attempts=3,
        initial_delay=Duration(seconds=1),
        backoff_rate=2.0,
    )
)

step_config = StepConfig(retry_strategy=retry_strategy)


# ========== WORKFLOW-SPECIFIC HELPER FUNCTIONS ==========

def update_status_and_publish(order_id, status, event_type, detail, timestamp,
                              update_expression=None, expression_values=None):
    """Update DynamoDB status and publish to AppSync"""
    # Update DynamoDB
    base_update = 'SET #status = :status, updatedAt = :timestamp'
    final_update = f'{base_update}, {update_expression}' if update_expression else base_update

    expression_attr_names = {'#status': 'status'}
    if update_expression and '#timestamps' in update_expression:
        expression_attr_names['#timestamps'] = 'timestamps'

    expr_values = {
        ':status': status,
        ':timestamp': timestamp,
    }
    if expression_values:
        expr_values.update(expression_values)

    orders_table.update_item(
        Key={'orderId': order_id},
        UpdateExpression=final_update,
        ExpressionAttributeNames=expression_attr_names,
        ExpressionAttributeValues=expr_values,
    )

    # Publish to AppSync for real-time updates
    event_data = {'type': event_type, 'orderId': order_id, 'timestamp': timestamp, 'data': detail}
    channels = [f'/coffee-ordering/orders/{order_id}']
    if detail.get('attendeeId'):
        channels.append(f'/coffee-ordering/attendee/{detail["attendeeId"]}')
    if status == 'QUEUED':
        channels.append('/coffee-ordering/barista/queue')

    for channel in channels:
        publish_to_appsync(channel, event_data, APPSYNC_HTTP_ENDPOINT)


def handle_cancellation(context, order_data, reason, cancelled_by, timestamp):
    """Handle order cancellation - unified cancellation logic"""
    def _cancel(_):
        update_status_and_publish(
            order_data['orderId'],
            'CANCELLED',
            'ORDER_CANCELLED',
            {
                'attendeeId': order_data['attendeeId'],
                'reason': reason,
                'cancelledBy': cancelled_by,
            },
            timestamp,
            'currentPhase = :phase, activeCallbackId = :null, cancellationReason = :reason, cancelledBy = :cancelledBy, #timestamps.cancelled = :timestamp',
            {
                ':phase': 'CANCELLED',
                ':null': None,
                ':reason': reason,
                ':cancelledBy': cancelled_by,
            }
        )

        context.logger.info("Order cancelled", {
            'orderId': order_data['orderId'],
            'reason': reason,
            'cancelledBy': cancelled_by,
        })

    context.step(_cancel, name='handle-cancellation', config=step_config)

    return {
        'orderId': order_data['orderId'],
        'status': 'CANCELLED',
        'reason': reason,
        'cancelledBy': cancelled_by,
    }


def update_phase_and_callback(order_id, phase, callback_id):
    """Update phase and callback tracking in DynamoDB"""
    timestamp = get_timestamp()

    if phase == 'WAITING_ACCEPTANCE':
        orders_table.update_item(
            Key={'orderId': order_id},
            UpdateExpression='SET currentPhase = :phase, activeCallbackId = :callbackId, callbackIds = :callbackIds, updatedAt = :timestamp',
            ExpressionAttributeValues={
                ':phase': phase,
                ':callbackId': callback_id,
                ':callbackIds': {'acceptance': callback_id},
                ':timestamp': timestamp,
            },
        )
    else:
        # For completion, preserve the acceptance callback
        orders_table.update_item(
            Key={'orderId': order_id},
            UpdateExpression='SET currentPhase = :phase, activeCallbackId = :callbackId, callbackIds.completion = :callbackId, updatedAt = :timestamp',
            ExpressionAttributeValues={
                ':phase': phase,
                ':callbackId': callback_id,
                ':timestamp': timestamp,
            },
        )

def check_event_id(event_id):
    result = config_table.get_item(Key={'eventId': event_id})
    item = result.get('Item', None)
    return item

def check_attendee_orders(attendee_id, event_id):
    result = orders_table.query(
        IndexName='AttendeeEventIndex',
        KeyConditionExpression='attendeeId = :attendeeId AND eventId = :eventId',
        ExpressionAttributeValues={
            ':attendeeId': attendee_id,
            ':eventId': event_id,
        },
    )
    items = result.get('Items', [])
    return items

# ========== STEPS ==========
@durable_step
def initialize_order(context: StepContext, order: dict) -> dict:
    timestamp = get_timestamp()
    context.logger.info("Generated workflow timestamp", {'timestamp': timestamp})

    order_id = order.get('orderId') or str(uuid.uuid4())
    order_record = {
        'orderId': order_id,
        'attendeeId': order['attendeeId'],
        'eventId': order['eventId'],
        'status': 'PENDING',
        'orderDetails': order['orderDetails'],
        'executionArn': '',  # Looked up dynamically via orderId when needed
        'timestamps': {'placed': timestamp},
        'createdAt': timestamp,
        'updatedAt': timestamp,
    }
    
    orders_table.put_item(Item=order_record)
    context.logger.info("Order initialized", {'orderId': order_id, 'status': 'PENDING'})
    
    return order_record

# ========== MAIN HANDLER ==========
@durable_execution
def lambda_handler(event: dict, context: DurableContext):
    # Parse event if it's a string
    event = json.loads(event) if isinstance(event, str) else event
    context.logger.info("Starting coffee order orchestration", {"event": event})

    # Step 1: Initialize order
    order_data = context.step(initialize_order(event))
    workflow_timestamp = order_data['createdAt']
    context.logger.info("Workflow timestamp set", {'workflowTimestamp': workflow_timestamp})

    # Step 2: Parallel validation steps
    event_id_for_query = event['eventId']
    attendee_id_for_query = event['attendeeId']
    
    def fetch_event_config(event_id):
        def fetch(ctx: DurableContext):
            return ctx.step(lambda _: check_event_id(event_id), name='fetch-event-config')
        return fetch

    def fetch_attendee_orders(attendee_id,  event_id):
        def fetch(ctx: DurableContext):
            return ctx.step(lambda _: check_attendee_orders(attendee_id, event_id), name='fetch-attendee-orders')
        return fetch

    validation_results: BatchResult = context.parallel(
        [
            fetch_event_config(event_id_for_query), 
            fetch_attendee_orders(attendee_id_for_query, event_id_for_query)
        ],
    )

    all_items = validation_results.all
    event_config = all_items[0].result if len(all_items) > 0 else None
    previous_orders = all_items[1].result if len(all_items) > 1 else []

    # ========== VALIDATION LOGIC ==========
    validation_errors = []

    if not event_config:
        validation_errors.append(f"Event {event['eventId']} does not exist")
    else:
        if not event_config.get('storeOpen'):
            validation_errors.append("Store is currently closed")

        max_orders = event_config.get('maxOrdersPerAttendee', 3)
        non_cancelled_orders = [o for o in previous_orders if o.get('status') != 'CANCELLED']

        if len(non_cancelled_orders) >= max_orders:
            validation_errors.append(
                f"Daily limit of {max_orders} orders exceeded (current: {len(non_cancelled_orders)})"
            )
        else:
            context.logger.info("Daily limit check passed", {
                'attendeeId': event['attendeeId'],
                'orderCount': len(non_cancelled_orders),
                'maxOrders': max_orders,
            })

    # Handle validation failures
    if validation_errors:
        error_message = '; '.join(validation_errors)
        context.logger.error("Validation failed", {
            'orderId': order_data['orderId'],
            'errors': validation_errors,
        })

        return handle_cancellation(context, order_data, error_message, 'system', workflow_timestamp)

    context.logger.info("All validations passed", {'orderId': order_data['orderId']})

    # ========== STEP 3: PUBLISH ORDER_PLACED EVENT ==========
    def _publish_order_placed(_):
        event_data = {
            'type': 'ORDER_PLACED',
            'orderId': order_data['orderId'],
            'timestamp': workflow_timestamp,
            'data': {
                'attendeeId': order_data['attendeeId'],
                'eventId': order_data['eventId'],
                'orderDetails': order_data['orderDetails'],
            },
        }

        publish_to_appsync(
            f"/coffee-ordering/orders/{order_data['orderId']}",
            event_data,
            APPSYNC_HTTP_ENDPOINT,
        )
        publish_to_appsync(
            f"/coffee-ordering/attendee/{order_data['attendeeId']}",
            event_data,
            APPSYNC_HTTP_ENDPOINT,
        )

    context.step(_publish_order_placed, name='publish-order-placed', config=step_config)

    # ========== STEP 4: UPDATE STATUS TO QUEUED ==========
    def _update_queued(_):
        update_status_and_publish(
            order_data['orderId'],
            'QUEUED',
            'ORDER_QUEUED',
            {
                'attendeeId': order_data['attendeeId'],
                'eventId': order_data['eventId'],
                'orderDetails': order_data['orderDetails'],
            },
            workflow_timestamp,
            '#timestamps.queued = :timestamp',
            {},
        )

    context.step(_update_queued, name='update-status-queued', config=step_config)

    # ========== STEP 5: WAIT FOR BARISTA ACCEPTANCE ==========
    context.logger.info("Waiting for barista acceptance", {'orderId': order_data['orderId']})

    try:
        def _acceptance_submitter(callback_id, ctx):
            update_phase_and_callback(order_data['orderId'], 'WAITING_ACCEPTANCE', callback_id)
            ctx.logger.info("Acceptance callback registered", {
                'orderId': order_data['orderId'],
                'callbackId': callback_id,
                'phase': 'WAITING_ACCEPTANCE',
            })

        acceptance_result = context.wait_for_callback(
            _acceptance_submitter,
            name='wait-acceptance',
            config=WaitForCallbackConfig(timeout=Duration(seconds=TIMEOUTS['ACCEPTANCE'])),
        )
    except Exception as e:
        context.logger.warning("Acceptance timeout occurred", {
            'orderId': order_data['orderId'],
            'error': str(e),
        })

        return handle_cancellation(
            context, order_data, CANCELLATION_REASONS['ACCEPTANCE_TIMEOUT'], 'system', workflow_timestamp
        )

    # Parse and validate acceptance result
    acceptance_result = parse_callback_result(acceptance_result, context, order_data['orderId'])
    context.logger.info("Acceptance result received", {
        'orderId': order_data['orderId'],
        'result': acceptance_result,
    })

    # Check for cancellation during acceptance
    if acceptance_result.get('action') == 'CANCEL':
        return handle_cancellation(
            context,
            order_data,
            acceptance_result.get('reason', CANCELLATION_REASONS['USER_CANCELLED']),
            acceptance_result.get('cancelledBy', 'unknown'),
            workflow_timestamp,
        )

    context.logger.info("Order accepted by barista", {
        'orderId': order_data['orderId'],
        'baristaId': acceptance_result.get('baristaId'),
    })

    # ========== STEP 6: UPDATE STATUS TO ACCEPTED ==========
    def _update_accepted(_):
        update_status_and_publish(
            order_data['orderId'],
            'ACCEPTED',
            'ORDER_ACCEPTED',
            {
                'attendeeId': order_data['attendeeId'],
                'baristaId': acceptance_result.get('baristaId'),
            },
            workflow_timestamp,
            '#timestamps.accepted = :timestamp, baristaId = :baristaId',
            {
                ':baristaId': acceptance_result.get('baristaId', 'unknown'),
            },
        )

    context.step(_update_accepted, name='update-status-accepted', config=step_config)

    # ========== STEP 7: WAIT FOR ORDER COMPLETION ==========
    context.logger.info("Waiting for order completion", {'orderId': order_data['orderId']})

    try:
        def _completion_submitter(callback_id, ctx):
            update_phase_and_callback(order_data['orderId'], 'WAITING_COMPLETION', callback_id)
            ctx.logger.info("Completion callback registered", {
                'orderId': order_data['orderId'],
                'callbackId': callback_id,
                'phase': 'WAITING_COMPLETION',
            })

        completion_result = context.wait_for_callback(
            _completion_submitter,
            name='wait-completion',
            config=WaitForCallbackConfig(timeout=Duration(seconds=TIMEOUTS['COMPLETION'])),
        )
    except Exception as e:
        context.logger.warning("Completion timeout occurred", {
            'orderId': order_data['orderId'],
            'error': str(e),
        })

        return handle_cancellation(
            context, order_data, CANCELLATION_REASONS['COMPLETION_TIMEOUT'], 'system', workflow_timestamp
        )

    # Parse and validate completion result
    completion_result = parse_callback_result(completion_result, context, order_data['orderId'])
    context.logger.info("Completion result received", {
        'orderId': order_data['orderId'],
        'result': completion_result,
    })

    # Check for cancellation during completion
    if completion_result.get('action') == 'CANCEL':
        return handle_cancellation(
            context,
            order_data,
            completion_result.get('reason', CANCELLATION_REASONS['USER_CANCELLED']),
            completion_result.get('cancelledBy', 'unknown'),
            workflow_timestamp,
        )

    context.logger.info("Order completed by barista", {
        'orderId': order_data['orderId'],
        'baristaId': completion_result.get('baristaId'),
    })

    # ========== STEP 8: UPDATE STATUS TO COMPLETED ==========
    def _update_completed(_):
        update_status_and_publish(
            order_data['orderId'],
            'COMPLETED',
            'ORDER_COMPLETED',
            {
                'attendeeId': order_data['attendeeId'],
                'baristaId': completion_result.get('baristaId'),
            },
            workflow_timestamp,
            'currentPhase = :phase, activeCallbackId = :null, #timestamps.completed = :timestamp',
            {
                ':phase': 'COMPLETED',
                ':null': None,
            },
        )

    context.step(_update_completed, name='update-status-completed', config=step_config)

    # ========== RETURN SUCCESS ==========
    return {
        'orderId': order_data['orderId'],
        'status': 'COMPLETED',
        'attendeeId': order_data['attendeeId'],
        'eventId': order_data['eventId'],
        'orderDetails': order_data['orderDetails'],
        'baristaId': completion_result.get('baristaId'),
        'timestamps': {
            'placed': order_data['timestamps']['placed'],
            'completed': workflow_timestamp,
        },
    }
