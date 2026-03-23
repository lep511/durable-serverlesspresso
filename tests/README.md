# Tests — Durable Serverlesspresso (Python)

## Prerequisites

- Python 3.13+
- pip

## Setup

```bash
cd tests
pip install -r requirements.txt
```

### Dependencies

| Package | Purpose |
|---|---|
| `pytest` | Test runner |
| `aws-durable-execution-sdk-python` | Durable execution SDK (runtime dependency of the handler under test) |
| `aws-durable-execution-sdk-python-testing` | Local test runner (`DurableFunctionTestRunner`) for durable functions |
| `boto3` | AWS SDK — used by all Lambda functions; mocked in tests |

## Running tests

```bash
# From the project root
python3 -m pytest tests/ -v

# Run a single module
python3 -m pytest tests/test_coffee_orders.py -v

# Run a single test class
python3 -m pytest tests/test_coffee_orders.py::TestCallbacks -v

# Run a single test
python3 -m pytest tests/test_coffee_orders.py::TestCallbacks::test_should_complete_order_with_accept_and_complete_callbacks -v
```

## Test structure

```
tests/
├── README.md                          # This file
├── requirements.txt                   # Test dependencies
├── conftest.py                        # Shared fixtures and helpers
├── test_coffee_orders.py              # Durable function tests (15 tests)
├── test_callback_handler.py           # Callback handler tests  (9 tests)
├── test_event_publisher.py            # Event publisher tests   (14 tests)
└── test_get_execution_history.py      # Execution history tests (10 tests)
```

## Configuration

### Environment variables

All required environment variables are set automatically in `conftest.py` before any Lambda module is imported. No manual configuration is needed.

| Variable | Test value | Used by |
|---|---|---|
| `ORDERS_TABLE_NAME` | `test-orders` | coffee-orders, callback-handler, event-publisher |
| `CONFIG_TABLE_NAME` | `test-config` | coffee-orders, event-publisher |
| `EVENT_BUS_NAME` | `test-bus` | coffee-orders |
| `APPSYNC_HTTP_ENDPOINT` | `test.appsync.com` | coffee-orders |
| `APPSYNC_EVENTS_API_URL` | `https://test.appsync.com/event` | event-publisher |
| `APPSYNC_EVENTS_API_KEY` | `test-api-key` | event-publisher |
| `COFFEE_ORDERS_FUNCTION` | `test-coffee-orders` | get-execution-history |
| `AWS_REGION` | `us-east-1` | all |

### Module loading

Each Lambda function lives in its own directory with a `lambda_function.py` file. Because multiple modules share the same filename, `conftest.py` provides a `load_lambda_module(name, function_dir)` helper that uses `importlib` to load each one under a unique module name, avoiding import collisions.

### Mocking strategy

All external dependencies (DynamoDB, Lambda client, AppSync) are replaced with `unittest.mock.MagicMock` objects **before** the handler modules are imported. This ensures:

- No real AWS credentials or network calls are needed.
- Each test controls exactly what DynamoDB returns.
- Tests run fast (the full suite completes in ~11 seconds).

Mocks are automatically reset between tests via an `autouse` fixture in each test file.

## Test modules

### test_coffee_orders.py — Durable function

Uses `DurableFunctionTestRunner` from the AWS durable execution testing SDK for local testing, following the [official best practices](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions-testing.html).

**Mocked dependencies:** DynamoDB tables (`orders_table`, `config_table`), `utils` module (`publish_to_appsync`, `get_timestamp`, `convert_decimals`, `parse_callback_result`).

#### Testing patterns used

| Pattern | API | When to use |
|---|---|---|
| Synchronous | `runner.run(input=json_str, timeout=N)` | Flows that complete without callbacks (validation failures) |
| Asynchronous with callbacks | `runner.run_async()` / `runner.wait_for_callback()` / `runner.send_callback_success()` / `runner.wait_for_result()` | Flows that pause on `wait_for_callback` |

> **Note:** `runner.run()` expects a JSON **string** as `input`, and `result.result` is also returned as a JSON string. Use `json.dumps()` / `json.loads()` accordingly.

> **Note:** When sending two sequential callbacks (acceptance then completion), a brief `time.sleep(1)` is needed between them to allow the execution to process the first callback and create the second one.

#### Tests

