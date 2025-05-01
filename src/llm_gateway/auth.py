import os
import logging
from typing import List, Set

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

API_KEY_HEADER_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)

# Get a logger instance for this module
log = logging.getLogger(__name__)

# In-memory cache for valid API keys to avoid reading env var on every request
_valid_api_keys_cache: Set[str] | None = None

def get_valid_api_keys() -> Set[str]:
    """
    Retrieves the set of valid API keys from the environment variable.
    Caches the result for subsequent calls.
    """
    global _valid_api_keys_cache
    if _valid_api_keys_cache is None:
        api_keys_str = os.getenv("VALID_API_KEYS", "")
        if not api_keys_str:
            # In a real scenario, you might raise an error or log a warning
            # if no keys are configured, depending on security requirements.
            log.warning("No VALID_API_KEYS environment variable set. Authentication may fail.")
            _valid_api_keys_cache = set()
        else:
            _valid_api_keys_cache = set(key.strip() for key in api_keys_str.split(','))
            # print(f"Loaded {len(_valid_api_keys_cache)} API keys.") # Be careful logging this in prod
            # Use debug level for potentially sensitive info like count of keys
            log.debug(f"Loaded API keys count", count=len(_valid_api_keys_cache))
    return _valid_api_keys_cache

async def authenticate_api_key(
    api_key_header: str = Security(api_key_header),
) -> str:
    """
    FastAPI dependency to authenticate a request using the X-API-Key header.

    Raises:
        HTTPException: If the API key is missing or invalid.

    Returns:
        str: The validated API key.
    """
    if not api_key_header:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing API Key in header",
        )

    valid_keys = get_valid_api_keys()
    if api_key_header not in valid_keys:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN,
            detail="Invalid API Key",
        )

    # Return the key itself, might be useful later (e.g., identifying the client)
    return api_key_header

def get_client_identifier(api_key: str = Depends(authenticate_api_key)) -> str:
    """
    Dependency that returns a client identifier based on the validated API key.
    For now, it's just the key itself, but could be mapped to a project/client name later.
    """
    # Example: Could potentially map key to a project ID or client name here
    # For now, just return the key (or a hash/prefix for logging)
    # return f"client_{hash(api_key)[:8]}" # Example using hash prefix
    return api_key # Returning the full key for now 