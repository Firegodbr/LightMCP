import aiohttp
import os
from typing import Dict, Optional

class OpenNodeClient:
    """Lightning Network client using OpenNode API"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.opennode.com/v1"
        
    async def create_invoice(self, amount_sats: int, description: str) -> Dict:
        """Create a Lightning invoice via OpenNode"""
        invoice_data = {
            "amount": amount_sats,
            "currency": "BTC",
            "description": description,
            "auto_settle": False,
            "callback_url": os.getenv("WEBHOOK_URL", "")
        }
        
        headers = {
            'Content-Type': 'application/json',
            'Authorization': self.api_key
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/charges",
                json=invoice_data,
                headers=headers
            ) as resp:
                if resp.status == 200 or resp.status == 201:
                    data = await resp.json()
                    charge = data['data']
                    return {
                        "payment_request": charge['lightning_invoice']['payreq'],
                        "payment_hash": charge['id'],
                        "expires_at": charge['lightning_invoice']['expires_at']
                    }
                else:
                    error = await resp.text()
                    raise Exception(f"OpenNode API error: {error}")
    
    async def check_payment(self, charge_id: str) -> bool:
        """Check if payment has been received"""
        headers = {'Authorization': self.api_key}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base_url}/charge/{charge_id}",
                headers=headers
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    status = data['data']['status']
                    return status == 'paid'
                return False