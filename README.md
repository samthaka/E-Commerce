# E-Commerce

A Django storefront with product search, cart and checkout, order history, reviews, and Stripe and Shippo integrations.

## Vendor inventory

Vendors are created by an administrator from **Vendors** in Django admin. Enter the
vendor's name, shop name, email, and address; no Django user needs to be selected or
created. The app emails a seven-day setup link to the vendor, who can create a password
or continue with Google using that email address. Accepting the invitation verifies
their email without another confirmation email. If an invitation expires or email
delivery fails, administrators can resend it from the vendor list. Vendors can manage
their inventory at <http://127.0.0.1:8000/inventory/> and, in the storefront, see only
their own products. Vendor accounts do not have customer cart or checkout access.

Every product must belong to a vendor. Administrators can add products from the
Products page in Django admin, but must select a vendor; administrator accounts
cannot own products. Vendors can manage only their own products.

Imported products receive a stable `MNY-{id}` SKU and an initial stock quantity of
zero, in addition to the storefront's existing product, category, image, and shipping
fields.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `SECRET_KEY` and any credentials for the services you want to use in `.env`, then initialize the database and start the app:

```bash
python manage.py migrate
python manage.py runserver
```

Open <http://127.0.0.1:8000/>. Create an admin account with
`python manage.py createsuperuser` before importing products or creating vendors.

## Import products

Product data and images are stored under `products/` (`MEDIA_ROOT`). The importer
expects `products/products.csv` and an `images/` directory containing `{id}.jpg`
files. Preview the import before writing to the database. A real import requires a
superuser account and automatically creates five verified demo vendor accounts,
then distributes imported products among them. The command prints each new
vendor's username and randomly generated password once; save these credentials
for testing. Re-running the import reuses the same vendor accounts and does not
show their passwords again.

```bash
python manage.py import_products products/products.csv --dry-run
python manage.py import_products products/products.csv
```

If you have not created an administrator yet, run
`python manage.py createsuperuser` first and then return to the import command.

If the CSV is missing, the command interactively offers to download and extract the products archive (about 2.6 GB). Ensure you have enough disk space. You can also use:

```bash
python manage.py import_products products/products.csv --limit 500
python manage.py import_products products/products.csv --wipe
python manage.py import_products --remove_products --dry-run
python manage.py import_products --remove_products
```

`--wipe` deletes previously imported products before importing; `--remove_products` deletes imported products without reimporting. Neither option removes image files.

## Tests

```bash
pytest
```
