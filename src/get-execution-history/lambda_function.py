import json
import os

import boto3

# Initialize AWS clients
lambda_client = boto3.client('lambda')

# Environment variables
COFFEE_ORDERS_FUNCTION = os.environ.get('COFFEE_ORDERS_FUNCTION', 'coffee-orders')


def lambda_handler(event, context):
    """
    Lambda function to fetch durable execution history.
    Called via API Gateway when barista wants to view execution details.

    Accepts either:
    - executionArn: Full durable execution ARN
    - orderId: Order ID (will list executions to find the ARN)
    """
    print("Get Execution History request:", json.dumps(event, indent=2))

    try:
        # Get execution ARN or orderId from path/query parameters
        path_params = event.get('pathParameters') or {}
        query_params = event.get('queryStringParameters') or {}

        execution_arn = path_params.get('executionArn')
        order_id = query_params.get('orderId')

        print("Initial params:", json.dumps({'executionArn': execution_arn, 'orderId': order_id}))

        # If executionArn is a placeholder or invalid, treat it as not provided
        if execution_arn and (execution_arn == '_' or not execution_arn.startswith('arn:')):
            print(f"Ignoring invalid executionArn: {execution_arn}")
            execution_arn = None

        # If orderId provided and no valid executionArn, list executions to find the ARN
        if order_id and not execution_arn:
            print(f"Looking up execution ARN for orderId: {order_id}")
            print(f"Using function name: {COFFEE_ORDERS_FUNCTION}")

            # List recent executions for the coffee-orders function
            list_response = lambda_client.list_durable_executions_by_function(
                FunctionName=COFFEE_ORDERS_FUNCTION,
                Statuses=['SUCCEEDED'],  # Try succeeded first
            )

            print("List response:", json.dumps(list_response, indent=2, default=str))

            # Find the execution that matches this orderId by checking the input
            executions = list_response.get('DurableExecutions', [])
            print(f"Found {len(executions)} executions")

            for execution in executions:
                durable_execution_arn = execution.get('DurableExecutionArn')
                if durable_execution_arn:
                    # Fetch the history to check the input
                    try:
                        history_response = lambda_client.get_durable_execution_history(
                            DurableExecutionArn=durable_execution_arn,
                            IncludeExecutionData=True,
                        )

                        # Check if the first event (ExecutionStarted) contains our orderId
                        events = history_response.get('Events', [])
                        start_event = next(
                            (e for e in events if e.get('EventType') == 'ExecutionStarted'),
                            None
                        )

                        if start_event:
                            started_details = start_event.get('ExecutionStartedDetails', {})
                            input_data = started_details.get('Input', {})
                            payload = input_data.get('Payload')

                            if payload:
                                input_obj = json.loads(payload) if isinstance(payload, str) else payload
                                if input_obj.get('orderId') == order_id:
                                    execution_arn = durable_execution_arn
                                    print(f"Found execution ARN: {execution_arn}")
                                    break
                    except Exception as err:
                        print(f"Error checking execution: {err}")
                        continue

            if not execution_arn:
                return {
                    'statusCode': 404,
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*',
                    },
                    'body': json.dumps({
                        'error': 'Execution not found for orderId',
                        'orderId': order_id,
                    }),
                }

        if not execution_arn:
            return {
                'statusCode': 400,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*',
                },
                'body': json.dumps({
                    'error': 'Missing executionArn path parameter or orderId query parameter',
                }),
            }

        print(f"Fetching execution history for: {execution_arn}")

        # Fetch execution history using the SDK
        response = lambda_client.get_durable_execution_history(
            DurableExecutionArn=execution_arn,
            IncludeExecutionData=True,
        )

        print("Successfully fetched execution history")

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*',
            },
            'body': json.dumps({
                'executionArn': execution_arn,
                'history': response,
            }, default=str),
        }

    except Exception as error:
        print(f"Error fetching execution history: {error}")

        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*',
            },
            'body': json.dumps({
                'error': 'Failed to fetch execution history',
                'message': str(error),
            }),
        }
