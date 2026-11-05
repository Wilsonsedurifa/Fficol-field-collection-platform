"""
FiCol Bulk Account Importer & Geocoding Engine
Handles CSV & Excel parsing, OpenStreetMap Nominatim / Philippine heuristic geocoding,
validation preview, and atomic ingestion into FiCol database.
"""
import csv
import io
import re
import hashlib
import urllib.request
import urllib.parse
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime, date

from django.conf import settings
from django.contrib.gis.geos import Point
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account, Client, Customer, CustomerAddress
from apps.accounts.information.models import CustomerContact
from apps.collection.models import CommissionRule
from apps.audit.services import append_audit_event


TRIAL_IMPORT_CLIENT_CODE = "TRIAL-IMPORT"
TRIAL_IMPORT_RULE_CODE = "TRIAL-NO-PAYOUT"


def get_trial_import_defaults():
    """Return the internal portfolio and zero-payout rule used by bulk imports.

    The importer still preserves the required account relationships, but Back
    Office users don't have to set up creditor or payout configuration merely
    to put trial accounts on the map.
    """
    with transaction.atomic():
        client, _ = Client.objects.get_or_create(
            code=TRIAL_IMPORT_CLIENT_CODE,
            defaults={"name": "Trial Import Portfolio", "is_active": True},
        )
        rule, _ = CommissionRule.objects.get_or_create(
            client=client,
            code=TRIAL_IMPORT_RULE_CODE,
            version=1,
            defaults={
                "name": "Trial — No Payout",
                "kind": CommissionRule.Kind.FIXED,
                "fixed_amount": Decimal("0.00"),
                "currency": "PHP",
                "earning_basis": CommissionRule.EarningBasis.VERIFIED_PAYMENT,
                "fixed_frequency": CommissionRule.FixedFrequency.ONCE_PER_ACCOUNT,
                "is_active": True,
            },
        )
    return client, rule


def grant_trial_import_access_to_collectors(client):
    """Give existing collectors access to the internal trial portfolio only."""
    if client.code != TRIAL_IMPORT_CLIENT_CODE:
        return

    # Imported trial accounts are meant to be immediately testable on the
    # collector offers map.  Real client portfolios retain their normal,
    # explicitly managed access boundaries.
    from apps.user.models import ClientAccess, CollectorProfile

    collector_user_ids = CollectorProfile.objects.filter(
        approval_status=CollectorProfile.ApprovalStatus.APPROVED,
    ).values_list("user_id", flat=True)
    for user_id in collector_user_ids:
        ClientAccess.objects.get_or_create(
            user_id=user_id,
            client=client,
            defaults={"is_active": True},
        )


