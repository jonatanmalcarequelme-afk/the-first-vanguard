import json
import os
import re
import secrets
import time
from decimal import Decimal
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CATALOG = {
    "Camiseta Vanguard": Decimal("28.00"),
    "Hoodie First Move": Decimal("56.00"),
    "Chaqueta Vanguard": Decimal("74.00"),
    "Top Asimétrico": Decimal("36.00"),
    "Gorra Vanguard": Decimal("24.00"),
    "Lentes Eclipse": Decimal("38.00"),
    "Cartera Nocturna": Decimal("32.00"),
    "Mochila Vector": Decimal("68.00"),
    "Colgante Cruz Estriada": Decimal("29.00"),
    "Colgante Cruz Filigrana": Decimal("32.00"),
    "Colgante Cruz Sombra": Decimal("30.00"),
    "Colgante Cruz Cristal": Decimal("36.00"),
    "Colgante Cruz Gótica": Decimal("34.00"),
    "Colgante Cruz Medallón": Decimal("32.00"),
    "Pulsera Cadena Doble": Decimal("18.00"),
    "Pulsera Eslabón Clásico": Decimal("26.00"),
    "Pulsera Eslabón Entrelazado": Decimal("24.00"),
    "Pulsera Trenzada": Decimal("25.00"),
    "Pulsera Eslabón Grueso": Decimal("29.00"),
}


def load_local_environment():
    environment_file = ROOT / ".env"
    if not environment_file.exists():
        return

    for raw_line in environment_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def validate_billing(data):
    if not isinstance(data, dict):
        raise ValueError("Completa tus datos de facturación.")

    limits = {
        "firstName": 80,
        "lastName": 80,
        "email": 160,
        "phoneNumber": 24,
        "document": 20,
        "street": 160,
        "city": 80,
        "state": 80,
        "postalCode": 20,
    }
    billing = {}
    for field, maximum_length in limits.items():
        value = data.get(field)
        if not isinstance(value, str):
            raise ValueError("Completa todos los datos solicitados.")
        value = value.strip()
        if not value or len(value) > maximum_length:
            raise ValueError("Revisa los datos de facturación ingresados.")
        billing[field] = value

    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", billing["email"]):
        raise ValueError("Ingresa un correo electrónico válido.")
    if not re.fullmatch(r"[0-9+()\-\s]{7,24}", billing["phoneNumber"]):
        raise ValueError("Ingresa un número de teléfono válido.")
    if not re.fullmatch(r"[A-Za-z0-9-]{6,20}", billing["document"]):
        raise ValueError("Revisa el número de documento ingresado.")

    document_type = data.get("documentType", "DNI")
    if document_type not in {"DNI", "CE", "PAS", "RUC"}:
        raise ValueError("El tipo de documento no es válido.")
    billing["documentType"] = document_type
    return billing


class StorefrontHandler(SimpleHTTPRequestHandler):
    server_version = "VanguardStore/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path == "/api/izipay/health":
            configured = bool(os.getenv("IZIPAY_MERCHANT_CODE") and os.getenv("IZIPAY_PUBLIC_KEY"))
            self.send_json(200, {"configured": configured, "mode": os.getenv("IZIPAY_MODE", "sandbox")})
            return

        path_parts = path.replace("\\", "/").split("/")
        if any(part.startswith(".") or part.endswith(".py") for part in path_parts):
            self.send_error(404)
            return
        super().do_GET()

    def do_POST(self):
        if urlsplit(self.path).path != "/api/izipay/session":
            self.send_json(404, {"error": "Ruta no encontrada."})
            return

        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self.send_json(415, {"error": "La solicitud debe enviarse como JSON."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Solicitud no válida."})
            return
        if content_length <= 0 or content_length > 32768:
            self.send_json(413, {"error": "El contenido de la solicitud no es válido."})
            return

        try:
            payload = json.loads(self.rfile.read(content_length))
            billing = validate_billing(payload.get("billing"))
            items = payload.get("items")
            if not isinstance(items, list) or not items or len(items) > len(CATALOG):
                raise ValueError("La bolsa no contiene productos válidos.")

            quantities = {}
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("La bolsa contiene un producto no válido.")
                name = item.get("name")
                quantity = item.get("quantity")
                if name not in CATALOG or type(quantity) is not int or not 1 <= quantity <= 20:
                    raise ValueError("La bolsa contiene un producto o cantidad no válida.")
                quantities[name] = quantities.get(name, 0) + quantity

            total = sum((CATALOG[name] * quantity for name, quantity in quantities.items()), Decimal("0.00"))
            if total <= 0 or total > Decimal("100000.00"):
                raise ValueError("El total del pedido no es válido.")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError, ValueError) as error:
            self.send_json(400, {"error": str(error) or "Solicitud no válida."})
            return

        merchant_code = os.getenv("IZIPAY_MERCHANT_CODE", "").strip()
        public_key = os.getenv("IZIPAY_PUBLIC_KEY", "").strip()
        if not merchant_code or not public_key:
            self.send_json(503, {"error": "Configura IZIPAY_MERCHANT_CODE e IZIPAY_PUBLIC_KEY en el archivo .env local."})
            return

        mode = os.getenv("IZIPAY_MODE", "sandbox").strip().lower()
        if mode not in {"sandbox", "production"}:
            self.send_json(500, {"error": "IZIPAY_MODE debe ser sandbox o production."})
            return

        api_host = "sandbox-api-pw.izipay.pe" if mode == "sandbox" else "api-pw.izipay.pe"
        sdk_url = "https://sandbox-checkout.izipay.pe/payments/v1/js/index.js" if mode == "sandbox" else "https://checkout.izipay.pe/payments/v1/js/index.js"
        order_number = f"{int(time.time() * 1000) % 10_000_000_000:010d}"
        transaction_id = f"{int(time.time() * 1000)}{secrets.randbelow(900) + 100}"
        token_request_body = {
            "requestSource": "ECOMMERCE",
            "merchantCode": merchant_code,
            "orderNumber": order_number,
            "publicKey": public_key,
            "amount": f"{total:.2f}",
        }
        request = Request(
            f"https://{api_host}/security/v1/Token/Generate",
            data=json.dumps(token_request_body).encode("utf-8"),
            headers={"Content-Type": "application/json", "transactionId": transaction_id},
            method="POST",
        )

        try:
            with urlopen(request, timeout=20) as response:
                provider_payload = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
            self.send_json(502, {"error": "Izipay no pudo crear la sesión. Revisa las credenciales y el entorno del comercio."})
            return

        if not isinstance(provider_payload, dict):
            self.send_json(502, {"error": "Izipay devolvió una respuesta no válida."})
            return
        token = provider_payload.get("response", {}).get("token")
        if not token:
            self.send_json(502, {"error": "Izipay no devolvió un token de pago válido."})
            return

        self.send_json(200, {
            "token": token,
            "transactionId": transaction_id,
            "orderNumber": order_number,
            "merchantCode": merchant_code,
            "amount": f"{total:.2f}",
            "sdkUrl": sdk_url,
            "billing": billing,
        })


def load_local_environment():
    environment_file = ROOT / ".env"
    if not environment_file.exists():
        return
    for raw_line in environment_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main():
    load_local_environment()
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5500"))
    server = ThreadingHTTPServer((host, port), StorefrontHandler)
    print(f"Tienda disponible en http://{host}:{port}/")
    print(f"Entorno Izipay: {os.getenv('IZIPAY_MODE', 'sandbox')}")
    server.serve_forever()


if __name__ == "__main__":
    main()
