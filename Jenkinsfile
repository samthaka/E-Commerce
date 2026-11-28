pipeline {
    agent {
                    node {
                        label 'built-in' // or your agent/node label
                        customWorkspace '/home/wizard/ecommerce'
                    }
                }

    options {
        skipDefaultCheckout(true)
        disableConcurrentBuilds()
    }

    parameters {
        string(
            name: 'DEPLOY_BRANCH',
            defaultValue: 'master',
            description: 'Branch that is allowed to deploy to production'
        )
    }

    environment {
        DEPLOY_DIR = '/home/wizard/ecommerce'
        DEPLOY_PYTHON = '/home/wizard/.pyenv/versions/3.10.13/envs/e-commerce/bin/python'
        ENV_CONFIG = 'test'
        SECRET_KEY = 'jenkins-test-only-key'
        ALLOWED_HOSTS = 'localhost,127.0.0.1,testserver'
        PYENV_VERSION = 'e-commerce'
        PYENV_ROOT = '/home/wizard/.pyenv'
        DJANGO_ENV_FILE = '/dev/null'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Test') {
            steps {
                sh '''
                    export PATH="$PYENV_ROOT/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
                    eval "$(pyenv init -)"
                    pyenv exec python -m pytest
                '''
            }
        }

        stage('Deploy') {
            when {
                beforeAgent true
                expression {
                    def branch = env.BRANCH_NAME ?: env.GIT_BRANCH?.replaceFirst('^origin/', '')
                    branch == params.DEPLOY_BRANCH
                }
            }
            agent {
                node {
                    customWorkspace '/home/wizard/ecommerce'
                }
            }
            steps {
                checkout scm
                sh '''
                    set -eu
                    test -r "$DEPLOY_DIR/.env" || {
                        echo "Create $DEPLOY_DIR/.env with production settings before deploying."
                        exit 1
                    }
                    chmod 600 "$DEPLOY_DIR/.env"

                    mkdir -p "$DEPLOY_DIR/products" "$DEPLOY_DIR/staticfiles"

                    run_python() {
                        env -i \
                            HOME=/home/wizard \
                            PATH=/usr/bin:/bin \
                            ENV_CONFIG=prod \
                            DJANGO_ENV_FILE="$DEPLOY_DIR/.env" \
                            "$DEPLOY_PYTHON" "$@"
                    }

                    run_manage() {
                        run_python "$DEPLOY_DIR/manage.py" "$@"
                    }

                    run_python -m pip install --requirement "$DEPLOY_DIR/requirements.txt"
                    run_manage check --deploy
                    run_manage migrate --noinput
                    run_manage collectstatic --noinput

                    sudo -n /usr/bin/install -m 0644 \
                        "$DEPLOY_DIR/deploy/ecommerce.service" \
                        /etc/systemd/system/ecommerce.service
                    sudo -n /usr/bin/systemctl daemon-reload
                    sudo -n /usr/bin/systemctl enable ecommerce

                    CERT_PATH=/etc/letsencrypt/live/ecommerce.samthaka.com/fullchain.pem
                    NGINX_SITE=/etc/nginx/sites-available/ecommerce
                    NGINX_LINK=/etc/nginx/sites-enabled/ecommerce
                    NGINX_BIN="$(command -v nginx)"
                    CERTBOT_BIN="$(command -v certbot)"

                    if [ ! -s "$CERT_PATH" ]; then
                        CERTBOT_EMAIL="$(run_python -c 'from dotenv import dotenv_values; print((dotenv_values("/home/wizard/ecommerce/.env").get("GMAIL_ADDRESS") or "").strip())')"
                        : "${CERTBOT_EMAIL:?Set GMAIL_ADDRESS in .env to a valid certificate contact email}"

                        sudo -n /usr/bin/install -d -m 0755 \
                            /var/www/letsencrypt/.well-known/acme-challenge
                        sudo -n /usr/bin/install -m 0644 \
                            "$DEPLOY_DIR/deploy/nginx-ecommerce-bootstrap.conf" \
                            "$NGINX_SITE"
                        sudo -n /usr/bin/ln -sfn "$NGINX_SITE" "$NGINX_LINK"
                        sudo -n "$NGINX_BIN" -t
                        sudo -n /usr/bin/systemctl enable --now nginx
                        sudo -n "$CERTBOT_BIN" certonly \
                            --webroot \
                            --webroot-path /var/www/letsencrypt \
                            --non-interactive \
                            --agree-tos \
                            --email "$CERTBOT_EMAIL" \
                            --domain ecommerce.samthaka.com
                    fi

                    sudo -n /usr/bin/install -m 0644 \
                        "$DEPLOY_DIR/deploy/nginx-ecommerce.conf" \
                        "$NGINX_SITE"
                    sudo -n /usr/bin/install -D -m 0755 \
                        "$DEPLOY_DIR/deploy/reload-nginx-after-certbot.sh" \
                        /etc/letsencrypt/renewal-hooks/deploy/ecommerce-nginx-reload
                    sudo -n /usr/bin/ln -sfn "$NGINX_SITE" "$NGINX_LINK"
                    sudo -n /usr/bin/setfacl -m u:www-data:--x /home/wizard
                    sudo -n /usr/bin/setfacl -m u:www-data:--x "$DEPLOY_DIR"
                    sudo -n /usr/bin/setfacl -R -m u:www-data:r-X \
                        "$DEPLOY_DIR/staticfiles" "$DEPLOY_DIR/products"
                    sudo -n /usr/bin/setfacl -d -m u:www-data:r-X \
                        "$DEPLOY_DIR/staticfiles" "$DEPLOY_DIR/products"
                    sudo -n "$NGINX_BIN" -t
                    sudo -n /usr/bin/systemctl reload nginx
                    sudo -n /usr/bin/systemctl enable --now certbot.timer
                    sudo -n /usr/bin/systemctl restart ecommerce
                    sudo -n /usr/bin/systemctl is-active --quiet ecommerce
                '''
            }
        }
    }
}
