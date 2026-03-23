import os
import sys
import importlib.util

import pytest

# ========== ENVIRONMENT VARIABLES ==========
# Set before any Lambda modules are imported so module-level code sees them
os.environ.setdefault('ORDERS_TABLE_NAME', 'test-orders')
os.environ.setdefault('CONFIG_TABLE_NAME', 'test-config')
os.environ.setdefault('EVENT_BUS_NAME', 'test-bus')
os.environ.setdefault('APPSYNC_HTTP_ENDPOINT', 'test.appsync.com')
os.environ.setdefault('APPSYNC_EVENTS_API_URL', 'https://test.appsync.com/event')
os.environ.setdefault('APPSYNC_EVENTS_API_KEY', 'test-api-key')
os.environ.setdefault('COFFEE_ORDERS_FUNCTION', 'test-coffee-orders')
os.environ.setdefault('AWS_REGION', 'us-east-1')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


def load_lambda_module(module_name, function_dir):
    """
    Load a Lambda function module from src/<function_dir>/lambda_function.py
    using importlib so multiple lambda_function.py files don't collide.
    """
    file_path = os.path.join(BASE_DIR, 'src', function_dir, 'lambda_function.py')
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ========== SHARED FIXTURES ==========

@pytest.fixture
def order_event():
    """Standard test event for placing a coffee order."""
    return {
        'orderId': 'test-order-123',
        'attendeeId': 'attendee-456',
        'eventId': 'coffee-shop',
        'orderDetails': {
            'drinkType': 'latte',
            'size': 'medium',
        },
        'timestamp': '2025-01-01T00:00:00.000Z',
    }


@pytest.fixture
def open_store_config():
    """Event config with store open."""
    return {
        'eventId': 'coffee-shop',
        'eventName': 'Coffee Shop',
        'storeOpen': True,
        'maxOrdersPerAttendee': 3,
        'createdAt': '2025-01-01T00:00:00.000Z',
        'updatedAt': '2025-01-01T00:00:00.000Z',
    }


@pytest.fixture
def closed_store_config():
    """Event config with store closed."""
    return {
        'eventId': 'coffee-shop',
        'eventName': 'Coffee Shop',
        'storeOpen': False,
        'maxOrdersPerAttendee': 3,
        'createdAt': '2025-01-01T00:00:00.000Z',
        'updatedAt': '2025-01-01T00:00:00.000Z',
    }
