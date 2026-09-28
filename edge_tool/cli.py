"""Interface de terminal e assistente interativo."""
import argparse
import json
import os
from pathlib import Path
import sys

from .app import Edge
from .core import EdgeError


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise EdgeError(f'Argumentos inválidos: {message}. Consulte edge --help.')


def common(parser, *, top=False):
    default = None if top else argparse.SUPPRESS
    parser.add_argument('--root', default=os.environ.get('EDGE_ROOT', '/srv/edge') if top else argparse.SUPPRESS,
                        help='Diretório operacional (padrão: /srv/edge ou EDGE_ROOT)')
    parser.add_argument('--json', action='store_true', default=False if top else argparse.SUPPRESS, help='Saída JSON')
    parser.add_argument('--non-interactive', action='store_true', default=False if top else argparse.SUPPRESS,
                        help='Não solicitar entrada; confirmar a operação informada')


def settings(parser):
    parser.add_argument('--network', help='Rede Docker externa compartilhada')
    parser.add_argument('--http-port', type=int, help='Porta HTTP no host')
    parser.add_argument('--https-port', type=int, help='Porta HTTPS no host')
    parser.add_argument('--certificates-dir', help='Diretório protegido de certificados (init)')
    parser.add_argument('--image', help='Imagem Nginx')
    parser.add_argument('--compose-name', help='Nome Compose exclusivo do gateway (init)')


def parser():
    root = Parser(description='Gateway Docker compartilhado para produção.', prog='edge')
    common(root, top=True)
    commands = root.add_subparsers(dest='command')
    for action in ['init', 'configure']:
        child = commands.add_parser(action, help='Inicializar' if action == 'init' else 'Configurar gateway')
        common(child)
        settings(child)
    child = commands.add_parser('wizard', help='Assistente para cadastrar/editar projetos')
    common(child)
    child.add_argument('project_id', nargs='?', help='Identificador existente para edição')
    child = commands.add_parser('list', help='Projetos cadastrados e containers Compose descobertos')
    common(child)
    child = commands.add_parser('validate', help='Validar configuração e estado real')
    common(child)
    child.add_argument('project_id', nargs='?')
    child.add_argument('--configuration-only', action='store_true', help='Não exigir aplicação em execução')
    child.add_argument('--require-gateway', action='store_true', help='Exigir gateway disponível mesmo na validação estática')
    child.add_argument('--directory', help='Exigir que o cadastro pertença a este checkout')
    for operation in ['up', 'stop', 'restart', 'logs']:
        child = commands.add_parser(operation, help=f'{operation} dos serviços cadastrados')
        common(child)
        child.add_argument('project_id')
        if operation == 'logs':
            child.add_argument('--follow', action='store_true')
    child = commands.add_parser('project', help='Administrar cadastro de projetos')
    common(child)
    operations = child.add_subparsers(dest='operation', required=True)
    for operation in ['add', 'edit', 'show', 'remove']:
        leaf = operations.add_parser(operation)
        common(leaf)
        leaf.add_argument('project_id')
        if operation in ['add', 'edit']:
            leaf.add_argument('--directory', required=operation == 'add')
            leaf.add_argument('--compose', action='append', help='Arquivo Compose; repetir para overrides, em ordem')
            leaf.add_argument('--env-file', help='Arquivo de ambiente do Compose; vazio remove o valor na edição')
            leaf.add_argument('--service', action='append', required=operation == 'add', help='Serviço gerenciado; repetir')
            leaf.add_argument('--route', nargs=3, action='append', required=operation == 'add',
                              metavar=('DOMINIO', 'UPSTREAM', 'CERTIFICADO'), help='Rota HTTPS; repetir para múltiplos domínios')
    child = commands.add_parser('route', help='Publicar rotas cadastradas')
    common(child)
    operations = child.add_subparsers(dest='operation', required=True)
    leaf = operations.add_parser('apply')
    common(leaf)
    leaf.add_argument('project_id')
    child = commands.add_parser('cert', help='Importar e validar certificados PEM')
    common(child)
    operations = child.add_subparsers(dest='operation', required=True)
    for operation in ['import', 'list', 'check', 'remove']:
        leaf = operations.add_parser(operation)
        common(leaf)
        if operation != 'list':
            leaf.add_argument('profile')
        if operation == 'import':
            leaf.add_argument('--certificate', required=True)
            leaf.add_argument('--key', required=True)
    child = commands.add_parser('gateway', help='Operar somente o gateway')
    common(child)
    operations = child.add_subparsers(dest='operation', required=True)
    for operation in ['up', 'stop', 'restart', 'status', 'logs']:
        leaf = operations.add_parser(operation)
        common(leaf)
        if operation == 'logs':
            leaf.add_argument('--follow', action='store_true')
    return root


def confirm(message, arguments):
    if arguments.non_interactive:
        return
    if not sys.stdin.isatty():
        raise EdgeError('Operação exige confirmação. Em scripts, informe --non-interactive.')
    if input(message + ' [s/N]: ').strip().lower() not in ['s', 'sim']:
        raise EdgeError('Operação cancelada.')


def prompt(message, default=''):
    answer = input(f'{message}' + (f' [{default}]' if default else '') + ': ').strip()
    return answer or default


