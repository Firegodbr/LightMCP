"""
MCP Server for Bitcoin Lightning SMS Payments
Smithery-compatible version
"""

from urllib.parse import quote
from io import BytesIO
from typing import Any

import httpx
import qrcode
from twilio.rest import Client as TwilioClient
from pydantic import BaseModel, Field

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.session import ServerSession
from smithery.decorators import smithery


# Configuration schema for session-specific settings
class ConfigSchema(BaseModel):
    opennode_api_key: str = Field(..., description="OpenNode API key for Lightning payments")
    twilio_account_sid: str = Field(..., description="Twilio Account SID")
    twilio_auth_token: str = Field(..., description="Twilio Auth Token")
    twilio_phone_number: str = Field(..., description="Twilio phone number (E.164 format)")
    sms_price_usd: float = Field(0.10, description="Price per SMS in USD")


@smithery.server(config_schema=ConfigSchema)
def create_server():
    """Create and configure the SMS payment MCP server."""
    
    mcp = FastMCP(
        name="Bitcoin Lightning SMS Server",
        instructions="Simple pay-per-SMS: Create charge, scan QR, pay, send SMS"
    )

    # Store SMS requests awaiting payment: charge_id -> request_info
    pending_sms: dict[str, dict[str, Any]] = {}

    def get_opennode_headers(ctx: Context) -> dict[str, str]:
        """Get headers for OpenNode API requests."""
        api_key = ctx.session_config.opennode_api_key
        return {
            "Authorization": api_key,
            "Content-Type": "application/json"
        }

    def get_twilio_client(ctx: Context) -> TwilioClient:
        """Initialize Twilio client."""
        config = ctx.session_config
        return TwilioClient(config.twilio_account_sid, config.twilio_auth_token)

    def generate_qr_code(data: str) -> Image:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_Q,
            box_size=12,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        return Image(data=buffer.getvalue(), format="png")

    def generate_lightning_deep_link(invoice: str) -> str:
        """Returns a deep link using the lightning: URI scheme."""
        return f"lightning:{quote(invoice, safe='')}"

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
            price = ctx.session_config.sms_price_usd

            payload = {
                "amount": price,
                "currency": "USD",
                "description": f"SMS to {phone_number}",
                "auto_settle": False,
                "order_id": f"sms-{user_id}-{len(pending_sms)}"
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.opennode.com/v1/charges",
                    json=payload,
                    headers=get_opennode_headers(ctx),
                    timeout=30.0
                )
                response.raise_for_status()
                charge = response.json()

            charge_id = charge["data"]["id"]
            lightning_invoice = charge["data"]["lightning_invoice"]["payreq"]

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
        hosted_checkout = data.get("hosted_checkout_url", None)
        deep_link = generate_lightning_deep_link(invoice)

        html_snippet = f"""
        <!doctype html>
        <html>
          <head>
            <meta name="viewport" content="width=device-width,initial-scale=1"/>
            <title>Pay with Lightning</title>
            <script>
              function openWallet() {{
                window.location = "{deep_link}";
                setTimeout(function() {{
                  {"window.location = %s;" % ("'" + hosted_checkout + "'") if hosted_checkout else "document.getElementById('fallback').style.display='block';"}
                }}, 1200);
              }}
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
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://api.opennode.com/v1/charge/{charge_id}",
                    headers=get_opennode_headers(ctx),
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

            if not sms_request["sent"]:
                twilio = get_twilio_client(ctx)
                from_number = ctx.session_config.twilio_phone_number

                sms = twilio.messages.create(
                    body=sms_request["message"],
                    from_=from_number,
                    to=sms_request["phone_number"]
                )

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
                    headers=get_opennode_headers(ctx),
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
        return """
Simple Bitcoin Lightning SMS Service

💰 Price: Configured per session

📱 How to Use (3 steps):

1. Create Payment:
   charge = create_sms_payment("+1234567890", "Hello World!")

2. Get QR Code & Pay:
    
    💻Desktop Wallet Users
    qr = get_sms_qr(charge["charge_id"])
    # Scan QR with Lightning wallet and pay
    
    📱 Mobile Wallet Users
    Use get_sms_qr_with_link(charge_id) to get:
    - A lightning: deep link (opens your wallet app)
    - A fallback HTML snippet (redirects to wallet or hosted checkout)

3. Send SMS:
   result = pay_and_send_sms(charge["charge_id"])
   # Automatically sends SMS if payment received

Optional:
- check_charge_status(charge_id) - Check status without sending

That's it! No packages, no credits, just pay and send.
"""

    return mcp