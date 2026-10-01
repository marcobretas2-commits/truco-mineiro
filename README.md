# Truco Mineiro Online

MVP completo em Python + FastAPI + WebSocket.

## Executar no Windows

```powershell
cd C:\Users\Administrador\ProjetosIA
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

Abra:
http://127.0.0.1:8000

Para testar online na mesma rede, descubra o IP do computador com `ipconfig` e abra:
http://SEU-IP:8000

## Regras implementadas no MVP

- 4 jogadores (2 duplas)
- Truco Mineiro com manilhas fixas:
  4 de Paus, 7 de Copas, Ás de Espadas, 7 de Ouros
- 3 cartas por jogador
- Rodadas de 3 vazas
- Pontuação de mão: 2, 4, 6, 8, 10, 12
- Pedido de Truco / 6 / 10 / 12
- Aceitar ou correr
- Bot para preencher lugares vazios
- Salas com código
- Convite por link
- WebSocket para atualização em tempo real
- Cartas e mesa desenhadas no navegador via CSS/SVG, sem depender de imagens externas

Esta é a primeira versão jogável. O código está separado para depois acrescentarmos login, banco de dados, ranking, sons, animações e geração de arte personalizada.