| Class | Test | What it verifies |
|---|---|---|
| `TestExecution` | `test_should_initialize_and_create_operations` | Function starts and produces durable operations |
| `TestValidation` | `test_should_cancel_when_store_is_closed` | Store closed → order CANCELLED |
| | `test_should_cancel_when_daily_limit_exceeded` | Limit exceeded → order CANCELLED |
| | `test_should_cancel_when_event_not_found` | Missing event config → order CANCELLED |
| | `test_cancelled_orders_should_not_count_toward_limit` | CANCELLED orders excluded from daily count |
| `TestCallbacks` | `test_should_complete_order_with_accept_and_complete_callbacks` | Happy path: ACCEPT → COMPLETE → COMPLETED |
| | `test_should_cancel_order_on_cancel_callback_during_acceptance` | CANCEL during acceptance wait → CANCELLED |
| | `test_should_cancel_order_on_cancel_callback_during_completion` | CANCEL during completion wait → CANCELLED |
| `TestUpdateStatusAndPublish` | `test_should_update_dynamodb_with_status` | DynamoDB update expression is correct |
| | `test_should_publish_to_appsync_channels` | Publishes to order + attendee channels |
| | `test_should_publish_to_barista_queue_when_queued` | QUEUED also publishes to barista queue |
| | `test_should_append_custom_update_expression` | Extra expressions are appended correctly |
| `TestUpdatePhaseAndCallback` | `test_should_set_acceptance_phase` | Stores acceptance callback ID in DynamoDB |
| | `test_should_set_completion_phase` | Stores completion callback ID in DynamoDB |
| `TestEventParsing` | `test_should_parse_string_event` | Accepts JSON string input (not just dict) |

---

### test_callback_handler.py — Callback handler

Tests the Lambda that receives EventBridge events for barista actions and invokes durable execution callbacks via `send_durable_execution_callback_success`.

**Mocked dependencies:** `orders_table` (DynamoDB), `lambda_client` (Lambda).

| Class | Test | What it verifies |
|---|---|---|
| `TestAcceptAction` | `test_should_send_accept_callback` | BARISTA_ACCEPT_ORDER sends correct callback |
| | `test_should_fail_when_no_acceptance_callback` | 400 if no acceptance callback ID |
| `TestCompleteAction` | `test_should_send_complete_callback` | BARISTA_COMPLETE_ORDER sends correct callback |
| | `test_should_fail_when_no_completion_callback` | 400 if no completion callback ID |
| `TestCancelAction` | `test_should_send_cancel_callback` | ORDER_CANCEL_REQUEST sends cancel callback |
| | `test_should_fail_when_order_not_in_waiting_state` | 400 if no active callback |
| `TestErrorHandling` | `test_should_reject_unknown_event_type` | Unknown detail-type → 400 |
| | `test_should_return_404_when_order_not_found` | Missing order → 404 |
| | `test_should_return_500_on_dynamodb_error` | DynamoDB error → 500 |

---

### test_event_publisher.py — Event publisher

Tests the Lambda that updates DynamoDB display state and publishes real-time events to AppSync.

**Mocked dependencies:** `orders_table`, `config_table` (DynamoDB), `publish_to_channel` (AppSync HTTP).

| Class | Test | What it verifies |
|---|---|---|
| `TestOrderQueued` | `test_should_update_status_to_queued` | Sets status=QUEUED in DynamoDB |
| | `test_should_publish_to_barista_queue` | Publishes to barista queue channel |
| `TestOrderAccepted` | `test_should_update_status_to_accepted_with_barista` | Sets status=ACCEPTED with baristaId |
| `TestOrderCompleted` | `test_should_update_status_to_completed` | Sets status=COMPLETED, clears callback state |
| `TestOrderCancelled` | `test_should_update_status_to_cancelled_with_reason` | Sets status=CANCELLED with reason |
| | `test_should_default_reason_and_cancelled_by` | Uses defaults when fields missing |
| `TestStoreStatusChanged` | `test_should_update_config_table` | Updates storeOpen in config table |
| | `test_should_publish_to_store_channel` | Publishes to store-specific channel |
| | `test_should_fail_when_event_id_missing` | Raises ValueError if eventId missing |
| `TestAppSyncPublishing` | `test_should_publish_to_order_channel` | Publishes to order-specific channel |
| | `test_should_publish_to_attendee_channel` | Publishes to attendee-specific channel |
| | `test_should_continue_on_publish_failure` | Failure on one channel doesn't block others |
| `TestErrorHandling` | `test_should_raise_when_order_id_missing_for_order_event` | Missing orderId → ValueError |
| | `test_should_skip_dynamodb_for_unknown_event_type` | Unknown event → no DynamoDB update |

---

### test_get_execution_history.py — Execution history

Tests the API Gateway Lambda that fetches durable execution history.

**Mocked dependencies:** `lambda_client` (Lambda — `get_durable_execution_history`, `list_durable_executions_by_function`).

| Class | Test | What it verifies |
|---|---|---|
| `TestDirectArnLookup` | `test_should_fetch_history_by_execution_arn` | Fetches history by ARN |
| | `test_should_ignore_invalid_execution_arn` | Placeholder `_` treated as missing → 400 |
| | `test_should_ignore_non_arn_string` | Non-ARN string → 400 |
| `TestOrderIdLookup` | `test_should_find_execution_by_order_id` | Lists executions, matches orderId, returns history |
| | `test_should_return_404_when_no_matching_execution` | No match → 404 |
| | `test_should_return_404_when_no_executions_exist` | Empty list → 404 |
| `TestMissingParameters` | `test_should_return_400_when_no_params` | No ARN or orderId → 400 |
| | `test_should_return_400_with_empty_path_params` | Empty params → 400 |
| `TestErrorHandling` | `test_should_return_500_on_lambda_api_error` | Lambda API error → 500 |
| | `test_should_include_cors_headers` | All responses include CORS headers |