# High-precision coordinates for major Philippine Cities & Municipalities (Offline / Fallback Geocoder)
PH_CITIES_COORDINATES = {
    # Metro Manila (NCR)
    "pasig": (14.5764, 121.0851),
    "quezon city": (14.6760, 121.0437),
    "qc": (14.6760, 121.0437),
    "manila": (14.5995, 120.9842),
    "makati": (14.5547, 121.0244),
    "taguig": (14.5176, 121.0509),
    "bgc": (14.5507, 121.0489),
    "mandaluyong": (14.5794, 121.0359),
    "san juan": (14.6019, 121.0355),
    "marikina": (14.6507, 121.1029),
    "caloocan": (14.6540, 120.9839),
    "malabon": (14.6625, 120.9566),
    "navotas": (14.6667, 120.9417),
    "valenzuela": (14.7011, 120.9830),
    "pasay": (14.5378, 120.9996),
    "paranaque": (14.4793, 121.0198),
    "parañaque": (14.4793, 121.0198),
    "las pinas": (14.4445, 120.9939),
    "las piñas": (14.4445, 120.9939),
    "muntinlupa": (14.4081, 121.0415),
    "alabang": (14.4250, 121.0360),

    # Cavite
    "bacoor": (14.4311, 120.9417),
    "imus": (14.4296, 120.9367),
    "dasmarinas": (14.3294, 120.9367),
    "dasmariñas": (14.3294, 120.9367),
    "general trias": (14.3867, 120.8811),
    "gentri": (14.3867, 120.8811),
    "cavite city": (14.4833, 120.9000),
    "kawit": (14.4450, 120.9020),
    "silang": (14.2300, 120.9750),
    "tagaytay": (14.1153, 120.9621),

    # Laguna
    "san pedro": (14.3583, 121.0583),
    "binan": (14.3333, 121.0833),
    "biñan": (14.3333, 121.0833),
    "santa rosa": (14.3167, 121.1167),
    "sta rosa": (14.3167, 121.1167),
    "cabuyao": (14.2783, 121.1250),
    "calamba": (14.2167, 121.1667),
    "los banos": (14.1700, 121.2400),
    "los baños": (14.1700, 121.2400),
    "san pablo": (14.0683, 121.3250),

    # Rizal
    "antipolo": (14.5842, 121.1763),
    "cainta": (14.5772, 121.1147),
    "taytay": (14.5583, 121.1333),
    "angono": (14.5260, 121.1550),
    "san mateo": (14.6975, 121.1219),
    "rodriguez": (14.7333, 121.1500),
    "montalban": (14.7333, 121.1500),
    "binangonan": (14.4644, 121.1922),

    # Bulacan
    "san jose del monte": (14.8139, 121.0453),
    "sjdm": (14.8139, 121.0453),
    "meycauayan": (14.7369, 120.9606),
    "marilao": (14.7578, 120.9481),
    "malolos": (14.8433, 120.8114),
    "bocaue": (14.7980, 120.9270),
    "sta maria": (14.8180, 120.9570),

    # Pampanga
    "san fernando": (15.0286, 120.6897),
    "angeles": (15.1450, 120.5887),
    "mabalacat": (15.2230, 120.5750),

    # Central Visayas & Mindanao
    "cebu city": (10.3157, 123.8854),
    "cebu": (10.3157, 123.8854),
    "mandaue": (10.3333, 123.9333),
    "lapu-lapu": (10.3103, 123.9494),
    "davao": (7.1907, 125.4553),
    "davao city": (7.1907, 125.4553),
    "cagayan de oro": (8.4542, 124.6319),
    "cdo": (8.4542, 124.6319),
    "iloilo": (10.7202, 122.5621),
    "bacolod": (10.6765, 122.9509),
}


def normalize_column_name(col):
    if not col:
        return ""
    c = str(col).strip().lower().replace("_", " ").replace("-", " ")
    aliases = {
        "customer": "customer_name",
        "customer name": "customer_name",
        "name": "customer_name",
        "borrower": "customer_name",
        "account": "account_no",
        "account no": "account_no",
        "account number": "account_no",
        "acc no": "account_no",
        "reference": "account_no",
        "ref": "account_no",
        "balance": "balance",
        "outstanding balance": "balance",
        "amount due": "balance",
        "principal": "principal",
        "principal amount": "principal",
        "phone": "phone",
        "phone number": "phone",
        "mobile": "phone",
        "contact": "phone",
        "contact number": "phone",
        "cellphone": "phone",
        "address": "address",
        "street": "address",
        "address line": "address",
        "barangay": "barangay",
        "bgy": "barangay",
        "brgy": "barangay",
        "city": "city",
        "municipality": "city",
        "province": "province",
        "lat": "latitude",
        "latitude": "latitude",
        "lng": "longitude",
        "lon": "longitude",
        "long": "longitude",
        "longitude": "longitude",
        "due date": "due_date",
        "first action due": "due_date",
        "notes": "notes",
        "remarks": "notes",
    }
    return aliases.get(c, c)


