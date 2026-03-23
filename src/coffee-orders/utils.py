import json
import os
from decimal import Decimal
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from datetime import datetime, timezone

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session as BotocoreSession


def parse_callback_result(result, context, order_id):
    """Parse callback result - handles both string and dict responses"""
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            context.logger.error(f"Failed to parse callback result for order {order_id}")
            raise ValueError("Invalid callback result format")
    return result


def get_timestamp():
    """Generate ISO timestamp matching JS Date.toISOString() format"""
    now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'


def convert_decimals(obj):
    """Convert DynamoDB Decimal values to int/float for JSON serialization"""
    if isinstance(obj, list):
        return [convert_decimals(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    return obj


def publish_to_appsync(channel, event_data, http_endpoint):
    """Publish event to AppSync Events using IAM-signed request"""
    print(f"[AppSync] Publishing to channel: {channel}, endpoint: {http_endpoint}")

    if not http_endpoint:
        print(f"[LOCAL] Skipping AppSync publish to {channel}")
        return

    try:
        url = f"https://{http_endpoint}/event"
        print(f"[AppSync] Creating signed request to: {url}")

        body = json.dumps({
            "channel": channel,
            "events": [json.dumps(event_data, default=str)]
        })

        # Sign the request with SigV4
        session = BotocoreSession()
        credentials = session.get_credentials().get_frozen_credentials()
        region = os.environ.get('AWS_REGION', 'us-east-1')

        aws_request = AWSRequest(
            method='POST',
            url=url,
            data=body,
            headers={'Content-Type': 'application/json'}
        )
        SigV4Auth(credentials, 'appsync', region).add_auth(aws_request)

        print("[AppSync] Sending request...")
        req = Request(
            url,
            data=body.encode('utf-8'),
            headers=dict(aws_request.headers),
            method='POST'
        )

        with urlopen(req) as response:
            status = response.status
            print(f"[AppSync] Response status: {status}")
            if status == 200:
                print(f"[AppSync] Successfully published to {channel}")
            else:
                error_text = response.read().decode('utf-8')
                print(f"AppSync publish failed: {status} - {error_text}")
    except HTTPError as e:
        error_text = e.read().decode('utf-8')
        print(f"AppSync publish failed: {e.code} - {error_text}")
    except Exception as e:
        print(f"AppSync publish error: {e}")
