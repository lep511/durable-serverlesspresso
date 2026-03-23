# Serverlesspresso API

**Real-time serverless coffee ordering powered by AWS Lambda Durable Executions**

**Base URL:** `https://{api-id}.execute-api.{region}.amazonaws.com/prod`

**Stage:** `prod`

**CORS:** All endpoints return `Access-Control-Allow-Origin: *`

---

## Endpoints Overview

| Method | Path | Integration | Description |
|--------|------|-------------|-------------|
| GET | `/orders` | DynamoDB (direct) | List orders with optional filtering |
| POST | `/orders` | Lambda (async) | Place a new coffee order |
| GET | `/execution/history` | Lambda (proxy) | Get durable execution history |
| POST | `/barista/accept/{orderId}` | EventBridge | Barista accepts an order |
| POST | `/barista/complete/{orderId}` | EventBridge | Barista completes an order |
| POST | `/orders/{orderId}/cancel` | EventBridge | Cancel an order |
| POST | `/store/status` | EventBridge | Update store open/closed status |
| GET | `/config/{eventId}` | DynamoDB (direct) | Get event configuration |
| GET | `/orders/count` | DynamoDB (direct) | Get completed order count for an attendee |

---

## 1. List Orders

```
GET /orders
```

Queries orders from DynamoDB directly (no Lambda). The query strategy depends on which parameters are provided.

### Query Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `eventId` | string | No | - | Filter orders by event ID |
| `status` | string | No | `QUEUED` | Filter by status (e.g. `QUEUED`, `ACCEPTED`, `COMPLETED`, `CANCELLED`) |
| `limit` | integer | No | `100` | Maximum number of orders to return |

### Query Logic

| Parameters provided | DynamoDB Index Used | Behavior |
|---|---|---|
| `eventId` only (no `status`) | `EventTimeIndex` | Returns all orders for that event, newest first |
| `status` (with or without `eventId`) | `StatusIndex` | Returns orders matching the first status value, newest first |
| Neither | `StatusIndex` | Defaults to `QUEUED` status |

### Example Request

```bash
# Get all QUEUED orders (default)
curl "${API_URL}/orders"

# Get orders by event
curl "${API_URL}/orders?eventId=coffee-shop"

# Get completed orders, limit 10
curl "${API_URL}/orders?status=COMPLETED&limit=10"
```

### Response `200 OK`

```json
{
  "orders": [
    {
      "orderId": "abc-123",
      "attendeeId": "attendee-456",
      "eventId": "coffee-shop",
      "status": "QUEUED",
      "createdAt": "2025-01-15T10:30:00Z",
      "updatedAt": "2025-01-15T10:30:05Z",
      "orderDetails": {
        "drinkType": "latte",
        "size": "medium"
      },
      "timestamps": {
        "placed": "2025-01-15T10:30:00Z"
      }
    }
  ],
  "count": 1
}
```

---

## 2. Place Order

```
POST /orders
```

Invokes the `CoffeeOrdersFunction` durable Lambda **asynchronously** (`X-Amz-Invocation-Type: Event`). The `orderId` is auto-generated from the API Gateway request ID.

### Request Body

```json
{
  "attendeeId": "attendee-456",
  "eventId": "coffee-shop",
  "orderDetails": {
    "drinkType": "latte",
    "size": "medium"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `attendeeId` | string | Yes | Unique identifier for the attendee |
| `eventId` | string | Yes | Event/shop identifier (used for config lookup) |
| `orderDetails` | object | Yes | Drink details |
| `orderDetails.drinkType` | string | Yes | Type of drink (e.g. `latte`, `cappuccino`) |
| `orderDetails.size` | string | Yes | Size (e.g. `small`, `medium`, `large`) |

### Response `202 Accepted`

```json
{
  "orderId": "d290f1ee-6c54-4b01-90e6-d701748f0851",
  "status": "PENDING",
  "message": "Order placed successfully"
}
```

### Error Responses

**`400 Bad Request`** — Invalid request body

```json
{
  "error": {
    "code": "INVALID_REQUEST",
    "message": "Invalid request body"
  }
}
```

**`500 Internal Server Error`**

```json
{
  "error": {
    "code": "INTERNAL_ERROR",
    "message": "Internal server error"
  }
}
```

### Example

```bash
curl -X POST "${API_URL}/orders" \
  -H "Content-Type: application/json" \
  -d '{
    "attendeeId": "attendee-456",
    "eventId": "coffee-shop",
    "orderDetails": {
      "drinkType": "latte",
      "size": "medium"
    }
  }'
