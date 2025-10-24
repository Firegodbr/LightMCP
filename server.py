"""
Simplified MCP Server for Bitcoin Lightning SMS Payments

Pay-per-SMS: Create charge -> Get QR -> Pay -> Send SMS

Installation:
    uv pip install mcp twilio qrcode[pil] httpx python-dotenv
    # or separately:
    uv pip install mcp twilio qrcode Pillow httpx python-dotenv

Environment Variables:
    OPENNODE_API_KEY=your_opennode_api_key
    TWILIO_ACCOUNT_SID=your_twilio_account_sid
    TWILIO_AUTH_TOKEN=your_twilio_auth_token
    TWILIO_PHONE_NUMBER=your_twilio_phone_number
    SMS_PRICE_USD=0.10

Run:
    uv run mcp dev sms.py
"""

from urllib.parse import quote
import os
from io import BytesIO
from typing import Any

import httpx
import qrcode
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.session import ServerSession
import uvicorn
load_dotenv()

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
mcp = FastMCP(
    name="Bitcoin Lightning SMS Server",
    instructions="Simple pay-per-SMS: Create charge, scan QR, pay, send SMS"
)

# Store SMS requests awaiting payment: charge_id -> request_info
pending_sms: dict[str, dict[str, Any]] = {}


def get_sms_price() -> float:
    """Get the price per SMS in USD."""
    return float(os.getenv("SMS_PRICE_USD", "0.10"))


def get_opennode_headers() -> dict[str, str]:
    """Get headers for OpenNode API requests."""
    api_key = os.getenv("OPENNODE_API_KEY")
    if not api_key:
        raise ValueError("OPENNODE_API_KEY not set")
    return {
        "Authorization": api_key,
        "Content-Type": "application/json"
    }


