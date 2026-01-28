from decimal import Decimal
from typing import List, Dict, Any
import re
# from django.core.mail import EmailMessage
# from django.conf import settings
# from playwright.sync_api import sync_playwright
# import io

# def send_receipt_email(receipt):
#     """
#     1. Generates PDF by visiting the Next.js URL
#     2. Sends it to the customer's email
#     """
#     # The URL of your Next.js Print View
#     # Note: Use an internal URL if they are on the same network
#     print_url = f"https://your-app.com/api/print/receipt/{receipt.id}?secret={settings.INTERNAL_PRINT_KEY}"

#     with sync_playwright() as p:
#         browser = p.chromium.launch()
#         page = browser.new_page()
#         page.goto(print_url, wait_until="networkidle") # Wait for React to load
#         pdf_content = page.pdf(format="A4", print_background=True)
#         browser.close()

#     # Create the Email
#     email = EmailMessage(
#         subject=f"Receipt from {receipt.business.name}",
#         body=f"Hi {receipt.customer_name}, please find your receipt attached.",
#         from_email=settings.DEFAULT_FROM_EMAIL,
#         to=[receipt.customer_email],
#     )

#     # Attach the PDF we just generated
#     email.attach(f"receipt_{receipt.receipt_number}.pdf", pdf_content, "application/pdf")
#     email.send()

def calculate_receipt_totals(
    items: List[Dict[str, Any]],
    tax_enabled: bool,
    tax_rate: Decimal,
    discount: Decimal = Decimal('0.00')
) -> Dict[str, Decimal]:
    """
    Calculate receipt totals from items
    Matches TypeScript calculateReceiptTotals utility
    """
    subtotal = sum(
        Decimal(str(item['quantity'])) * Decimal(str(item['unitPrice']))
        for item in items
    )
    
    tax_amount = subtotal * tax_rate if tax_enabled else Decimal('0.00')
    grand_total = subtotal + tax_amount - discount
    
    return {
        'subtotal': subtotal,
        'taxAmount': tax_amount,
        'grandTotal': grand_total
    }


def generate_receipt_number(last_number: str = None) -> str:
    """
    Generate next receipt number
    Matches TypeScript generateReceiptNumber utility
    """
    if not last_number:
        return '#001'
    
    match = re.search(r'\d+', last_number)
    if not match:
        return '#001'
    
    next_num = int(match.group(0)) + 1
    return f"#{str(next_num).zfill(3)}"