```

---

## 3. Get Execution History

```
GET /execution/history
```

Lambda proxy integration — fetches the durable execution history for a specific order. Supports lookup by ARN or by orderId.

### Query Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `orderId` | string | Conditional | Order ID to look up (searches all executions to find matching one) |

### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `executionArn` | string | Conditional | Direct durable execution ARN |

> **Note:** Provide either `executionArn` (path param) or `orderId` (query param). If both are missing, returns `400`.

### Response `200 OK`

```json
{
  "executionArn": "arn:aws:lambda:us-east-1:123456789012:function:coffee-orders:exec-1",
  "history": [
    {
      "EventType": "ExecutionStarted",
      "Timestamp": "2025-01-15T10:30:00Z"
    },
    {
      "EventType": "StepSucceeded",
      "Timestamp": "2025-01-15T10:30:01Z"
    }
  ]
}
```

### Error Responses

| Status | Condition |
|--------|-----------|
| `400` | No `executionArn` or `orderId` provided |
| `404` | No execution found matching the `orderId` |
| `500` | Lambda API call failed |

### Example

```bash
# By orderId
curl "${API_URL}/execution/history?orderId=abc-123"
```

---

## 4. Barista Accept Order

```
POST /barista/accept/{orderId}
```

Publishes a `BARISTA_ACCEPT_ORDER` event to EventBridge, which triggers the `CallbackHandlerFunction` to resolve the acceptance callback on the durable execution.

### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `orderId` | string | Yes | Order to accept |

### Request Body

```json
{
  "baristaId": "barista-123"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `baristaId` | string | Yes | Identifier of the accepting barista |

### Response `200 OK`

```json
{
  "success": true,
  "message": "Order accepted"
}
```

### Example

```bash
curl -X POST "${API_URL}/barista/accept/abc-123" \
  -H "Content-Type: application/json" \
  -d '{"baristaId": "barista-123"}'
```

### EventBridge Event Produced

```json
{
  "Source": "coffee.ordering",
  "DetailType": "BARISTA_ACCEPT_ORDER",
  "Detail": {
    "orderId": "abc-123",
    "action": "ACCEPT",
    "baristaId": "barista-123",
    "timestamp": "15/Jan/2025:10:35:00 +0000"
  }
}
```

---

## 5. Barista Complete Order

```
POST /barista/complete/{orderId}
```

Publishes a `BARISTA_COMPLETE_ORDER` event to EventBridge, which triggers the callback handler to resolve the completion callback.

### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `orderId` | string | Yes | Order to complete |

### Request Body

```json
{
  "baristaId": "barista-123"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `baristaId` | string | Yes | Identifier of the completing barista |

### Response `200 OK`

```json
{
  "success": true,
  "message": "Order completed"
}
```

### Example

```bash
curl -X POST "${API_URL}/barista/complete/abc-123" \
  -H "Content-Type: application/json" \
  -d '{"baristaId": "barista-123"}'
```

### EventBridge Event Produced

```json
{
  "Source": "coffee.ordering",
  "DetailType": "BARISTA_COMPLETE_ORDER",
  "Detail": {
    "orderId": "abc-123",
    "action": "COMPLETE",
    "baristaId": "barista-123",
    "timestamp": "15/Jan/2025:10:40:00 +0000"
  }
}
```

---

## 6. Cancel Order

```
POST /orders/{orderId}/cancel
```

Publishes an `ORDER_CANCEL_REQUEST` event to EventBridge. The callback handler sends a CANCEL callback to whichever wait the durable execution is currently paused on (acceptance or completion).

### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `orderId` | string | Yes | Order to cancel |

### Request Body

```json
{
  "reason": "Customer changed mind",
  "cancelledBy": "attendee"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `reason` | string | Yes | Cancellation reason |
| `cancelledBy` | string | Yes | Who cancelled (`attendee`, `barista`, `system`) |

### Response `200 OK`

```json
{
  "success": true,
  "message": "Cancellation request submitted"
}
```

### Example

```bash
curl -X POST "${API_URL}/orders/abc-123/cancel" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Customer left", "cancelledBy": "attendee"}'
```

### EventBridge Event Produced

```json
{
  "Source": "coffee.ordering",
  "DetailType": "ORDER_CANCEL_REQUEST",
  "Detail": {
    "orderId": "abc-123",
    "action": "CANCEL",
    "reason": "Customer left",
    "cancelledBy": "attendee",
    "timestamp": "15/Jan/2025:10:32:00 +0000"
  }
}
```

---

## 7. Update Store Status

```
POST /store/status
```

Publishes a `STORE_STATUS_CHANGED` event to EventBridge. The event publisher updates the config table and publishes a real-time notification to the frontend via AppSync.

### Request Body

```json
{
  "eventId": "coffee-shop",
  "storeOpen": true
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `eventId` | string | Yes | Event/shop identifier |
| `storeOpen` | boolean | Yes | `true` to open, `false` to close |

### Response `200 OK`

```json
{
  "success": true,
  "message": "Store status updated"
}
```

### Example

```bash
# Open the store
curl -X POST "${API_URL}/store/status" \
  -H "Content-Type: application/json" \
  -d '{"eventId": "coffee-shop", "storeOpen": true}'

# Close the store
curl -X POST "${API_URL}/store/status" \
  -H "Content-Type: application/json" \
  -d '{"eventId": "coffee-shop", "storeOpen": false}'
```

### EventBridge Event Produced

```json
{
  "Source": "coffee.ordering",
  "DetailType": "STORE_STATUS_CHANGED",
  "Detail": {
    "eventId": "coffee-shop",
    "storeOpen": true,
    "timestamp": "15/Jan/2025:09:00:00 +0000"
  }
}
```

---

## 8. Get Event Configuration

```
GET /config/{eventId}
```

Reads the event configuration directly from the `ConfigTable` in DynamoDB (no Lambda).

### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `eventId` | string | Yes | Event identifier |

### Response `200 OK` (found)

```json
{
  "eventId": "coffee-shop",
  "eventName": "Coffee Shop",
  "storeOpen": true,
  "maxOrdersPerAttendee": 3,
  "createdAt": "2025-01-01T00:00:00Z",
  "updatedAt": "2025-01-15T09:00:00Z"
}
```

### Response `200 OK` (not found)

```json
{
  "error": "Event configuration not found"
}
```

### Example

```bash
curl "${API_URL}/config/coffee-shop"
```

---

## 9. Get Order Count

```
GET /orders/count
```

Queries the count of **COMPLETED** orders for a specific attendee on the current day. Used by the frontend to show remaining order allowance.

### Query Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `attendeeId` | string | Yes | Attendee identifier |
| `eventId` | string | Yes | Event identifier |

### Response `200 OK`

```json
{
  "count": 2
}
```

### Example

```bash
curl "${API_URL}/orders/count?attendeeId=attendee-456&eventId=coffee-shop"
```

---

## Integration Architecture

```
                                    +------------------+
POST /orders -----> Lambda (async)  | CoffeeOrders     |  (Durable Execution)
                                    | Function         |
                                    +------------------+
                                           |
GET  /orders -----> DynamoDB (direct)      | writes to
GET  /config -----> DynamoDB (direct)      v
GET  /orders/count -> DynamoDB (direct)  +------------------+
                                         | OrdersTable      |
                                         | ConfigTable      |
                                         +------------------+

POST /barista/accept ----+
POST /barista/complete --+--> EventBridge --> CallbackHandler --> Durable Callback
POST /orders/cancel -----+                --> EventPublisher  --> AppSync + DynamoDB
POST /store/status ------+

GET /execution/history --> Lambda (proxy) --> GetExecutionHistory
```

### Integration Types

| Type | Endpoints | Description |
|------|-----------|-------------|
| `aws` (DynamoDB direct) | `GET /orders`, `GET /config/{eventId}`, `GET /orders/count` | API Gateway queries DynamoDB directly via VTL request/response templates. No Lambda cold starts. |
| `aws` (Lambda async) | `POST /orders` | Invokes Lambda with `X-Amz-Invocation-Type: Event`. Returns `202` immediately; the durable execution runs in the background. |
| `aws` (EventBridge) | `POST /barista/*`, `POST /orders/*/cancel`, `POST /store/status` | API Gateway calls `PutEvents` directly. Events trigger downstream Lambdas via EventBridge rules. |
| `aws_proxy` (Lambda proxy) | `GET /execution/history` | Standard Lambda proxy integration. Lambda controls the full response. |
| `mock` | `OPTIONS` on `/orders` | Returns CORS preflight headers without hitting any backend. |

### IAM Role

All direct integrations use `ApiGatewayRole` with least-privilege policies:

- **lambda:InvokeFunction** on `CoffeeOrdersFunction` (+ alias) and `GetExecutionHistoryFunction`
- **dynamodb:Query** on `OrdersTable` and its GSIs
- **dynamodb:GetItem** on `ConfigTable`
- **events:PutEvents** on the custom EventBus