def get_twilio_client() -> TwilioClient:
    """Initialize Twilio client."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise ValueError("TWILIO credentials not set")
    return TwilioClient(account_sid, auth_token)


def generate_qr_code(data: str) -> Image:
    qr = qrcode.QRCode(
        version=None,  # auto fit
        error_correction=qrcode.constants.ERROR_CORRECT_Q,  # better scanning
        box_size=12,  # Bigger boxes = larger image
        border=4,     # Standard quiet zone
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return Image(data=buffer.getvalue(), format="png")


@mcp.custom_route("/", methods=["GET", "POST"])
async def index(request: Request) -> dict[str, str]:
    return JSONResponse(content={"message": "Good and healthy!"})


@mcp.tool()
async def create_sms_payment(
    phone_number: str,
    message: str,
    user_id: str = "anonymous",
    ctx: Context[ServerSession, None] | None = None
) -> dict[str, Any]:
    """
    Create a Lightning payment to send an SMS.
    Returns charge_id and payment details. Use get_sms_qr() to get QR code.

    Args:
        phone_number: Recipient phone (E.164 format, e.g., +1234567890)
        message: SMS text to send
        user_id: Optional user identifier

    Returns:
        Payment details with charge_id for QR generation
    """
    if ctx:
        await ctx.info(f"Creating SMS payment for {phone_number}")

    try:
        price = get_sms_price()

        payload = {
            "amount": price,
            "currency": "USD",
            "description": f"SMS to {phone_number}",
            "auto_settle": False,
            "order_id": f"sms-{user_id}-{len(pending_sms)}"
        }

        # Create charge via OpenNode
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.opennode.com/v1/charges",
                json=payload,
                headers=get_opennode_headers(),
                timeout=30.0
            )
            response.raise_for_status()
            charge = response.json()

        charge_id = charge["data"]["id"]
        lightning_invoice = charge["data"]["lightning_invoice"]["payreq"]

        # Store SMS request
        # Store SMS request (now includes hosted_checkout_url)
        pending_sms[charge_id] = {
            "user_id": user_id,
            "phone_number": phone_number,
            "message": message,
            "amount": price,
            "lightning_invoice": lightning_invoice,
            "hosted_checkout_url": charge["data"]["hosted_checkout_url"],
            "status": "pending",
            "sent": False
        }

        if ctx:
            await ctx.info(f"Payment created: {charge_id}")

        return {
            "charge_id": charge_id,
            "amount": price,
            "currency": "USD",
            "phone_number": phone_number,
            "lightning_invoice": lightning_invoice,
            "hosted_checkout_url": charge["data"]["hosted_checkout_url"],
            "expires_at": charge["data"]["lightning_invoice"]["expires_at"],
            "instructions": "Call get_sms_qr(charge_id) to get QR code, then pay_and_send_sms(charge_id)"
        }

    except httpx.HTTPStatusError as e:
        error_msg = f"OpenNode API error: {e.response.status_code} - {e.response.text}"
        if ctx:
            await ctx.error(error_msg)
        raise ValueError(error_msg)
    except Exception as e:
        if ctx:
            await ctx.error(f"Error: {str(e)}")
        raise


@mcp.tool()
async def get_sms_qr(
    charge_id: str,
    ctx: Context[ServerSession, None] | None = None
) -> Image:
    """
    Generate QR code for the Lightning payment.

    Args:
        charge_id: The charge ID from create_sms_payment

    Returns:
        QR code image to scan with Lightning wallet
    """
    if ctx:
        await ctx.info(f"Generating QR for {charge_id}")

    if charge_id not in pending_sms:
        raise ValueError(f"Charge {charge_id} not found")

    lightning_invoice = pending_sms[charge_id]["lightning_invoice"]
    return generate_qr_code(lightning_invoice)


def generate_lightning_deep_link(invoice: str) -> str:
    """
    Returns a deep link using the lightning: URI scheme.
    Example: lightning:lntb1...
    We percent-encode the invoice just in case.
    """
    return f"lightning:{quote(invoice, safe='')}"


@mcp.tool()
async def get_sms_qr_with_link(
    charge_id: str,
    ctx: Context[ServerSession, None] | None = None
) -> dict[str, Any]:
    """
    Return QR image plus mobile-friendly deep link and HTML fallback.
    Useful for mobile webviews or clients that can render HTML.
    """
    if ctx:
        await ctx.info(f"Generating QR + link for {charge_id}")

    if charge_id not in pending_sms:
        raise ValueError(f"Charge {charge_id} not found")

    data = pending_sms[charge_id]
    invoice = data["lightning_invoice"]

    # image
    image = generate_qr_code(invoice)

    # deep link
    deep_link = generate_lightning_deep_link(invoice)

    # Fallback hosted checkout URL (if present when the charge was created)
    # If you want the hosted_checkout_url available, store it during create_sms_payment
    # (modify create_sms_payment to save charge["data"]["hosted_checkout_url"] into pending_sms)
    hosted_checkout = data.get("hosted_checkout_url", None)

    # Simple HTML snippet for mobile: attempts to open lightning: link, then falls back
    # to hosted checkout or shows the invoice text. This is safe to render in a webview.
    html_snippet = f"""
    <!doctype html>
    <html>
      <head>
        <meta name="viewport" content="width=device-width,initial-scale=1"/>
        <title>Pay with Lightning</title>
        <script>
          function openWallet() {{
            // Try to open via lightning: URI scheme
            window.location = "{deep_link}";
            // On many mobile browsers this will open a wallet; if not, we let fallback occur below.
            // After a short delay, redirect to hosted checkout if available.
            setTimeout(function() {{
              {"window.location = %s;" % ("'" + hosted_checkout + "'") if hosted_checkout else "document.getElementById('fallback').style.display='block';"}
            }}, 1200);
          }}
          // On load, attempt automatically (useful in mobile webview)
          window.addEventListener('load', function() {{
            openWallet();
          }});
        </script>
        <style>body {{ font-family: sans-serif; text-align:center; padding:20px; }}</style>
      </head>
      <body>
        <h2>Pay with Lightning</h2>
        <p>Tap the button below if your wallet didn't open automatically.</p>
        <p><a href="{deep_link}" style="display:inline-block;padding:12px 18px;background:#111;color:#fff;border-radius:8px;text-decoration:none;">Open wallet</a></p>
        <div id="fallback" style="display:none;">
          <p>If your wallet doesn't open, use this link:</p>
          <p><a href="{hosted_checkout if hosted_checkout else deep_link}">{hosted_checkout if hosted_checkout else deep_link}</a></p>
        </div>
        <hr/>
        <p style="font-size:0.85em;color:#666;">Or scan the QR code shown by the app.</p>
      </body>
    </html>
    """

    # Return both the image and strings. MCP frontends that can render images will show `Image`.
    # If your client expects only an Image, you can return image directly instead.
    return {
    "deep_link": deep_link,
    "hosted_checkout_url": hosted_checkout,
    "mobile_html": html_snippet,
    "instructions": "Scan QR separately, or tap deep_link to open a wallet."
}



@mcp.tool()
async def pay_and_send_sms(
    charge_id: str,
    ctx: Context[ServerSession, None] | None = None
) -> dict[str, Any]:
    """
    Check if payment received and send SMS if paid.

    Args:
        charge_id: The charge ID to check

    Returns:
        Payment status and SMS delivery result
    """
    if ctx:
        await ctx.info(f"Checking payment for {charge_id}")

    if charge_id not in pending_sms:
        raise ValueError(f"Charge {charge_id} not found")

    try:
        # Check payment status
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://api.opennode.com/v1/charge/{charge_id}",
                headers=get_opennode_headers(),
                timeout=30.0
            )
            response.raise_for_status()
            charge = response.json()

        status = charge["data"]["status"]
        sms_request = pending_sms[charge_id]

        if status != "paid":
            return {
                "charge_id": charge_id,
                "status": status,
                "paid": False,
                "sms_sent": False,
                "message": f"Payment not received yet (status: {status})"
            }

        # Payment received - send SMS if not already sent
        if not sms_request["sent"]:
            twilio = get_twilio_client()
            from_number = os.getenv("TWILIO_PHONE_NUMBER")

            if not from_number:
                raise ValueError("TWILIO_PHONE_NUMBER not set")

            sms = twilio.messages.create(
                body=sms_request["message"],
                from_=from_number,
                to=sms_request["phone_number"]
            )

            # Mark as sent
            pending_sms[charge_id]["sent"] = True
            pending_sms[charge_id]["status"] = "completed"
            pending_sms[charge_id]["sms_sid"] = sms.sid

            if ctx:
                await ctx.info(f"SMS sent successfully: {sms.sid}")

            return {
                "charge_id": charge_id,
                "status": "completed",
                "paid": True,
                "sms_sent": True,
                "sms_sid": sms.sid,
                "to": sms_request["phone_number"],
                "from": from_number,
                "paid_at": charge["data"].get("paid_at"),
                "message": "Payment received and SMS sent successfully!"
            }
        else:
            return {
                "charge_id": charge_id,
                "status": "completed",
                "paid": True,
                "sms_sent": True,
                "message": "SMS already sent for this payment"
            }

    except httpx.HTTPStatusError as e:
        error_msg = f"OpenNode API error: {e.response.status_code} - {e.response.text}"
        if ctx:
            await ctx.error(error_msg)
        raise ValueError(error_msg)
    except Exception as e:
        if ctx:
            await ctx.error(f"Error: {str(e)}")
        raise


@mcp.tool()
async def check_charge_status(
    charge_id: str,
    ctx: Context[ServerSession, None] | None = None
) -> dict[str, Any]:
    """
    Check payment and SMS status without attempting to send.

    Args:
        charge_id: The charge ID to check

    Returns:
        Current status information
    """
    if charge_id not in pending_sms:
        raise ValueError(f"Charge {charge_id} not found")

    sms_request = pending_sms[charge_id]

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://api.opennode.com/v1/charge/{charge_id}",
                headers=get_opennode_headers(),
                timeout=30.0
            )
            response.raise_for_status()
            charge = response.json()

        return {
            "charge_id": charge_id,
            "payment_status": charge["data"]["status"],
            "sms_sent": sms_request["sent"],
            "phone_number": sms_request["phone_number"],
            "amount": sms_request["amount"],
            "created_at": charge["data"]["created_at"],
            "paid_at": charge["data"].get("paid_at")
        }

    except Exception as e:
        if ctx:
            await ctx.error(f"Error: {str(e)}")
        raise


@mcp.resource("sms://instructions")
def instructions() -> str:
    """Instructions for using the SMS service."""
    price = get_sms_price()
    return f"""
Simple Bitcoin Lightning SMS Service

💰 Price: ${price} USD per SMS

📱 How to Use (3 steps):

1. Create Payment:
   charge = create_sms_payment("+1234567890", "Hello World!")

2. Get QR Code & Pay:
    
    💻Desktop Wallet Users
    qr = get_sms_qr(charge["charge_id"])
    # Scan QR with Lightning wallet and pay
    
    📱 Mobile Wallet Users

    Use get_sms_qr_with_link(charge_id) to get:

    A lightning: deep link (opens your wallet app)

    A fallback HTML snippet (redirects to wallet or hosted checkout)

3. Send SMS:
   result = pay_and_send_sms(charge["charge_id"])
   # Automatically sends SMS if payment received

Optional:
- check_charge_status(charge_id) - Check status without sending

That's it! No packages, no credits, just pay and send.
"""


if __name__ == "__main__":
    mcp.run(transport="sse")