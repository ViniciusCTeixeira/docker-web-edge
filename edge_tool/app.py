"""Operações transacionais compartilhadas pela CLI e pelo assistente."""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import re
import tempfile

from .certificates import certificate_paths, check_pair
from .core import EdgeError, Store, atomic_write, hostname, identifier, parse_json_rows, port, upstream
from .docker import Docker, available_ports, compose_model, network_aliases, public_rows
from .nginx import render_compose, render_main, render_site


class Edge:
    def __init__(self, root='/srv/edge', docker=None):
        self.store = Store(root)
        self.docker = docker or Docker()

    def _config(self, **values):
        config = {
            'version': 1, 'network': 'web_edge', 'http_port': 80, 'https_port': 443,
            'certificates_dir': '/etc/edge/certificates',
            'image': 'nginx:1.27.5-alpine', 'compose_name': 'edge',
        }
        config.update({key: value for key, value in values.items() if value is not None})
        for key in ['network', 'compose_name']:
            identifier(config[key])
        for key in ['http_port', 'https_port']:
            config[key] = port(config[key])
        if config['http_port'] == config['https_port']:
            raise EdgeError('HTTP e HTTPS precisam de portas diferentes.')
        if not isinstance(config['image'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:@-]*', config['image']):
            raise EdgeError('Referência de imagem Docker inválida.')
        config['certificates_dir'] = str(Path(config['certificates_dir']).expanduser().resolve())
        return config

    def _write_gateway(self, config):
        root = self.store.root
        (root / 'sites').mkdir(exist_ok=True, mode=0o700)
        (root / 'projects').mkdir(exist_ok=True, mode=0o700)
        (root / 'gateway').mkdir(exist_ok=True, mode=0o700)
        Path(config['certificates_dir']).mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(root / 'gateway' / 'nginx.conf', render_main(), 0o644)
        atomic_write(root / 'compose.yaml', render_compose(root, config))

    def init(self, **values):
        self.docker.prerequisites()
        with self.store.lock():
            if (self.store.root / 'config.json').exists():
                current = self.store.config()
                requested = self._config(**{**current, **{key: value for key, value in values.items() if value is not None}})
                if current != requested:
                    raise EdgeError('Edge já inicializado com outros valores. Use edge configure.')
                return current
            if any((self.store.root / path).exists() for path in ['compose.yaml', 'nginx.conf', 'gateway/nginx.conf']) or any((self.store.root / 'sites').glob('*.conf')) or any((self.store.root / 'projects').glob('*.json')):
                raise EdgeError('Diretório contém um gateway ou cadastro não gerenciado. Escolha outro --root.')
            config = self._config(**values)
            self._write_gateway(config)
            self.store.write(self.store.root / 'config.json', config)
            return config

    def gateway_project(self, config=None):
        config = config or self.store.config()
        return {'directory': str(self.store.root), 'compose_files': [str(self.store.root / 'compose.yaml')],
                'compose_name': config['compose_name'], 'env_file': None}

    def gateway_status(self):
        rows = parse_json_rows(self.docker.compose(self.gateway_project(), 'ps', '--all', '--format', 'json', sensitive=True))
        rows = [row for row in rows if row.get('Service') == 'nginx']
        running = bool(rows) and all(row.get('State') == 'running' and row.get('Health') in ['', None, 'healthy'] for row in rows)
        return {'running': running, 'active': any(row.get('State') in ['running', 'restarting', 'paused'] for row in rows), 'containers': public_rows(rows)}

    def _nginx(self, *args):
        return self.docker.compose(self.gateway_project(), 'exec', '-T', 'nginx', 'nginx',
                                   '-c', '/etc/nginx/edge/nginx.conf', *args, sensitive=True)

    def _test_reload(self):
        self._nginx('-t')
        self._nginx('-s', 'reload')

    @contextmanager
    def _transaction(self, paths, *, reload=False, recreate=False):
        previous = {Path(path): (Path(path).read_bytes() if Path(path).exists() else None) for path in paths}
        backups = self.store.root / 'backups'
        backups.mkdir(exist_ok=True, mode=0o700)
        for path, data in previous.items():
            if data is not None:
                # Prefix the parent so certificate.pem from different profiles do not collide.
                atomic_write(backups / f'{path.parent.name}-{path.name}.previous', data)
        try:
            yield
        except BaseException as error:
            for path, data in previous.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, data)
            try:
                if recreate:
                    self.docker.compose(self.gateway_project(), 'up', '-d', '--wait', 'nginx', sensitive=True)
                elif (reload() if callable(reload) else reload):
                    self._test_reload()
            except EdgeError:
                raise EdgeError('Falha na aplicação; arquivos restaurados, mas a recuperação do gateway falhou. Execute edge gateway status/logs.') from error
            raise

    def configure(self, **values):
        with self.store.lock():
            current = self.store.config()
            config = self._config(**{**current, **{key: value for key, value in values.items() if value is not None}})
            if config == current:
                return config
            if config['certificates_dir'] != current['certificates_dir']:
                raise EdgeError('Diretório de certificados é definido em init e não pode ser alterado por configure.')
            if config['compose_name'] != current['compose_name']:
                raise EdgeError('O nome Compose do gateway é definido em init.')
            if config['network'] != current['network'] and self.store.projects():
                raise EdgeError('Remova os cadastros antes de alterar a rede compartilhada.')
            status = self.gateway_status()
            available_ports(config, self.docker.containers(), current['compose_name'])
            paths = [self.store.root / 'config.json', self.store.root / 'compose.yaml',
                     self.store.root / 'gateway' / 'nginx.conf']
            paths += [self.store.root / 'sites' / f'{project["id"]}.conf' for project in self.store.projects()]
            with self._transaction(paths, recreate=status['active']):
                self._write_gateway(config)
                for project in self.store.projects():
                    site = self.store.root / 'sites' / f'{project["id"]}.conf'
                    if site.exists():
                        atomic_write(site, render_site(project, config))
                self.store.write(self.store.root / 'config.json', config)
                if status['active']:
                    self._ensure_network(config['network'])
                    self._candidate_test()
                    self.docker.compose(self.gateway_project(), 'up', '-d', '--wait', 'nginx', sensitive=True)
            return config

    def _ensure_network(self, network):
        try:
            self.docker.run(['docker', 'network', 'inspect', network], sensitive=True)
        except EdgeError:
            self.docker.run(['docker', 'network', 'create', network], sensitive=True)

    def _candidate_test(self):
        self.docker.compose(self.gateway_project(), 'run', '--rm', '--no-deps', 'nginx',
                            'nginx', '-c', '/etc/nginx/edge/nginx.conf', '-t', sensitive=True)

    def gateway_operation(self, operation, *, follow=False):
        with self.store.lock():
            config = self.store.config()
            project = self.gateway_project(config)
            if operation in ['up', 'restart']:
                available_ports(config, self.docker.containers(), config['compose_name'])
                self._ensure_network(config['network'])
                self._candidate_test()
                self.docker.compose(project, 'up', '-d', '--wait', 'nginx', sensitive=True)
                if operation == 'restart':
                    self.docker.compose(project, 'restart', '--no-deps', 'nginx', sensitive=True)
                    self.docker.compose(project, 'up', '-d', '--wait', 'nginx', sensitive=True)
                return self.gateway_status()
            if operation == 'stop':
                self.docker.compose(project, 'stop', 'nginx', sensitive=True)
                return {'stopped': True}
            if operation == 'logs':
                # Log following must not hold the mutation lock.
                pass
            else:
                raise EdgeError('Operação de gateway inválida.')
        self.docker.compose(project, 'logs', '--tail', '100', *(['--follow'] if follow else []), 'nginx', stream=True)
        return None

    def _normalize_project(self, project):
        result = copy.deepcopy(project)
        if result.get('version', 1) != 1:
            raise EdgeError('Versão de cadastro não suportada.')
        result['version'] = 1
        identifier(result.get('id'))
        directory = Path(result.get('directory', '')).expanduser().resolve()
        if not directory.is_dir():
            raise EdgeError('Diretório do projeto não encontrado.')
        result['directory'] = str(directory)
        if not result.get('compose_files') or not isinstance(result['compose_files'], list):
            raise EdgeError('Informe pelo menos um arquivo Compose.')
        files = []
        for filename in result['compose_files']:
            path = (directory / filename).resolve()
            if not path.is_file():
                raise EdgeError(f'Arquivo Compose não encontrado: {path.name}.')
            files.append(str(path))
        result['compose_files'] = files
        if result.get('env_file'):
            env_file = (directory / result['env_file']).resolve()
            if not env_file.is_file():
                raise EdgeError('Arquivo de ambiente não encontrado.')
            result['env_file'] = str(env_file)
        else:
            result['env_file'] = None
        if not isinstance(result.get('services'), list) or not result['services']:
            raise EdgeError('Informe os serviços a gerenciar.')
        if any(not isinstance(service, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', service) for service in result['services']):
            raise EdgeError('Nome de serviço inválido.')
        result['services'] = list(dict.fromkeys(result['services']))
        if not isinstance(result.get('routes'), list) or not result['routes']:
            raise EdgeError('Informe pelo menos uma rota HTTPS.')
        domains = set()
        for route in result['routes']:
            route['domain'] = hostname(route.get('domain'))
            route['upstream'] = upstream(route.get('upstream'))
            identifier(route.get('certificate'))
            if route['domain'] in domains:
                raise EdgeError('Domínio duplicado no projeto.')
            domains.add(route['domain'])
        return result

    def _project_checks(self, project):
        config = self.store.config()
        model = compose_model(self.docker, project, config)
        if model['name'] == config['compose_name']:
            raise EdgeError('Aplicação não pode usar o nome Compose do gateway.')
        aliases = network_aliases(model, config)
        hosts = {route['upstream'].rsplit(':', 1)[0] for route in project['routes']}
        for other in self.store.projects():
            if other['id'] == project['id']:
                continue
            if {route['domain'] for route in other['routes']} & {route['domain'] for route in project['routes']}:
                raise EdgeError(f'Domínio já cadastrado no projeto {other["id"]}.')
            other_model = json.loads(self.docker.compose(other, 'config', '--format', 'json', sensitive=True))
            if other_model['name'] == model['name']:
                raise EdgeError(f'Identidade Compose já cadastrada no projeto {other["id"]}.')
            other_aliases = network_aliases(other_model, config)
            other_hosts = {route['upstream'].rsplit(':', 1)[0] for route in other['routes']}
            if hosts.intersection(other_aliases) or other_hosts.intersection(aliases):
                raise EdgeError(f'Alias de upstream ambíguo com o projeto {other["id"]}. Use aliases exclusivos.')
        for container in self.docker.containers():
            if not container.get('state', {}).get('Running'):
                continue
            labels = container.get('labels') or {}
            for host in hosts:
                attached = container.get('networks', {}).get(config['network'], {})
                container_aliases = set(attached.get('Aliases') or []) | set(attached.get('DNSNames') or [])
                if host in container_aliases and (labels.get('com.docker.compose.project') != model['name']
                                                  or labels.get('com.docker.compose.service') not in aliases[host]):
                    raise EdgeError(f'Alias {host} já utilizado por outro container na rede do Edge.')
        for profile in {route['certificate'] for route in project['routes']}:
            domains = [route['domain'] for route in project['routes'] if route['certificate'] == profile]
            check_pair(*certificate_paths(config, profile), domains)
        return model

    def project_save(self, project, *, edit=False):
        with self.store.lock():
            project = self._normalize_project(project)
            path = self.store.project_path(project['id'])
            if path.exists() != edit:
                raise EdgeError('Projeto já cadastrado; use edit.' if path.exists() else 'Projeto não cadastrado; use add.')
            self._project_checks(project)
            site = self.store.root / 'sites' / f'{project["id"]}.conf'
            active = site.exists()
            if active and not self.gateway_status()['running']:
                raise EdgeError('Inicie o gateway antes de editar uma rota já publicada.')
            with self._transaction([path, site], reload=active):
                if active:
                    atomic_write(site, render_site(project, self.store.config()))
                    self._test_reload()
                self.store.write(path, project)
            return project

    def project_remove(self, project_id):
        with self.store.lock():
            self.store.project(project_id)
            site = self.store.root / 'sites' / f'{project_id}.conf'
            path = self.store.project_path(project_id)
            active = site.exists()
            if active and not self.gateway_status()['running']:
                raise EdgeError('Inicie o gateway antes de remover uma rota publicada.')
            with self._transaction([path, site], reload=active):
                site.unlink(missing_ok=True)
                if active:
                    self._test_reload()
                path.unlink()
            return {'removed': project_id, 'containers_preserved': True}

    def route_apply(self, project_id):
        with self.store.lock():
            project = self._normalize_project(self.store.project(project_id))
            self._project_checks(project)
            if not self.gateway_status()['running']:
                raise EdgeError('Gateway não está disponível. Execute edge gateway up.')
            site = self.store.root / 'sites' / f'{project_id}.conf'
            tested = False
            with self._transaction([site], reload=lambda: tested):
                atomic_write(site, render_site(project, self.store.config()))
                self._nginx('-t')
                tested = True
                self._nginx('-s', 'reload')
            return {'applied': project_id}

    def project_operation(self, project_id, operation, *, follow=False):
        if operation == 'logs':
            project = self._normalize_project(self.store.project(project_id))
            self.docker.compose(project, 'logs', '--tail', '100', *(['--follow'] if follow else []),
                                *project['services'], stream=True)
            return None
        with self.store.lock():
            project = self._normalize_project(self.store.project(project_id))
            if operation == 'up':
                self._project_checks(project)
                self.docker.run(['docker', 'network', 'inspect', self.store.config()['network']], sensitive=True)
                self.docker.compose(project, 'up', '-d', '--no-build', '--no-deps', '--wait',
                                    *project['services'], sensitive=True)
            elif operation in ['stop', 'restart']:
                self.docker.compose(project, operation, *(['--no-deps'] if operation == 'restart' else []),
                                    *project['services'], sensitive=True)
            else:
                raise EdgeError('Operação de projeto inválida.')
            return {'project': project_id, 'operation': operation}

    def cert_import(self, profile, certificate, key):
        identifier(profile)
        with self.store.lock():
            config = self.store.config()
            domains = [route['domain'] for project in self.store.projects() for route in project['routes']
                       if route['certificate'] == profile]
            # Validate precisely the bytes that will be installed, even if the
            # caller's files are being renewed by another process.
            cert_data, key_data = Path(certificate).read_bytes(), Path(key).read_bytes()
            with tempfile.TemporaryDirectory(prefix='.cert-import-', dir=self.store.root) as temporary:
                snapshot_cert, snapshot_key = Path(temporary) / 'cert.pem', Path(temporary) / 'key.pem'
                atomic_write(snapshot_cert, cert_data)
                atomic_write(snapshot_key, key_data)
                result = check_pair(snapshot_cert, snapshot_key, domains)
            cert_path, key_path = certificate_paths(config, profile)
            active = any((self.store.root / 'sites' / f'{project["id"]}.conf').exists()
                         for project in self.store.projects() if any(route['certificate'] == profile for route in project['routes']))
            if active and not self.gateway_status()['running']:
                raise EdgeError('Inicie o gateway antes de substituir um certificado em uso.')
            with self._transaction([cert_path, key_path], reload=active):
                cert_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_write(cert_path, cert_data)
                atomic_write(key_path, key_data)
                if active:
                    self._test_reload()
            return {'profile': profile, **result}

    def cert_check(self, profile):
        config = self.store.config()
        domains = [route['domain'] for project in self.store.projects() for route in project['routes']
                   if route['certificate'] == profile]
        return {'profile': profile, **check_pair(*certificate_paths(config, profile), domains)}

    def cert_list(self):
        directory = Path(self.store.config()['certificates_dir'])
        profiles = []
        for path in sorted(directory.iterdir()):
            if path.is_dir() and not path.name.startswith('.'):
                try:
                    profiles.append(self.cert_check(path.name))
                except EdgeError as error:
                    profiles.append({'profile': path.name, 'valid': False, 'error': str(error)})
        return profiles

    def cert_remove(self, profile):
        identifier(profile)
        with self.store.lock():
            if any(route['certificate'] == profile for project in self.store.projects() for route in project['routes']):
                raise EdgeError('Certificado em uso por um projeto; remova ou altere suas rotas primeiro.')
            cert, key = certificate_paths(self.store.config(), profile)
            if not cert.parent.is_dir():
                raise EdgeError('Perfil de certificado não encontrado.')
            cert.unlink(missing_ok=True)
            key.unlink(missing_ok=True)
            cert.parent.rmdir()
            return {'removed': profile}

    def validate(self, project_id=None, *, configuration_only=False, require_gateway=False, directory=None):
        with self.store.lock():
            config = self.store.config()
            projects = [self.store.project(project_id)] if project_id else self.store.projects()
            if directory and (len(projects) != 1 or Path(directory).resolve() != Path(projects[0]['directory'])):
                raise EdgeError('Cadastro pertence a outro diretório; confira o identificador de projeto.')
            results = []
            for original in projects:
                result = {'id': original['id'], 'configuration_valid': False, 'running': None, 'upstreams_accessible': None}
                try:
                    project = self._normalize_project(original)
                    self._project_checks(project)
                    result['configuration_valid'] = True
                    if not configuration_only:
                        rows = parse_json_rows(self.docker.compose(project, 'ps', '--all', '--format', 'json', sensitive=True))
                        rows = [row for row in rows if row.get('Service') in project['services']]
                        result['containers'] = public_rows(rows)
                        result['running'] = (set(project['services']) == {row.get('Service') for row in rows}
                                             and all(row.get('State') == 'running' and row.get('Health') in [None, '', 'healthy'] for row in rows))
                        self.docker.run(['docker', 'network', 'inspect', config['network']], sensitive=True)
                        probes = []
                        for route in project['routes']:
                            try:
                                self.docker.compose(self.gateway_project(), 'exec', '-T', 'nginx', 'wget',
                                                    '-q', '-T', '5', '-O', '/dev/null', '--header',
                                                    f'Host: {route["domain"]}', f'http://{route["upstream"]}/', sensitive=True)
                                probes.append(True)
                            except EdgeError:
                                probes.append(False)
                        result['upstreams_accessible'] = all(probes)
                except EdgeError as error:
                    result['error'] = str(error)
                results.append(result)
            gateway = None
            if require_gateway or not configuration_only:
                try:
                    gateway = self.gateway_status()
                    if gateway['running']:
                        self._nginx('-t')
                    self.docker.run(['docker', 'network', 'inspect', config['network']], sensitive=True)
                except EdgeError as error:
                    gateway = {'running': False, 'error': str(error)}
            valid = all(item['configuration_valid'] for item in results)
            ok = valid and all(not item.get('error') and (configuration_only or (item['running'] and item['upstreams_accessible'])) for item in results)
            if gateway is not None:
                ok = ok and gateway['running']
            return {'ok': bool(ok), 'configuration_valid': valid, 'gateway': gateway, 'projects': results}

    def list_projects(self):
        config = self.store.config()
        containers = self.docker.containers()
        discovered = {}
        for container in containers:
            labels = container.get('labels') or {}
            name = labels.get('com.docker.compose.project')
            if not name or name == config['compose_name'] or labels.get('com.docker.compose.oneoff', '').lower() == 'true':
                continue
            discovered.setdefault(name, []).append({
                'name': container['name'].lstrip('/'), 'service': labels.get('com.docker.compose.service'),
                'status': container['state'].get('Status'),
                'health': container['state'].get('Health', {}).get('Status'),
            })
        registered = []
        for project in self.store.projects():
            try:
                model = json.loads(self.docker.compose(project, 'config', '--format', 'json', sensitive=True))
                rows = discovered.pop(model['name'], [])
                registered.append({'id': project['id'], 'registered': True, 'compose_project': model['name'],
                                   'domains': [route['domain'] for route in project['routes']],
                                   'route_applied': (self.store.root / 'sites' / f'{project["id"]}.conf').exists(), 'containers': rows})
            except (EdgeError, KeyError, json.JSONDecodeError):
                registered.append({'id': project['id'], 'registered': True, 'configuration_valid': False, 'containers': []})
        return registered + [{'compose_project': name, 'registered': False, 'containers': rows} for name, rows in sorted(discovered.items())]
