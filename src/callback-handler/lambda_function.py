import json
import os

import boto3

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')
lambda_client = boto3.client('lambda')

# Environment variables
ORDERS_TABLE_NAME = os.environ['ORDERS_TABLE_NAME']

orders_table = dynamodb.Table(ORDERS_TABLE_NAME)


def lambda_handler(event, context):
    """
    Callback Handler Lambda Function

    Processes EventBridge events for barista actions (ACCEPT, COMPLETE, CANCEL)
    and invokes the appropriate durable execution callback.

    Event Types:
    - BARISTA_ACCEPT_ORDER: Barista accepts an order from the queue
    - BARISTA_COMPLETE_ORDER: Barista marks an order as complete
    - ORDER_CANCEL_REQUEST: Attendee, barista, or system cancels an order
    """
    print("Received callback event:", json.dumps(event, indent=2))

    # Derive action from detail-type
    detail_type = event.get('detail-type', '')
    if detail_type == 'BARISTA_ACCEPT_ORDER':
        action = 'ACCEPT'
    elif detail_type == 'BARISTA_COMPLETE_ORDER':
        action = 'COMPLETE'
    elif detail_type == 'ORDER_CANCEL_REQUEST':
        action = 'CANCEL'
    else:
        print(f"Unknown event type: {detail_type}")
        return {
            'statusCode': 400,
            'body': json.dumps({'error': 'Unknown event type'})
        }

    detail = event.get('detail', {})
    order_id = detail.get('orderId')
    barista_id = detail.get('baristaId')
    reason = detail.get('reason')
    cancelled_by = detail.get('cancelledBy')

    try:
        # Retrieve order record to get callback IDs and current phase
        order_result = orders_table.get_item(Key={'orderId': order_id})

        if 'Item' not in order_result:
            print(f"Order not found: {order_id}")
            return {
                'statusCode': 404,
                'body': json.dumps({'error': 'Order not found'})
            }

        order = order_result['Item']
        print("Order record:", json.dumps(order, indent=2, default=str))

        # Determine which callback ID to use based on action type and current phase
        callback_id = None

        if action == 'CANCEL':
            # For cancellation, use the active callback ID (current waiting phase)
            callback_id = order.get('activeCallbackId')

            if not callback_id:
                print(f"No active callback for order {order_id} in phase {order.get('currentPhase')}. Order may not be in a waiting state.")
                return {
                    'statusCode': 400,
                    'body': json.dumps({
                        'error': 'Order is not in a waiting state and cannot be cancelled via callback',
                        'currentPhase': order.get('currentPhase'),
                        'status': order.get('status'),
                    })
                }

        elif action == 'ACCEPT':
            # For acceptance, use the acceptance callback ID
            callback_ids = order.get('callbackIds', {})
            callback_id = callback_ids.get('acceptance') if callback_ids else None

            if not callback_id:
                print(f"No acceptance callback ID found for order {order_id}")
                return {
                    'statusCode': 400,
                    'body': json.dumps({
                        'error': 'Order is not waiting for acceptance',
                        'currentPhase': order.get('currentPhase'),
                    })
                }

        elif action == 'COMPLETE':
            # For completion, use the completion callback ID
            callback_ids = order.get('callbackIds', {})
            callback_id = callback_ids.get('completion') if callback_ids else None

            if not callback_id:
                print(f"No completion callback ID found for order {order_id}")
                return {
                    'statusCode': 400,
                    'body': json.dumps({
                        'error': 'Order is not waiting for completion',
                        'currentPhase': order.get('currentPhase'),
                    })
                }

        if not callback_id:
            print(f"Could not determine callback ID for action {action} on order {order_id}")
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Invalid action or order state'})
            }

        # Prepare callback result data
        callback_result = {'action': action}

        if action in ('ACCEPT', 'COMPLETE') and barista_id:
            callback_result['baristaId'] = barista_id
        elif action == 'CANCEL':
            callback_result['reason'] = reason or 'Unknown'
            callback_result['cancelledBy'] = cancelled_by or 'unknown'

        # Invoke durable execution callback
        print(f"Invoking callback {callback_id} with result:", json.dumps(callback_result))

        lambda_client.send_durable_execution_callback_success(
            CallbackId=callback_id,
            Result=json.dumps(callback_result)
        )

        print(f"Successfully invoked callback for order {order_id}, action: {action}")

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Callback invoked successfully',
                'orderId': order_id,
                'action': action,
                'callbackId': callback_id,
            })
        }

    except Exception as error:
        print("Error processing callback event:", str(error))

        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': 'Internal server error',
                'message': str(error),
            })
        }
