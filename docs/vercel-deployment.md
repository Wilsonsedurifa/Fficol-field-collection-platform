# Deploying FiCol on Vercel

FiCol can run on Vercel's Django runtime, but it must use external services for its stateful production data:

- a managed PostgreSQL database with the `postgis` extension enabled;
- a private S3-compatible bucket for evidence uploads (Cloudflare R2 or Amazon S3 are suitable).

## 1. Import the repository

In Vercel, choose **Add New → Project**, import `Wilsonsedurifa/Fficol-field-collection-platform`, and use the repository root as the project root. Select the **Container** framework preset (or allow `Dockerfile.vercel` to select it). FiCol uses GeoDjango, so the container installs the required GEOS and GDAL system libraries; the standard Django/Python preset does not include them. Do not add legacy `/api` rewrites.

## 2. Add production environment variables

Set these for **Production** (and set separate Preview values if previews need database access):

| Variable | Value |
| --- | --- |
| `DJANGO_DEBUG` | `False` |
| `DJANGO_SECRET_KEY` | A new, long random value |
| `DATABASE_URL` | PostgreSQL/PostGIS connection string, including SSL requirements from the provider |
| `SITE_URL` | `https://your-project.vercel.app` or the final custom domain |
| `DJANGO_ALLOWED_HOSTS` | `.vercel.app,your-custom-domain.com` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://your-project.vercel.app,https://your-custom-domain.com` |
| `PRIVATE_STORAGE_BUCKET` | Private S3/R2 bucket name |
| `PRIVATE_STORAGE_ACCESS_KEY` | Bucket access key |
| `PRIVATE_STORAGE_SECRET_KEY` | Bucket secret key |
| `PRIVATE_STORAGE_ENDPOINT` | S3/R2 endpoint URL |
| `PRIVATE_STORAGE_REGION` | Bucket region (`auto` for R2) |

Add the existing email variables only when invitation email is required.

Vercel can build the public landing page before `DATABASE_URL` is attached, but FiCol's login and all operational workflows require a real PostGIS `DATABASE_URL`. Add it before sharing the deployment URL.

## 3. Provision database and storage

Create a PostgreSQL database whose provider supports PostGIS, then run `CREATE EXTENSION IF NOT EXISTS postgis;` using the provider SQL console. Keep the evidence bucket private; do not configure a public bucket domain.

## 4. Run migrations once against production

Do not run migrations automatically in every preview deployment. From a trusted terminal with the production environment variables available, run:

```bash
python manage.py migrate
python manage.py createsuperuser
```

## 5. Deploy and verify

Deploy `main`, then verify `/`, `/login/`, collector/back-office login, a map query, and an authorized evidence upload/download. Run `python manage.py check --deploy` with production variables before sharing the URL.

## Important

Vercel's filesystem is not persistent. FiCol's document workflow only works in production after the private storage variables are configured. Never commit `.env` or database/storage credentials.
