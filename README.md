# E-Commerce

A Django storefront with product search, cart and checkout, order history, reviews, and Stripe and Shippo integrations.

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

Open <http://127.0.0.1:8000/>. Create an admin account with `python manage.py createsuperuser`.

## Import products

Product data and images are stored under `products/` (`MEDIA_ROOT`). The importer expects `products/products.csv` and an `images/` directory containing `{id}.jpg` files. Preview the import before writing to the database:

```bash
python manage.py import_products products/products.csv --dry-run
python manage.py import_products products/products.csv
```

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
