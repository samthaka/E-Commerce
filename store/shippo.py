import requests
from django.conf import settings


class ShippoError(Exception):
    pass


class InvalidParcelError(ShippoError):
    pass


class ShippoClient:
    def __init__(self):
        self.token = settings.SHIPPO_API_TOKEN
        if not self.token:
            raise ShippoError("Shippo API token is not configured.")
        if settings.SHIPPO_TEST_MODE and not self.token.startswith("shippo_test_"):
            raise ShippoError("Shippo test mode requires a test API token.")

    def request(self, method, endpoint, payload=None):
        try:
            response = requests.request(
                method,
                f"{settings.SHIPPO_API_URL.rstrip('/')}/{endpoint.lstrip('/')}",
                headers={
                    "Authorization": f"ShippoToken {self.token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise ShippoError("Shippo could not be reached. Please try again.") from exc

        try:
            result = response.json()
        except ValueError as exc:
            raise ShippoError("Shippo returned an unreadable response.") from exc
        if not isinstance(result, dict):
            raise ShippoError("Shippo returned an unexpected response.")

        if not response.ok:
            detail = result.get("detail") or result.get("message") or "Shippo request failed."
            raise ShippoError(str(detail))
        return result

    def create_shipment(self, sender, recipient, parcels):
        result = self.request(
            "POST",
            "/shipments/",
            {
                "address_from": sender,
                "address_to": recipient,
                "parcels": parcels,
                "async": False,
            },
        )
        return result

    def create_transaction(self, rate_id):
        result = self.request(
            "POST",
            "/transactions/",
            {
                "rate": rate_id,
                "label_file_type": "PDF",
                "async": False,
            },
        )
        if result.get("status") != "SUCCESS":
            detail = result.get("messages") or result.get("status_details") or "Label purchase failed."
            raise ShippoError(str(detail))
        return result

    def track(self, carrier, tracking_number):
        if not carrier or not tracking_number:
            raise ShippoError("A carrier and tracking number are required.")
        return self.request("POST", f"/tracks/{carrier.lower()}/{tracking_number}/")


def serialize_rates(rates):
    serialized = []
    for rate in rates:
        if not rate.get("object_id") or rate.get("amount") is None:
            continue
        service_level = rate.get("servicelevel") or {}
        service_level_name = (
            service_level.get("name", "")
            if isinstance(service_level, dict)
            else str(service_level)
        )
        serialized.append({
            "object_id": rate["object_id"],
            "amount": rate["amount"],
            "currency": rate.get("currency", "USD"),
            "provider": rate.get("provider", ""),
            "servicelevel": service_level_name,
            "estimated_days": rate.get("estimated_days"),
            "duration_terms": rate.get("duration_terms", ""),
        })
    return serialized


def find_rate(rates, rate_id):
    return next((rate for rate in rates if rate.get("object_id") == rate_id), None)


def transaction_fields(transaction):
    tracking_number = transaction.get("tracking_number") or ""
    fields = {
        "tracking_number": tracking_number,
        "tracking_url": transaction.get("tracking_url_provider") or "",
        "label_url": transaction.get("label_url") or "",
        "carrier": transaction.get("provider") or transaction.get("carrier") or "",
        "tracking_status": "pre_transit" if tracking_number else "",
    }
    return {field: value for field, value in fields.items() if value}


def order_parcels(order):
    parcels = []
    for item in order.orderitem_set.select_related("product"):
        product = item.product
        if product is None or product.digital:
            continue
        dimensions = (
            product.shipping_weight,
            product.shipping_length,
            product.shipping_width,
            product.shipping_height,
        )
        if any(value is None or value <= 0 for value in dimensions):
            raise InvalidParcelError(
                f"Shipping weight and dimensions are required for {product.name or 'a product'}."
            )
        if item.quantity is None or item.quantity < 1:
            raise InvalidParcelError(
                f"A valid quantity is required for {product.name or 'a product'}."
            )
        parcels.extend(
            {
                "length": str(product.shipping_length),
                "width": str(product.shipping_width),
                "height": str(product.shipping_height),
                "distance_unit": "in",
                "weight": str(product.shipping_weight),
                "mass_unit": "lb",
            }
            for _ in range(item.quantity)
        )
    return parcels


def origin_address():
    return {
        "name": settings.SHIPPO_FROM_NAME,
        "street1": settings.SHIPPO_FROM_STREET,
        "city": settings.SHIPPO_FROM_CITY,
        "state": settings.SHIPPO_FROM_STATE,
        "zip": settings.SHIPPO_FROM_ZIP,
        "country": "US",
        "email": settings.SHIPPO_FROM_EMAIL,
    }


def recipient_address(data, email=""):
    return {
        "name": data.get("name", ""),
        "street1": data.get("address", ""),
        "city": data.get("city", ""),
        "state": data.get("state", ""),
        "zip": data.get("zipcode", ""),
        "country": "US",
        "email": email,
    }