def wizard(edge, project_id=None):
    previous = edge.store.project(project_id) if project_id else None
    project_id = project_id or prompt('Identificador do projeto')
    directory = prompt('Diretório do projeto', previous['directory'] if previous else '')
    files = prompt('Arquivos Compose separados por vírgula',
                   ','.join(previous['compose_files']) if previous else 'compose.yaml').split(',')
    environment = prompt('Arquivo de ambiente (opcional)', (previous.get('env_file') or '') if previous else '')
    services = prompt('Serviços separados por vírgula', ','.join(previous['services']) if previous else '').split(',')
    routes = []
    while True:
        old = previous['routes'][len(routes)] if previous and len(previous['routes']) > len(routes) else {}
        routes.append({'domain': prompt('Domínio', old.get('domain', '')),
                       'upstream': prompt('Upstream (alias:porta)', old.get('upstream', '')),
                       'certificate': prompt('Perfil de certificado importado', old.get('certificate', ''))})
        if prompt('Adicionar outro domínio? (s/n)', 'n').lower() not in ['s', 'sim']:
            break
    project = {'version': 1, 'id': project_id, 'directory': directory,
               'compose_files': [value.strip() for value in files], 'env_file': environment or None,
               'services': [value.strip() for value in services], 'routes': routes}
    print(json.dumps(project, ensure_ascii=False, indent=2))
    if prompt('Salvar cadastro? (s/n)', 'n').lower() not in ['s', 'sim']:
        raise EdgeError('Cadastro cancelado.')
    return edge.project_save(project, edit=previous is not None)


def dispatch(arguments, edge):
    command = arguments.command
    if command in ['init', 'configure']:
        values = {key: getattr(arguments, key) for key in ['network', 'http_port', 'https_port', 'certificates_dir', 'image', 'compose_name']}
        if command == 'configure':
            if arguments.http_port is not None or arguments.https_port is not None:
                print('Alterar portas poderá recriar o gateway e interromper conexões brevemente.', file=sys.stderr)
                confirm('Aplicar configuração?', arguments)
            return edge.configure(**values)
        return edge.init(**values)
    if command == 'wizard':
        if arguments.non_interactive or arguments.json:
            raise EdgeError('O assistente exige interação; use project add/edit em scripts.')
        return wizard(edge, arguments.project_id)
    if command == 'list':
        return edge.list_projects()
    if command == 'validate':
        return edge.validate(arguments.project_id, configuration_only=arguments.configuration_only,
                             require_gateway=arguments.require_gateway, directory=arguments.directory)
    if command in ['up', 'stop', 'restart', 'logs']:
        if command == 'logs' and arguments.json:
            raise EdgeError('Logs são transmitidos em texto; remova --json.')
        return edge.project_operation(arguments.project_id, command, follow=getattr(arguments, 'follow', False))
    if command == 'route':
        return edge.route_apply(arguments.project_id)
    if command == 'gateway':
        if arguments.operation == 'status':
            return edge.gateway_status()
        if arguments.operation == 'logs' and arguments.json:
            raise EdgeError('Logs são transmitidos em texto; remova --json.')
        return edge.gateway_operation(arguments.operation, follow=getattr(arguments, 'follow', False))
    if command == 'cert':
        if arguments.operation == 'list':
            return edge.cert_list()
        if arguments.operation == 'import':
            return edge.cert_import(arguments.profile, arguments.certificate, arguments.key)
        if arguments.operation == 'check':
            return edge.cert_check(arguments.profile)
        confirm('Remover certificado?', arguments)
        return edge.cert_remove(arguments.profile)
    if command == 'project':
        if arguments.operation == 'show':
            return edge.store.project(arguments.project_id)
        if arguments.operation == 'remove':
            confirm('Remover cadastro e rotas? Os containers serão preservados.', arguments)
            return edge.project_remove(arguments.project_id)
        editing = arguments.operation == 'edit'
        project = edge.store.project(arguments.project_id) if editing else {
            'version': 1, 'id': arguments.project_id, 'compose_files': ['compose.yaml'], 'env_file': None,
        }
        for source, target in [('directory', 'directory'), ('compose', 'compose_files'), ('env_file', 'env_file'), ('service', 'services')]:
            value = getattr(arguments, source)
            if value is not None:
                project[target] = value
        if arguments.route:
            project['routes'] = [dict(zip(['domain', 'upstream', 'certificate'], route)) for route in arguments.route]
        return edge.project_save(project, edit=editing)
    raise EdgeError('Comando inválido.')


def main(argv=None):
    arguments = None
    try:
        cli = parser()
        arguments = cli.parse_args(argv)
        if not arguments.command:
            cli.print_help()
            return 0
        edge = Edge(arguments.root)
        result = dispatch(arguments, edge)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=None if arguments.json else 2))
        if arguments.command == 'validate' and not result['ok']:
            return 1
        if arguments.command == 'gateway' and arguments.operation == 'status' and not result['running']:
            return 1
        return 0
    except (EdgeError, OSError, ValueError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, EdgeError) else 'Não foi possível ler ou executar a configuração. Confira arquivos, formato e permissões.'
        if (arguments is not None and arguments.json) or '--json' in (argv or sys.argv[1:]):
            print(json.dumps({'ok': False, 'error': message}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f'Erro: {message}', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('Operação interrompida.', file=sys.stderr)
        return 130
