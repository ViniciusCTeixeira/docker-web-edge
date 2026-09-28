"""Validação de certificados PEM sem depender de uma cadeia pública."""
from datetime import datetime, timezone
from pathlib import Path
import ssl

from .core import EdgeError, identifier, run


def certificate_paths(config, profile):
    directory = Path(config['certificates_dir']) / identifier(profile)
    return directory / 'certificate.pem', directory / 'private.key'


def check_pair(certificate, key, domains=()):
    certificate, key = Path(certificate), Path(key)
    if not certificate.is_file() or not key.is_file():
        raise EdgeError('Certificado e chave precisam ser arquivos locais legíveis.')
    cert_public = run(['openssl', 'x509', '-in', certificate, '-pubkey', '-noout'], sensitive=True)
    key_public = run(['openssl', 'pkey', '-in', key, '-passin', 'pass:', '-pubout'], sensitive=True)
    if cert_public.strip() != key_public.strip():
        raise EdgeError('A chave privada não corresponde ao certificado.')
    dates = run(['openssl', 'x509', '-in', certificate, '-noout', '-startdate', '-enddate'], sensitive=True)
    parsed = dict(line.split('=', 1) for line in dates.splitlines())
    now = datetime.now(timezone.utc).timestamp()
    if not ssl.cert_time_to_seconds(parsed['notBefore']) <= now < ssl.cert_time_to_seconds(parsed['notAfter']):
        raise EdgeError('Certificado expirado ou ainda não válido.')
    for domain in domains:
        try:
            run(['openssl', 'x509', '-in', certificate, '-noout', '-checkhost', domain], sensitive=True)
        except EdgeError:
            raise EdgeError(f'O certificado não cobre o domínio {domain}.') from None
    # SSLContext also checks that the complete PEM chain and key are loadable.
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
    except (ssl.SSLError, OSError):
        raise EdgeError('Cadeia PEM ou chave privada inválida.') from None
    return {'valid': True, 'not_before': parsed['notBefore'], 'not_after': parsed['notAfter']}
