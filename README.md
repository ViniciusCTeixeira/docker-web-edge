# Docker Web Edge

Ferramenta de terminal para administrar um gateway Nginx compartilhado entre projetos Docker em um servidor Linux de produção. Não depende de framework, aplicação específica ou pacotes Python externos.

## Instalação

Pré-requisitos: Python 3.11+, Docker Engine com Compose v2 ou posterior, OpenSSL e permissão para usar o Docker. As operações também precisam de permissão sobre o diretório operacional e os certificados.

```sh
git clone git@github.com:ViniciusCTeixeira/docker-web-edge.git
cd docker-web-edge
sudo ./install.sh
edge --help
```

O instalador copia o pacote para `/usr/local/lib/docker-web-edge` e cria `/usr/local/bin/edge`. Pode ser repetido para atualizar a instalação. Ele não inicia containers nem cria o estado operacional. Alternativamente, `./install.sh --prefix "$HOME/.local"` instala para o usuário; inclua `$HOME/.local/bin` no PATH e escolha diretórios operacionais graváveis.

## Preparar o gateway

```sh
sudo edge init
sudo edge gateway up
sudo edge gateway status
```

Padrões: `/srv/edge`, certificados em `/etc/edge/certificates`, rede `web_edge`, HTTP 80 e HTTPS 443. A imagem inicial é `nginx:1.27.5-alpine`; a referência pode ser escolhida em `init --image` ou atualizada explicitamente em `configure --image`. Mantenha essa imagem atualizada segundo a política do servidor.

A inicialização recusa sobrescrever um gateway ou cadastro sem configuração reconhecida. Use `--root /outro/diretorio` ou `EDGE_ROOT` para um estado independente. O nome Compose do gateway, rede e portas precisam ser exclusivos por instalação. `init` aceita `--network`, `--http-port`, `--https-port`, `--certificates-dir`, `--compose-name` e `--image`. `configure` altera portas, imagem ou rede, mas não move certificados nem muda a identidade Compose. Alterar a rede exige remover previamente os cadastros.

```sh
sudo edge configure --http-port 8080 --https-port 8443
```

Uma mudança de portas recria o gateway e pode interromper conexões brevemente. O redirecionamento HTTP e `X-Forwarded-Port` acompanham a porta HTTPS pública. As portas internas do Nginx permanecem 80/443.

## Certificados

```sh
sudo edge cert import site-cert --certificate /caminho/fullchain.pem --key /caminho/private.key
sudo edge cert list
sudo edge cert check site-cert
```

Importe PEM com a cadeia completa quando necessária e chave sem senha. A ferramenta verifica correspondência, validade e cobertura dos domínios cadastrados; aceita Cloudflare Origin sem exigir confiança pública da cadeia. Certificados Origin precisam do proxy Cloudflare para acesso confiável pelo navegador. A ferramenta não emite nem renova certificados automaticamente e não configura DNS.

A chave e os arquivos de estado recebem modo `0600`, os diretórios protegidos `0700`. Substituir um certificado em uso valida os domínios afetados e recarrega o gateway com rollback. Um certificado referenciado não pode ser removido.

## Cadastrar uma aplicação

O Compose da aplicação declara a rede externa e um alias exclusivo para seu serviço HTTP. Não publique as portas HTTP/HTTPS reservadas ao gateway.

```yaml
name: loja
services:
  web:
    image: minha-aplicacao:versao
    networks:
      edge:
        aliases: [loja-web]
networks:
  edge:
    external: true
    name: web_edge
```

O serviço deve responder a HTTP na porta interna cadastrada. Domínios devem apontar para o servidor ou para seu proxy externo. Configure a aplicação para confiar no proxy e interpretar `X-Forwarded-Proto` quando necessário.

```sh
sudo edge project add loja \
  --directory /srv/apps/loja \
  --compose compose.yaml \
  --env-file docker/.env \
  --service web \
  --route loja.example.com loja-web:8080 site-cert
sudo edge validate loja --configuration-only --require-gateway
# Construa/publique a imagem pelos mecanismos da aplicação antes de edge up.
sudo edge up loja
sudo edge validate loja
sudo edge route apply loja
```

`--service`, `--compose` e `--route` podem ser repetidos. Os arquivos Compose mantêm a ordem indicada. Na edição, informar qualquer dessas opções substitui a lista correspondente; `--env-file ''` remove o arquivo de ambiente. Alternativamente, use `edge wizard` ou `edge wizard loja` para editar.

O cadastro inicial não publica a rota. Editar um projeto cuja rota já foi publicada atualiza e valida a configuração imediatamente, exigindo gateway disponível. A aplicação do Nginx usa lock, teste e reload; falhas restauram os arquivos e, quando necessário, recarregam a configuração anterior. Cópias anteriores ficam em `<root>/backups`.

## Operação

```sh
sudo edge list
sudo edge list --json
sudo edge validate loja
sudo edge project show loja
sudo edge logs loja --follow
sudo edge stop loja
sudo edge up loja
sudo edge restart loja
sudo edge gateway logs --follow
sudo edge project remove loja
sudo edge cert remove site-cert
```

`list` consulta os containers reais, incluindo projetos Compose sem cadastro e containers parados. Projetos descobertos precisam ser cadastrados para serem gerenciados. `validate` distingue configuração válida, execução/health dos serviços, disponibilidade do gateway e resposta HTTP do upstream. O teste HTTP consulta `/` com o Host cadastrado e considera falhas HTTP como indisponibilidade.

`up` usa imagens existentes e inicia somente os serviços cadastrados, sem build e sem dependências implícitas. Cadastre suas dependências explicitamente. Serviços com profiles, como migrations e manutenção, não podem ser gerenciados. `stop` preserva volumes e rede. Remover um cadastro retira suas rotas e preserva containers; pare a aplicação separadamente quando desejar.

Para scripts, use `--non-interactive` nas operações que pedem confirmação. As consultas aceitam `--json`. `validate` e `gateway status` retornam código 1 quando o estado não atende à validação. `--configuration-only` permite preflight antes do primeiro start; acrescente `--require-gateway` para exigir o gateway pronto e `--directory` para conferir o checkout associado.

O Edge não executa deploy, build, migrations, seeds, DNS, SSH ou emissão de certificados. Arquivos de ambiente são apenas referenciados e o Compose resolvido não é impresso. Logs transmitidos pertencem aos serviços; a aplicação deve evitar registrar segredos.

## Testes

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q edge_tool bin/edge install.py
sh -n install.sh
# Opt-in: cria containers edge-it-* e portas altas, limpando os recursos ao final.
python3 tests/integration.py
```

A integração usa duas aplicações genéricas e verifica HTTPS, redirecionamento, ciclo dos projetos, exclusão de manutenção, rejeição real pelo Nginx, mudança de porta e remoção de rotas. Não usa dados de aplicações existentes.

## Licença

Este projeto é disponibilizado sob a licença MIT. Consulte o arquivo [LICENSE](LICENSE).

Copyright (c) 2026 Vinicius da Conceição Teixeira. As dependências e imagens de terceiros permanecem sob suas respectivas licenças.
