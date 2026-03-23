"""
Tests for the get-execution-history Lambda function.

Verifies that execution history lookup works correctly via:
- Direct executionArn path parameter
- orderId query parameter (requires listing + matching executions)
- Error handling for missing params or not-found cases
"""
import json
import pytest
from unittest.mock import MagicMock

from conftest import load_lambda_module

# Load the module
execution_history = load_lambda_module('get_execution_history', 'get-execution-history')

# Replace module-level Lambda client with mock
mock_lambda_client = MagicMock()
execution_history.lambda_client = mock_lambda_client


@pytest.fixture(autouse=True)
def reset_mocks():
    mock_lambda_client.reset_mock()
    yield


def _make_apigw_event(path_params=None, query_params=None):
    """Create an API Gateway proxy event."""
    return {
        'httpMethod': 'GET',
        'pathParameters': path_params,
        'queryStringParameters': query_params,
        'headers': {},
        'body': None,
    }


# ========== DIRECT ARN LOOKUP TESTS ==========

class TestDirectArnLookup:

    def test_should_fetch_history_by_execution_arn(self):
        """Should call GetDurableExecutionHistory with the provided ARN."""
        arn = 'arn:aws:lambda:us-east-1:123456789012:function:coffee-orders:exec-1'
        mock_lambda_client.get_durable_execution_history.return_value = {
            'Events': [
                {'EventType': 'ExecutionStarted', 'Timestamp': '2025-01-01T00:00:00Z'},
                {'EventType': 'StepSucceeded', 'Timestamp': '2025-01-01T00:00:01Z'},
            ]
        }

        event = _make_apigw_event(path_params={'executionArn': arn})
        result = execution_history.lambda_handler(event, None)

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['executionArn'] == arn
        assert 'history' in body
        mock_lambda_client.get_durable_execution_history.assert_called_once_with(
            DurableExecutionArn=arn,
            IncludeExecutionData=True,
        )

    def test_should_ignore_invalid_execution_arn(self):
        """Placeholder ARN (_) should be treated as missing."""
        event = _make_apigw_event(path_params={'executionArn': '_'})
        result = execution_history.lambda_handler(event, None)

        assert result['statusCode'] == 400

    def test_should_ignore_non_arn_string(self):
        """Non-ARN strings should be treated as invalid."""
        event = _make_apigw_event(path_params={'executionArn': 'not-an-arn'})
        result = execution_history.lambda_handler(event, None)

        assert result['statusCode'] == 400


# ========== ORDER ID LOOKUP TESTS ==========

class TestOrderIdLookup:

    def test_should_find_execution_by_order_id(self):
        """Should list executions, find matching orderId, and return history."""
        target_arn = 'arn:aws:lambda:us-east-1:123456789012:function:coffee-orders:exec-match'

        mock_lambda_client.list_durable_executions_by_function.return_value = {
            'DurableExecutions': [
                {'DurableExecutionArn': 'arn:aws:lambda:us-east-1:123456789012:function:coffee-orders:exec-other'},
                {'DurableExecutionArn': target_arn},
            ]
        }

        # First call: non-matching execution during search
        # Second call: matching execution during search
        # Third call: final history fetch after ARN is found
        mock_lambda_client.get_durable_execution_history.side_effect = [
            {
                'Events': [{
                    'EventType': 'ExecutionStarted',
                    'ExecutionStartedDetails': {
                        'Input': {'Payload': json.dumps({'orderId': 'other-order'})}
                    },
                }]
            },
            {
                'Events': [{
                    'EventType': 'ExecutionStarted',
                    'ExecutionStartedDetails': {
                        'Input': {'Payload': json.dumps({'orderId': 'order-1'})}
                    },
                }]
            },
            {
                'Events': [
                    {'EventType': 'ExecutionStarted', 'Timestamp': '2025-01-01T00:00:00Z'},
                    {'EventType': 'StepSucceeded', 'Timestamp': '2025-01-01T00:00:01Z'},
                ]
            },
        ]

        event = _make_apigw_event(query_params={'orderId': 'order-1'})
        result = execution_history.lambda_handler(event, None)

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['executionArn'] == target_arn

    def test_should_return_404_when_no_matching_execution(self):
        """Should return 404 if no execution matches the orderId."""
        mock_lambda_client.list_durable_executions_by_function.return_value = {
            'DurableExecutions': [
                {'DurableExecutionArn': 'arn:aws:lambda:us-east-1:123456789012:function:coffee-orders:exec-1'},
            ]
        }
        mock_lambda_client.get_durable_execution_history.return_value = {
            'Events': [{
                'EventType': 'ExecutionStarted',
                'ExecutionStartedDetails': {
                    'Input': {'Payload': json.dumps({'orderId': 'different-order'})}
                },
            }]
        }

        event = _make_apigw_event(query_params={'orderId': 'order-1'})
        result = execution_history.lambda_handler(event, None)

        assert result['statusCode'] == 404
        body = json.loads(result['body'])
        assert body['orderId'] == 'order-1'

    def test_should_return_404_when_no_executions_exist(self):
        """Should return 404 if the function has no executions at all."""
        mock_lambda_client.list_durable_executions_by_function.return_value = {
            'DurableExecutions': []
        }

        event = _make_apigw_event(query_params={'orderId': 'order-1'})
        result = execution_history.lambda_handler(event, None)

        assert result['statusCode'] == 404


# ========== MISSING PARAMETERS TESTS ==========

class TestMissingParameters:

    def test_should_return_400_when_no_params(self):
        """Should return 400 if neither executionArn nor orderId is provided."""
        event = _make_apigw_event()
        result = execution_history.lambda_handler(event, None)

        assert result['statusCode'] == 400

    def test_should_return_400_with_empty_path_params(self):
        """Should return 400 with empty path and query params."""
        event = _make_apigw_event(path_params={}, query_params={})
        result = execution_history.lambda_handler(event, None)

        assert result['statusCode'] == 400


# ========== ERROR HANDLING TESTS ==========

class TestErrorHandling:

    def test_should_return_500_on_lambda_api_error(self):
        """Should return 500 if the Lambda API call fails."""
        arn = 'arn:aws:lambda:us-east-1:123456789012:function:coffee-orders:exec-1'
        mock_lambda_client.get_durable_execution_history.side_effect = Exception(
            'Service unavailable'
        )

        event = _make_apigw_event(path_params={'executionArn': arn})
        result = execution_history.lambda_handler(event, None)

        assert result['statusCode'] == 500
        body = json.loads(result['body'])
        assert 'Service unavailable' in body['message']

    def test_should_include_cors_headers(self):
        """All responses should include CORS headers."""
        event = _make_apigw_event()
        result = execution_history.lambda_handler(event, None)

        assert result['headers']['Access-Control-Allow-Origin'] == '*'
