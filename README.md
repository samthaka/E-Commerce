# E-Commerce

A Django-based online storefront with a shopping cart, checkout flow, product catalog, order history, shipping quotes, and Stripe/Shippo integrations.

This project is organized as a single Django app named `store` under the `ecommerce` project package. It includes user authentication with `django-allauth`, Google OAuth support, persistent cart and order records, and shipping/return label workflows.

## Features

- Product catalog with category filtering and search
- Shopping cart and order item management
- Checkout flow with saved shipping addresses
- Stripe payment intents for secure checkout
- Shippo integration for live shipping rate quotes, purchase labels, and tracking updates
- Order history and delivery status management
- Return shipment quotes and label generation
- Product review and rating feature
- User account/profile pages
- Admin-ready Django structure

## Tech Stack

- Python
- Django 5.1
- SQLite (default local database)
- django-allauth
- Stripe
- Shippo
- Pillow for product images
- pytest + pytest-django for tests

## Repository Structure

```text
E-Commerce/
├── ecommerce/
│   ├── __init__.py
│   ├── settings.py
│   ├── urls.py
│   ├── views.py
│   ├── wsgi.py
│   └── asgi.py
├── store/
│   ├── migrations/
│   ├── templates/
│   ├── __init__.py
│   ├── admin.py
│   ├── apps.py
│   ├── forms.py
│   ├── models.py
│   ├── shippo.py
│   ├── tests.py
│   ├── urls.py
│   └── views.py
├── static/
│   ├── css/
│   ├── images/
│   └── js/
├── templates/
│   └── admin/base_site.html
├── .env.example
├── .gitignore
├── db.sqlite3
├── manage.py
├── pytest.ini
├── requirements.txt
└── README.md
```

## Main App Behavior

The app uses the `store` Django app to handle storefront logic:

- `store.views.store` renders the catalog and category pages.
- `store.views.cart` and `store.views.checkout` manage cart and checkout views.
- `store.views.updateItem` updates cart quantities via AJAX-style JSON requests.
- `store.views.create_payment_intent` and `store.views.processOrder` power Stripe checkout.
- `store.views.shipping_rates` fetches Shippo shipping rates for delivery.
- `store.views.order_history` and `store.views.submit_review` handle account orders and product reviews.
- `store.views.create_return_rates`, `purchase_return_label`, and `refresh_tracking` cover return labels and shipment monitoring.

## Local Setup

1. Clone the repository.
2. Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Configure environment variables:

```bash
cp .env.example .env
```

Then update `.env` with the values required for your local environment.

## Required Environment Variables

The project reads values from environment variables, including:

- `SECRET_KEY`
- `DEBUG`
- `ALLOWED_HOSTS`
- `GMAIL_ADDRESS`
- `GMAIL_APP_PASSWORD`
- `SHIPPO_API_TOKEN`
- `SHIPPO_TEST_MODE`
- `STRIPE_SECRET_KEY`
- `STRIPE_PUBLISHABLE_KEY`
- Google OAuth values such as `GOOGLE_OAUTH2_CLIENT_ID` and `GOOGLE_OAUTH2_CLIENT_SECRET`

A sample configuration is available in `.env.example`.

## Database and Migrations

Initialize the database schema with:

```bash
python manage.py migrate
```

Create an admin user if needed:

```bash
python manage.py createsuperuser
```

## Running the App

Start the development server:

```bash
python manage.py runserver
```

Then open:

- http://127.0.0.1:8000/
- Django admin: http://127.0.0.1:8000/admin/

## Testing

The repository includes Django tests covering Shippo/transaction helpers and core store logic. Run tests with:

```bash
pytest
```

or, for Django integration settings:

```bash
python manage.py test
```

## Notes

- The default database is SQLite, so this repo is suitable for local development and demos.
- For production, configure secure environment variables and consider switching to a production-grade database and deployment setup.
- Google OAuth, email delivery, Stripe, and Shippo all depend on valid service credentials being configured in the environment.

## Credits

This project is a Django e-commerce application inspired by classic online storefront patterns, with shipping and checkout features suitable for a demo or small commerce deployment.
