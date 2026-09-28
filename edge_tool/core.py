"""Persistência, validação de entradas e execução segura de comandos."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


class EdgeError(Exception):
    """Falha operacional cuja mensagem pode ser apresentada ao usuário."""


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,62}', value):
        raise EdgeError('Identificador inválido: use letras minúsculas, números, _ e -.')
    return value


def hostname(value):
    if not isinstance(value, str):
        raise EdgeError('Domínio inválido.')
    value = value.lower()
    labels = value.split('.')
    if len(value) > 253 or len(labels) < 2 or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
        for label in labels
    ):
        raise EdgeError('Domínio inválido; informe o nome completo sem protocolo ou porta.')
    return value


def port(value):
    if isinstance(value, bool):
        raise EdgeError('Porta inválida: esperado inteiro entre 1 e 65535.')
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise EdgeError('Porta inválida: esperado inteiro entre 1 e 65535.') from None
    if str(number) != str(value) or not 1 <= number <= 65535:
        raise EdgeError('Porta inválida: esperado inteiro entre 1 e 65535.')
    return number


def upstream(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*:[0-9]+', value):
        raise EdgeError('Upstream inválido; use alias-docker:porta.')
    host, target_port = value.rsplit(':', 1)
    if len(host) > 253:
        raise EdgeError('Alias de upstream muito longo.')
    return f'{host}:{port(target_port)}'


def parse_json_rows(value):
    if not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in value.splitlines() if line.strip()]
    return parsed if isinstance(parsed, list) else [parsed]


def run(args, *, sensitive=False, stream=False, input_data=None, timeout=120):
    try:
        result = subprocess.run(
            [str(arg) for arg in args], input=input_data, capture_output=not stream,
            timeout=None if stream else timeout,
        )
    except FileNotFoundError:
        raise EdgeError(f'Executável não encontrado: {args[0]}.') from None
    except subprocess.TimeoutExpired:
        raise EdgeError(f'Tempo limite excedido ao executar {args[0]}.') from None
    if result.returncode:
        # Compose/OpenSSL can include interpolated credentials or key data in stderr.
        detail = '' if sensitive or stream else (result.stderr or b'').decode(errors='replace').strip()[:1200]
        raise EdgeError(f'{args[0]} falhou (código {result.returncode}).' + (f' {detail}' if detail else ''))
    return '' if stream else result.stdout.decode(errors='replace')


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data if isinstance(data, bytes) else data.encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()

    @contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / '.edge.lock').open('a') as handle:
            os.chmod(handle.name, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def read(self, path):
        try:
            result = json.loads(Path(path).read_text())
        except FileNotFoundError:
            raise EdgeError(f'Cadastro não encontrado: {Path(path).name}. Execute edge init/project add.') from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise EdgeError(f'JSON inválido: {Path(path).name}.') from None
        if not isinstance(result, dict) or result.get('version') != 1:
            raise EdgeError(f'Formato de configuração não suportado: {Path(path).name}.')
        return result

    def write(self, path, value):
        atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')

    def config(self):
        return self.read(self.root / 'config.json')

    def project_path(self, project_id):
        return self.root / 'projects' / f'{identifier(project_id)}.json'

    def project(self, project_id):
        return self.read(self.project_path(project_id))

    def projects(self):
        return [self.read(path) for path in sorted((self.root / 'projects').glob('*.json'))]
