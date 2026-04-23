"""
Fyers OAuth2 authentication module.

Handles the complete login flow:
1. Generate auth URL for the user to open in browser
2. Exchange auth code for access token
3. Persist token to token.json with timestamp
4. Auto-detect stale tokens (date changed) and re-trigger auth flow
"""

import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

from fyers_apiv3 import fyersModel

from config import (
    FYERS_CLIENT_ID,
    FYERS_SECRET_KEY,
    FYERS_REDIRECT_URI,
    TOKEN_FILE,
)
from utils.time_utils import get_ist_now, get_today_date_str

logger = logging.getLogger(__name__)


def generate_auth_url() -> str:
    """
    Build and return the Fyers login URL for OAuth2 authorization.

    The user must open this URL in a browser, log in, and copy the
    auth_code from the redirect URL query parameter.

    Returns:
        The full authorization URL string.
    """
    session = fyersModel.SessionModel(
        client_id=FYERS_CLIENT_ID,
        secret_key=FYERS_SECRET_KEY,
        redirect_uri=FYERS_REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )
    auth_url = session.generate_authcode()
    logger.info("Auth URL generated. Open in browser to log in.")
    return auth_url


def get_access_token(auth_code: str) -> str:
    """
    Exchange an authorization code for a Fyers access token.

    Args:
        auth_code: The authorization code obtained from the redirect URL
                   after browser login.

    Returns:
        The access token string.

    Raises:
        ValueError: If the token exchange fails.
    """
    session = fyersModel.SessionModel(
        client_id=FYERS_CLIENT_ID,
        secret_key=FYERS_SECRET_KEY,
        redirect_uri=FYERS_REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    response = session.generate_token()

    if response.get("s") != "ok" and "access_token" not in response:
        error_msg = response.get("message", "Unknown error during token exchange")
        logger.error("Token exchange failed: %s", error_msg)
        raise ValueError(f"Token exchange failed: {error_msg}")

    access_token: str = response["access_token"]
    logger.info("Access token obtained successfully.")
    return access_token


def _save_token(access_token: str) -> None:
    """
    Save the access token and today's date to token.json.

    Args:
        access_token: The Fyers access token to persist.
    """
    token_data = {
        "access_token": access_token,
        "date": get_today_date_str(),
        "timestamp": get_ist_now().isoformat(),
    }
    token_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), TOKEN_FILE)
    with open(token_path, "w") as f:
        json.dump(token_data, f, indent=2)
    logger.info("Token saved to %s", token_path)


def _load_token() -> Optional[str]:
    """
    Load token from token.json if it exists and was generated today.

    Returns:
        The access token string if valid for today, otherwise None.
    """
    token_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), TOKEN_FILE)
    if not os.path.exists(token_path):
        logger.info("No token file found at %s", token_path)
        return None

    try:
        with open(token_path, "r") as f:
            token_data = json.load(f)
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Corrupt token file: %s", e)
        return None

    saved_date = token_data.get("date", "")
    today = get_today_date_str()

    if saved_date != today:
        logger.info(
            "Token is from %s, today is %s — token expired.",
            saved_date,
            today,
        )
        return None

    access_token = token_data.get("access_token", "")
    if not access_token:
        logger.warning("Token file exists but access_token is empty.")
        return None

    logger.info("Valid token loaded from %s (generated %s)", token_path, saved_date)
    return access_token


def _validate_token(access_token: str) -> bool:
    """
    Validate the token by making a simple profile API call.

    Args:
        access_token: The Fyers access token to validate.

    Returns:
        True if the token is valid, False otherwise.
    """
    try:
        fyers = fyersModel.FyersModel(
            client_id=FYERS_CLIENT_ID,
            is_async=False,
            token=access_token,
            log_path="",
        )
        response = fyers.get_profile()
        if response.get("s") == "ok":
            logger.info(
                "Token validated — logged in as %s",
                response.get("data", {}).get("name", "Unknown"),
            )
            return True
        else:
            logger.warning("Token validation failed: %s", response.get("message", ""))
            return False
    except Exception as e:
        logger.warning("Token validation error: %s", e)
        return False


def load_or_refresh_token() -> str:
    """
    Load a valid access token, or guide the user through the auth flow.

    Flow:
    1. Check token.json for a token generated today.
    2. If found, validate it with a profile API call.
    3. If not found or invalid, prompt the user to authenticate:
       a. Print the auth URL
       b. Wait for the user to paste the auth_code
       c. Exchange for access_token
       d. Save to token.json

    Returns:
        A valid Fyers access token string.

    Raises:
        SystemExit: If the user cancels or authentication fails repeatedly.
    """
    # Try loading existing token
    token = _load_token()
    if token and _validate_token(token):
        return token

    # Need fresh authentication
    logger.info("Starting fresh Fyers authentication flow...")
    print("\n" + "=" * 60)
    print("  FYERS AUTHENTICATION REQUIRED")
    print("=" * 60)

    auth_url = generate_auth_url()
    print(f"\n1. Open this URL in your browser:\n\n   {auth_url}\n")
    print("2. Log in to your Fyers account")
    print("3. After redirect, copy the 'auth_code' parameter from the URL")
    print("   (It's the value after '?auth_code=' in the redirect URL)\n")

    for attempt in range(3):
        try:
            auth_code = input("Paste the auth_code here: ").strip()
            if not auth_code:
                print("Auth code cannot be empty. Try again.")
                continue

            access_token = get_access_token(auth_code)
            _save_token(access_token)

            if _validate_token(access_token):
                print("\n✅ Authentication successful!\n")
                return access_token
            else:
                print("⚠️  Token obtained but validation failed. Try again.")

        except ValueError as e:
            print(f"\n❌ Error: {e}")
            if attempt < 2:
                print("Try again...\n")

    print("\n❌ Authentication failed after 3 attempts. Exiting.")
    logger.error("Authentication failed after 3 attempts.")
    sys.exit(1)
