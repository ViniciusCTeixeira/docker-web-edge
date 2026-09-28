"""Templates genéricos do gateway e das rotas."""
import json


def render_site(project, config):
    result = []
    public_port = config['https_port']
    suffix = '' if public_port == 443 else f':{public_port}'
    for route in project['routes']:
        domain = route['domain']
        profile = route['certificate']
        target = route['upstream']
        result.append(f'''server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    return 301 https://{domain}{suffix}$request_uri;
}}
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain};
    ssl_certificate /etc/nginx/certificates/{profile}/certificate.pem;
    ssl_certificate_key /etc/nginx/certificates/{profile}/private.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    location / {{
        set $upstream {target};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Port {public_port};
        proxy_set_header X-Forwarded-Proto https;
        proxy_pass http://$upstream;
    }}
}}
''')
    return '\n'.join(result)


def render_main():
    return '''user nginx;
worker_processes auto;
events { worker_connections 1024; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    log_format main '$remote_addr - $host [$time_local] "$request" $status $body_bytes_sent';
    access_log /var/log/nginx/access.log main;
    error_log /var/log/nginx/error.log warn;
    sendfile on;
    keepalive_timeout 65;
    server_tokens off;
    client_max_body_size 32m;
    resolver 127.0.0.11 valid=10s ipv6=off;
    server {
        listen 80 default_server;
        listen [::]:80 default_server;
        server_name _;
        return 444;
    }
    include /etc/nginx/sites.d/*.conf;
}
'''


def render_compose(root, config):
    # JSON is a YAML subset accepted by Compose; no YAML dependency is needed.
    return json.dumps({
        'name': config['compose_name'],
        'services': {'nginx': {
            'image': config['image'],
            'command': ['nginx', '-c', '/etc/nginx/edge/nginx.conf', '-g', 'daemon off;'],
            'ports': [f'{config["http_port"]}:80', f'{config["https_port"]}:443'],
            'networks': ['edge'],
            'volumes': [
                {'type': 'bind', 'source': str(root / 'gateway'), 'target': '/etc/nginx/edge', 'read_only': True},
                {'type': 'bind', 'source': str(root / 'sites'), 'target': '/etc/nginx/sites.d', 'read_only': True},
                {'type': 'bind', 'source': config['certificates_dir'], 'target': '/etc/nginx/certificates', 'read_only': True},
            ],
            'healthcheck': {
                'test': ['CMD', 'nginx', '-t', '-c', '/etc/nginx/edge/nginx.conf'],
                'interval': '10s', 'timeout': '5s', 'retries': 3,
            },
            'restart': 'unless-stopped',
        }},
        'networks': {'edge': {'external': True, 'name': config['network']}},
    }, indent=2) + '\n'
