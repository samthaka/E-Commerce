"""
store/management/commands/import_products.py

Imports products.csv into the store app:
  1. builds the Category tree (master > sub > article type)
  2. references product images already stored under MEDIA_ROOT
  3. bulk-creates Products in batches

Re-runnable: rows whose source_id already exists are skipped.

If the CSV is missing, the command interactively offers to download and extract
the public products archive from Google Drive.

Usage:
  python manage.py import_products products/products.csv --dry-run
  python manage.py import_products products/products.csv
  python manage.py import_products products/products.csv --limit 500
  python manage.py import_products products/products.csv --wipe
"""
import csv
from html.parser import HTMLParser
import os
import shutil
import stat
import sys
import tempfile
import time
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from store.models import Category, Product

REQUIRED_COLUMNS = {
    "id", "name", "price", "digital", "image", "description",
    "master_category", "sub_category", "article_type", "gender",
    "base_colour", "season", "year", "usage",
    "shipping_weight", "shipping_length", "shipping_width", "shipping_height",
    "is_synthetic",
}
PRODUCTS_FILE_ID = "1OGwCSCrk877KSqZCbyST9YSd2jrOfYYq"
PRODUCTS_ARCHIVE_SIZE_BYTES = 2_600_000_000


class _DriveConfirmationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current_form = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.current_form = {"action": attributes.get("action", ""), "params": {}}
            self.forms.append(self.current_form)
        elif tag == "input" and self.current_form is not None:
            name = attributes.get("name")
            if name:
                self.current_form["params"][name] = attributes.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current_form = None


def to_bool(value):
    return str(value).strip().lower() in ("true", "1", "yes")


def to_decimal(value):
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def to_int(value):
    value = str(value).strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