def geocode_address(address_line, barangay="", city="", raw_lat=None, raw_lng=None):
    """
    Multi-Tier Geocoder:
    1. Direct coords if supplied in file.
    2. OpenStreetMap Nominatim live search.
    3. Philippine Heuristic Offline / City Gazetteer + deterministic neighborhood jitter.
    """
    # 1. Direct supplied coordinates
    if raw_lat is not None and raw_lng is not None:
        try:
            lat = float(raw_lat)
            lng = float(raw_lng)
            if -90 <= lat <= 90 and -180 <= lng <= 180 and not (lat == 0 and lng == 0):
                return {
                    "latitude": round(lat, 6),
                    "longitude": round(lng, 6),
                    "source": "supplied",
                    "status_label": "Direct Coords (File)",
                }
        except (ValueError, TypeError):
            pass

    full_query = f"{address_line}, {barangay}, {city}, Philippines".strip(", ")
    clean_city = str(city).strip().lower()
    clean_brgy = str(barangay).strip().lower()

    # 2. Try Nominatim if network allows (timeout = 2.5s)
    if full_query and len(full_query) > 5:
        try:
            headers = {"User-Agent": "FiCol-Geocoding-Engine/1.0 (admin@ficol.ph)"}
            params = urllib.parse.urlencode({
                "q": full_query,
                "format": "json",
                "limit": "1",
                "countrycodes": "ph",
            })
            url = f"https://nominatim.openstreetmap.org/search?{params}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data and len(data) > 0:
                    lat = float(data[0]["lat"])
                    lng = float(data[0]["lon"])
                    return {
                        "latitude": round(lat, 6),
                        "longitude": round(lng, 6),
                        "source": "nominatim",
                        "status_label": "Nominatim Live GPS",
                    }
        except Exception:
            pass  # Fall through to Philippine Gazetteer fallback

    # 3. Philippine Heuristic / Gazetteer match
    base_coords = None
    for key, coords in PH_CITIES_COORDINATES.items():
        if key in clean_city or key in clean_brgy or key in address_line.lower():
            base_coords = coords
            break

    # If no city matched, default to Pasig / Metro Manila center
    if not base_coords:
        base_coords = (14.5764, 121.0851)

    # Apply deterministic pseudorandom jitter (within ~400 meters) based on address string hash
    # so addresses in the same barangay do not pile on top of each other!
    addr_hash = int(hashlib.md5((address_line + barangay).encode("utf-8")).hexdigest()[:6], 16)
    jitter_lat = ((addr_hash % 1000) - 500) * 0.000008  # ~ ±400 meters
    jitter_lng = (((addr_hash // 1000) % 1000) - 500) * 0.000008

    final_lat = round(base_coords[0] + jitter_lat, 6)
    final_lng = round(base_coords[1] + jitter_lng, 6)

    return {
        "latitude": final_lat,
        "longitude": final_lng,
        "source": "heuristic",
        "status_label": f"Auto-Geocoded ({city or 'Metro Manila'})",
    }


def normalize_phone(phone_str):
    if not phone_str:
        return ""
    cleaned = re.sub(r"[^\d+]", "", str(phone_str))
    if cleaned.startswith("+63"):
        cleaned = "0" + cleaned[3:]
    elif cleaned.startswith("63"):
        cleaned = "0" + cleaned[2:]
    return cleaned


def parse_import_file(uploaded_file, client_id):
    """
    Parses CSV or Excel (.xlsx) file and validates each row against database constraints.
    Returns preview data with validation errors, geocoded pins, and statistical summary.
    """
    filename = uploaded_file.name.lower()
    raw_rows = []

    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        import openpyxl
        wb = openpyxl.load_workbook(uploaded_file, data_only=True)
        sheet = wb.active
        headers = None
        for row in sheet.iter_rows(values_only=True):
            if not row or all(v is None for v in row):
                continue
            if headers is None:
                headers = [normalize_column_name(h) for h in row]
            else:
                row_dict = {}
                for idx, val in enumerate(row):
                    if idx < len(headers) and headers[idx]:
                        row_dict[headers[idx]] = str(val).strip() if val is not None else ""
                raw_rows.append(row_dict)
    else:
        # Default to CSV / Text
        content = uploaded_file.read()
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
        reader = csv.reader(io.StringIO(text))
        headers = None
        for row in reader:
            if not row or all(not cell.strip() for cell in row):
                continue
            if headers is None:
                headers = [normalize_column_name(h) for h in row]
            else:
                row_dict = {}
                for idx, cell in enumerate(row):
                    if idx < len(headers) and headers[idx]:
                        row_dict[headers[idx]] = cell.strip()
                raw_rows.append(row_dict)

    # Process and validate rows
    existing_refs = set(
        Account.objects.filter(client_id=client_id).values_list("reference", flat=True)
    )

    parsed_rows = []
    seen_refs_in_file = set()
    total_balance = Decimal("0.00")
    valid_count = 0
    error_count = 0
    geocoded_count = 0

    for idx, row in enumerate(raw_rows, start=1):
        customer_name = row.get("customer_name", "").strip()
        account_no = row.get("account_no", "").strip()
        balance_str = row.get("balance", "") or row.get("principal", "") or "0"
        phone = normalize_phone(row.get("phone", ""))
        address = row.get("address", "").strip()
        barangay = row.get("barangay", "").strip()
        city = row.get("city", "").strip()
        raw_lat = row.get("latitude")
        raw_lng = row.get("longitude")
        due_date_str = row.get("due_date", "").strip()
        notes = row.get("notes", "").strip()

        errors = []
        warnings = []

        # Validate Name
        if not customer_name:
            errors.append("Customer name is required.")

        # Validate Account No / Reference
        if not account_no:
            errors.append("Account number / reference is required.")
        elif account_no in existing_refs:
            errors.append(f"Account '{account_no}' already exists in database.")
        elif account_no in seen_refs_in_file:
            errors.append(f"Duplicate account '{account_no}' inside the uploaded file.")
        else:
            seen_refs_in_file.add(account_no)

        # Validate Balance
        clean_bal = re.sub(r"[^\d.]", "", balance_str) or "0.00"
        try:
            balance_dec = Decimal(clean_bal).quantize(Decimal("0.01"))
            if balance_dec <= 0:
                warnings.append("Balance is 0.00.")
        except InvalidOperation:
            balance_dec = Decimal("0.00")
            errors.append("Invalid balance amount format.")

        # Validate Address
        full_address = ", ".join(filter(bool, [address, barangay, city]))
        if not full_address:
            full_address = "No address specified, Metro Manila"
            warnings.append("No address specified; using default location.")

        # Geocode
        geo = geocode_address(address, barangay, city, raw_lat, raw_lng)
        if geo["source"] != "supplied":
            geocoded_count += 1

        # Validate Phone
        if not phone:
            warnings.append("No contact phone number provided.")

        status = "error" if errors else ("warning" if warnings else "valid")
        if status in ("valid", "warning"):
            valid_count += 1
            total_balance += balance_dec
        else:
            error_count += 1

        parsed_rows.append({
            "row_num": idx,
            "customer_name": customer_name,
            "account_no": account_no,
            "balance": str(balance_dec),
            "phone": phone,
            "address": full_address,
            "barangay": barangay,
            "city": city,
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "geo_source": geo["source"],
            "geo_label": geo["status_label"],
            "due_date": due_date_str,
            "notes": notes,
            "status": status,
            "errors": errors,
            "warnings": warnings,
        })

    return {
        "rows": parsed_rows,
        "stats": {
            "total_rows": len(parsed_rows),
            "valid_count": valid_count,
            "error_count": error_count,
            "geocoded_count": geocoded_count,
            "total_balance": format(total_balance, ".2f"),
        },
    }


def generate_sample_csv():
    """Generates standard CSV template with realistic Philippine accounts."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Customer Name",
        "Account No",
        "Principal",
        "Balance",
        "Phone",
        "Address",
        "Barangay",
        "City",
        "Latitude",
        "Longitude",
        "Due Date",
        "Notes",
    ])
    # 5 sample realistic debtor rows
    samples = [
        ["Juan Dela Cruz", "ACC-10001", "25000.00", "28500.00", "09171234567", "123 Caruncho Ave", "San Nicolas", "Pasig", "14.5585", "121.0820", "2026-11-15", "60 days delinquent"],
        ["Maria Santos", "ACC-10002", "15000.00", "16800.00", "09289876543", "45 Commonwealth Ave", "Batasan Hills", "Quezon City", "", "", "2026-11-20", "Auto-geocoded sample"],
        ["Pedro Reyes", "ACC-10003", "50000.00", "54200.00", "09395551234", "Block 5 Lot 12 Acacia St", "Pinagbuhatan", "Pasig", "", "", "2026-11-25", "Promised partial payment"],
        ["Elena Bautista", "ACC-10004", "32000.00", "35400.00", "09187778899", "78 General Luna St", "Tuktukan", "Taguig", "14.5260", "121.0720", "2026-11-28", "With direct GPS coordinate"],
        ["Ricardo Dalisay", "ACC-10005", "18500.00", "20100.00", "09081112233", "204 Boni Ave", "Plainview", "Mandaluyong", "", "", "2026-12-05", "Commercial loan default"],
    ]
    for row in samples:
        writer.writerow(row)
    return output.getvalue()


@transaction.atomic
def execute_import_batch(client_id, commission_rule_id, rows, admin_user):
    """
    Executes database insertion for all valid rows in an atomic transaction.
    """
    client = Client.objects.select_for_update().get(pk=client_id)
    rule = CommissionRule.objects.select_for_update().get(pk=commission_rule_id)

    now = timezone.now()
    created_accounts = []

    grant_trial_import_access_to_collectors(client)

    for item in rows:
        if item.get("status") == "error":
            continue

        ref = item["account_no"]
        name = item["customer_name"]
        bal = Decimal(item["balance"])
        phone = item.get("phone", "")
        addr_line = item["address"]
        lat = float(item["latitude"])
        lng = float(item["longitude"])
        pos = Point(lng, lat, srid=4326)

        # 1. Create or retrieve Customer
        customer = Customer.objects.create(
            client=client,
            display_name=name,
            customer_type=Customer.CustomerType.PERSON,
            source_system="IMPORT",
            external_id=ref,
        )

        # 2. Create Verified Customer Address
        cust_addr = CustomerAddress.objects.create(
            customer=customer,
            kind=CustomerAddress.Kind.RESIDENTIAL,
            address_line=addr_line[:300],
            position=pos,
            verification_status=CustomerAddress.VerificationStatus.VERIFIED,
            added_by=admin_user,
            verified_by=admin_user,
            verified_at=now,
        )

        # 3. Create Customer Phone Contact if available
        if phone:
            CustomerContact.objects.create(
                customer=customer,
                kind="phone",
                value=phone[:500],
                normalized_value=phone[:500],
                is_active=True,
                validated_by=admin_user,
                validated_at=now,
            )

        # 4. Create Account
        account = Account.objects.create(
            client=client,
            customer=customer,
            source_system="IMPORT",
            external_id=ref,
            reference=ref,
            balance_snapshot=bal,
            balance_as_of=now,
            currency=rule.currency or "PHP",
            status=Account.Status.OPEN,
            priority=3,
            collection_address=cust_addr,
            commission_rule=rule,
        )

        created_accounts.append(account)

        # 5. Audit Logging
        append_audit_event(
            actor=admin_user,
            action="account_created",
            entity_type="Account",
            entity_id=account.pk,
            account=account,
            changes={
                "status": account.status,
                "source_system": account.source_system,
                "reference": ref,
            },
        )

    return created_accounts