class Command(BaseCommand):
    help = "Import products (with category tree and images) from products.csv"

    def _confirm(self, message):
        if not sys.stdin.isatty():
            raise CommandError(
                f"{message} Run this command in an interactive terminal to continue."
            )
        self.stdout.write(f"{message} [y/N] ", ending="")
        self.stdout.flush()
        return input().strip().lower() in ("y", "yes")

    def _drive_confirmation(self, html, response_url):
        parser = _DriveConfirmationParser()
        parser.feed(html)
        for form in parser.forms:
            params = form["params"]
            if params.get("id") != PRODUCTS_FILE_ID or not params.get("uuid"):
                continue
            download_url = urljoin(response_url, form["action"])
            parsed_url = urlparse(download_url)
            if parsed_url.scheme != "https" or parsed_url.hostname not in {
                "drive.google.com",
                "drive.usercontent.google.com",
            }:
                break
            return download_url, params
        raise CommandError(
            "Google Drive returned an HTML page instead of the products archive. "
            "Check that the link is publicly accessible and try again."
        )

    def _write_archive_response(self, response, downloaded_file):
        response.raise_for_status()
        if "text/html" in response.headers.get("Content-Type", "").lower():
            return self._drive_confirmation(response.text, response.url)

        try:
            total_bytes = int(response.headers.get("Content-Length", 0))
        except ValueError:
            total_bytes = 0
        if total_bytes <= 0:
            total_bytes = PRODUCTS_ARCHIVE_SIZE_BYTES

        downloaded_bytes = 0
        last_reported_percent = 0
        last_reported_at = 0
        is_interactive = self.stdout.isatty()

        def report_progress(force=False):
            nonlocal last_reported_percent, last_reported_at
            percent = min(100, downloaded_bytes * 100 // total_bytes)
            now = time.monotonic()
            if not force:
                if is_interactive and now - last_reported_at < 0.1:
                    return
                if not is_interactive and percent // 5 <= last_reported_percent // 5:
                    return
            elif (
                not is_interactive
                and last_reported_at
                and percent == last_reported_percent
            ):
                return

            bar_width = 30
            filled = min(bar_width, percent * bar_width // 100)
            progress_bar = f"[{'=' * filled}{' ' * (bar_width - filled)}]"
            downloaded_gb = downloaded_bytes / 1_000_000_000
            total_gb = total_bytes / 1_000_000_000
            message = (
                f"Downloading products archive: {progress_bar} {percent:3d}% "
                f"({downloaded_gb:.2f}/{total_gb:.2f} GB)"
            )
            if is_interactive:
                self.stdout.write(f"\r{message}", ending="")
                if force:
                    self.stdout.write("")
            else:
                self.stdout.write(message)
            self.stdout.flush()
            last_reported_percent = percent
            last_reported_at = now

        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                downloaded_file.write(chunk)
                downloaded_bytes += len(chunk)
                report_progress()
        report_progress(force=True)
        return None

    def _download_products_archive(self, archive_path):
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{archive_path.name}.", suffix=".download", dir=archive_path.parent,
        )
        try:
            with os.fdopen(file_descriptor, "wb") as downloaded_file:
                try:
                    with requests.get(
                        "https://drive.google.com/uc",
                        params={
                            "export": "download",
                            "id": PRODUCTS_FILE_ID,
                        },
                        stream=True,
                        timeout=(15, 120),
                    ) as response:
                        confirmation = self._write_archive_response(response, downloaded_file)

                    if confirmation:
                        download_url, params = confirmation
                        with requests.get(
                            download_url,
                            params=params,
                            stream=True,
                            timeout=(15, 120),
                        ) as response:
                            if self._write_archive_response(response, downloaded_file):
                                raise CommandError(
                                    "Google Drive still returned a confirmation page instead "
                                    "of the products archive. Try again later."
                                )
                except requests.RequestException as exc:
                    raise CommandError(f"Could not download the products archive: {exc}") from exc

            if not zipfile.is_zipfile(temporary_path):
                raise CommandError(
                    "Google Drive did not return a valid ZIP file. Check that the link is "
                    "publicly accessible and try again."
                )
            os.replace(temporary_path, archive_path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def _extract_products_archive(self, archive_path, csv_path):
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=csv_path.parent) as temp_dir:
            extraction_root = Path(temp_dir) / "contents"
            extraction_root.mkdir()
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    for member in archive.infolist():
                        member_path = PurePosixPath(member.filename)
                        mode = member.external_attr >> 16
                        file_type = stat.S_IFMT(mode)
                        if (
                            member_path.is_absolute()
                            or ".." in member_path.parts
                            or "\\" in member.filename
                            or (member_path.parts and member_path.parts[0].endswith(":"))
                            or stat.S_ISLNK(mode)
                            or file_type not in (0, stat.S_IFREG, stat.S_IFDIR)
                        ):
                            raise CommandError(
                                f"Unsafe path in products archive: {member.filename}"
                            )
                    archive.extractall(extraction_root)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise CommandError(f"Could not extract products archive: {exc}") from exc

            csv_files = [
                path for path in extraction_root.rglob(csv_path.name) if path.is_file()
            ]
            if len(csv_files) != 1:
                raise CommandError(
                    f"Expected one {csv_path.name} in the products archive; found {len(csv_files)}."
                )

            source_root = csv_files[0].parent
            for source in source_root.rglob("*"):
                relative_path = source.relative_to(source_root)
                destination = csv_path.parent / relative_path
                if source.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)

    def _ensure_products_csv(self, csv_path):
        if csv_path.is_file():
            return

        archive_path = Path.cwd() / "products.zip"
        if archive_path.is_file():
            archive_size = archive_path.stat().st_size / 1_000_000_000
            prompt = (
                f"{csv_path} is missing. Extract the existing archive {archive_path} "
                f"({archive_size:.1f} GB)?"
            )
        else:
            try:
                available_bytes = shutil.disk_usage(archive_path.parent).free
            except OSError as exc:
                available_bytes = None
                self.stderr.write(self.style.WARNING(
                    f"Could not check available storage at {archive_path.parent}: {exc}"
                ))

            if (
                available_bytes is not None
                and available_bytes < PRODUCTS_ARCHIVE_SIZE_BYTES
            ):
                raise CommandError(
                    f"Downloading the products archive requires about 2.6 GB, but only "
                    f"{available_bytes / 1_000_000_000:.1f} GB is available at "
                    f"{archive_path.parent}."
                )
            available_note = (
                f" Available storage at {archive_path.parent}: "
                f"{available_bytes / 1_000_000_000:.1f} GB."
                if available_bytes is not None
                else " Available storage could not be checked."
            )
            prompt = (
                f"{csv_path} is missing. Download the products archive from Google Drive "
                f"(about 2.6 GB) and extract it to {csv_path.parent}?{available_note} "
                "Extraction requires additional space."
            )
        if not self._confirm(prompt):
            raise CommandError(f"CSV not found: {csv_path}")

        if not archive_path.is_file():
            self.stdout.write("Downloading products archive...")
            self._download_products_archive(archive_path)
        self.stdout.write("Extracting products archive...")
        self._extract_products_archive(archive_path, csv_path)
        if not csv_path.is_file():
            raise CommandError(f"Products archive did not provide the requested CSV: {csv_path}")

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Path to products.csv")
        parser.add_argument(
            "--images-dir",
            help="Directory containing {id}.jpg files under MEDIA_ROOT (default: <csv folder>/images)",
        )
        parser.add_argument("--batch-size", type=int, default=1000)
        parser.add_argument("--limit", type=int, help="Only import the first N rows")
        parser.add_argument("--dry-run", action="store_true",
                            help="Parse and validate only; write nothing, copy nothing")
        parser.add_argument("--wipe", action="store_true",
                            help="Delete all previously imported products (source_id set) first")

    # ------------------------------------------------------------------ helpers
    def build_category_tree(self, rows):
        """Create/get categories; return {(master, sub, article): Category(leaf)}."""
        leaves = {}
        cache = {}  # (parent_id, name) -> Category

        def get_node(name, parent):
            key = (parent.pk if parent else None, name)
            if key not in cache:
                slug_path = slugify(f"{parent.slug}-{name}" if parent else name)
                cache[key], _ = Category.objects.get_or_create(
                    parent=parent, name=name, defaults={"slug": slug_path},
                )
            return cache[key]

        combos = sorted({(r["master_category"], r["sub_category"], r["article_type"]) for r in rows})
        for master, sub, article in combos:
            m = get_node(master, None)
            s = get_node(sub, m)
            a = get_node(article, s)
            leaves[(master, sub, article)] = a
        return leaves

    # ------------------------------------------------------------------ main
    def handle(self, *args, **opts):
        csv_path = Path(opts["csv_path"]).expanduser()
        self._ensure_products_csv(csv_path)
        images_dir = Path(opts["images_dir"]).expanduser() if opts["images_dir"] else csv_path.parent / "images"
        if not images_dir.is_dir():
            raise CommandError(f"Images directory not found: {images_dir}")
        if not settings.MEDIA_ROOT:
            raise CommandError("settings.MEDIA_ROOT is not set")
        media_root = Path(settings.MEDIA_ROOT).resolve()
        try:
            image_path_prefix = images_dir.resolve().relative_to(media_root)
        except ValueError as exc:
            raise CommandError(
                f"Images directory must be inside MEDIA_ROOT ({media_root}): {images_dir}"
            ) from exc
        dry = opts["dry_run"]

        # ---- read + validate CSV
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing:
                raise CommandError(f"CSV is missing columns: {sorted(missing)}")
            rows = list(reader)
        if opts["limit"]:
            rows = rows[: opts["limit"]]
        self.stdout.write(f"Read {len(rows)} rows from {csv_path.name}")

        bad, bad_price, bad_name, no_image = [], [], [], 0
        for r in rows:
            price = to_decimal(r["price"])
            if price is None or price <= 0:
                bad_price.append(r["id"])
            if not r["name"].strip():
                bad_name.append(r["id"])
            if not (images_dir / f"{r['id']}.jpg").is_file():
                no_image += 1
        if bad_price or bad_name:
            raise CommandError(
                f"Invalid rows: {len(bad_price)} with bad price (e.g. {bad_price[:5]}), "
                f"{len(bad_name)} with blank name (e.g. {bad_name[:5]})"
            )
        if no_image:
            self.stdout.write(self.style.WARNING(f"{no_image} rows have no image file (will import without image)"))

        if dry:
            cats = {(r['master_category'], r['sub_category'], r['article_type']) for r in rows}
            self.stdout.write(self.style.SUCCESS(
                f"Dry run OK: {len(rows)} products, {len(cats)} leaf categories. Nothing written."))
            return

        # ---- optional wipe
        if opts["wipe"]:
            deleted, _ = Product.objects.filter(source_id__isnull=False).delete()
            self.stdout.write(f"Wiped {deleted} previously imported rows")

        # ---- categories
        with transaction.atomic():
            leaves = self.build_category_tree(rows)
        self.stdout.write(f"Category tree ready ({Category.objects.count()} categories)")

        # ---- skip already-imported
        existing_products = {
            product.source_id: product
            for product in Product.objects.filter(source_id__isnull=False)
        }
        todo = [r for r in rows if int(r["id"]) not in existing_products]
        self.stdout.write(f"{len(existing_products)} already imported, {len(todo)} to import")

        # Refresh category and image references for existing imports.
        existing_updates = []
        update_fields = set()
        category_updates = 0
        image_updates = 0
        for r in rows:
            product = existing_products.get(int(r["id"]))
            if not product:
                continue

            changed = False
            category = leaves[(r["master_category"], r["sub_category"], r["article_type"])]
            if product.category_id != category.pk:
                product.category = category
                category_updates += 1
                update_fields.add("category")
                changed = True

            image_file = images_dir / f"{r['id']}.jpg"
            if image_file.is_file():
                image_name = (image_path_prefix / image_file.name).as_posix()
                if product.image.name != image_name:
                    product.image = image_name
                    image_updates += 1
                    update_fields.add("image")
                    changed = True

            if changed:
                existing_updates.append(product)

        if existing_updates:
            with transaction.atomic():
                Product.objects.bulk_update(existing_updates, sorted(update_fields))
            if category_updates:
                self.stdout.write(f"Updated {category_updates} existing product categories")
            if image_updates:
                self.stdout.write(f"Updated {image_updates} existing image references")

        # ---- bulk create products
        bs = opts["batch_size"]
        created = 0
        for start in range(0, len(todo), bs):
            batch = []
            for r in todo[start:start + bs]:
                image_file = images_dir / f"{r['id']}.jpg"
                has_img = image_file.is_file()
                image_name = (
                    (image_path_prefix / image_file.name).as_posix() if has_img else None
                )
                batch.append(Product(
                    source_id=int(r["id"]),
                    name=r["name"].strip()[:200],
                    price=to_decimal(r["price"]),
                    digital=to_bool(r["digital"]),
                    image=image_name,
                    description=r["description"],
                    category=leaves[(r["master_category"], r["sub_category"], r["article_type"])],
                    gender=r["gender"].strip(),
                    base_colour=r["base_colour"].strip(),
                    season=r["season"].strip(),
                    year=to_int(r["year"]),
                    usage=r["usage"].strip(),
                    shipping_weight=to_decimal(r["shipping_weight"]),
                    shipping_length=to_decimal(r["shipping_length"]),
                    shipping_width=to_decimal(r["shipping_width"]),
                    shipping_height=to_decimal(r["shipping_height"]),
                    is_synthetic=to_bool(r["is_synthetic"]),
                ))
            with transaction.atomic():
                Product.objects.bulk_create(batch)
            created += len(batch)
            self.stdout.write(f"  products created: {created}/{len(todo)}")

        self.stdout.write(self.style.SUCCESS(
            f"Done. {created} products created, {Product.objects.count()} total, "
            f"{Category.objects.count()} categories."))