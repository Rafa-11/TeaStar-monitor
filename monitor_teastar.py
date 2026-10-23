"""
Monitor de guerras da aliança TeaStar no DomiStats.

Avisa no Telegram/Discord quando:
  - uma NOVA GUERRA aparece no histórico da aliança;
  - o ranking da aliança muda;
  - os selos "Quick XP" / "Rolled-back XP" aparecem ou somem.

Uso (dentro da pasta do projeto):
    pip install requests beautifulsoup4
    python monitor_teastar.py            # verifica uma vez
    python monitor_teastar.py --loop 30  # verifica a cada 30 minutos
    python monitor_teastar.py --teste    # envia a guerra mais recente como teste

Notificações (opcionais, use uma ou as duas):
    DISCORD_WEBHOOK     -> URL do webhook do Discord
    TELEGRAM_BOT_TOKEN  -> token do bot (criado com o @BotFather)
    TELEGRAM_CHAT_ID    -> ID do chat que vai receber as mensagens
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# Evita erro ao imprimir nomes coreanos/chineses/emojis no terminal do Windows
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------- Configuração ----------------
ALLIANCE_NAME = "TeaStar"
WARS_URL = "https://www.domistats.com/alliance_wars/964048"

NOTIFY_RANK_CHANGES = True    # avisar quando o ranking mudar
NOTIFY_BADGE_CHANGES = True   # avisar quando Quick XP / Rolled-back XP mudarem

STATE_FILE = Path(os.environ.get("STATE_FILE", "teastar_wars_state.json"))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TeaStarMonitor/2.0)"}
TIMEOUT = 15
MAX_SEEN = 300       # quantas guerras lembrar
MAX_MESSAGES = 5     # máximo de avisos de guerra por verificação
# -----------------------------------------------


# ---------- Leitura da página ----------

def fetch(url):
    """Baixa uma página, com 3 tentativas."""
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as error:
            last_error = error
            time.sleep(2 * (attempt + 1))
    raise last_error


def clean(element):
    """Texto de um elemento, com espaços normalizados."""
    if element is None:
        return ""
    return " ".join(element.get_text(" ", strip=True).split())


def to_int(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def parse_side(side):
    """Lê um dos lados (esquerdo ou direito) de um cartão de guerra."""
    box = side.find(class_=["war-winner", "war-loser"])
    result = None
    if box is not None:
        result = "winner" if "war-winner" in box.get("class", []) else "loser"

    link = side.find("a", href=True)
    match = re.search(r"/(\d+)$", link["href"]) if link else None

    rankings = [
        clean(e)
        for e in side.select(".war-alliance-ranking")
        if "league-ranking" not in e.get("class", [])
    ]
    return {
        "name": clean(side.select_one(".war-alliance-name")),
        "id": match.group(1) if match else "",
        "result": result,
        "glory": clean(side.select_one(".war-alliance-glory")),
        "league": clean(side.select_one(".league-ranking")),
        "rank": rankings[0] if rankings else "",
    }


def parse_page(html):
    """Retorna (lista de guerras, total de guerras, status da aliança)."""
    soup = BeautifulSoup(html, "html.parser")

    # A página repete a lista dentro de um bloco "VIP" borrado.
    # Usamos só o primeiro #wars_view, que é a lista real.
    container = soup.find(id="wars_view")
    wars = []
    if container is not None:
        for w in container.select("div.war"):
            left = w.select_one(".war-opponent-left")
            right = w.select_one(".war-opponent-right")
            if left is None or right is None:
                continue
            a, b = parse_side(left), parse_side(right)
            diff = to_int(clean(w.select_one(".war-glory-diff")))
            wars.append({
                # A página não traz um ID da guerra, então montamos um:
                # oponente + glory da aliança após a guerra + diferença.
                "key": f"{b['id']}|{a['glory']}|{diff}",
                "left": a,
                "right": b,
                "diff": diff,
                "when": clean(w.select_one(".war-rel-time")),
                "stamps": [clean(s) for s in w.select(".stamp")],
                "mismatch": w.get("data-ismismatch", "").lower() == "true",
            })

    total = None
    info = soup.select_one(".pagination-page-info")
    if info is not None:
        bolds = info.find_all("b")
        if bolds:
            total = to_int(clean(bolds[-1]))

    status = {"rank": "", "glory": "", "quick_xp": False, "rolled_back": False}
    card = soup.select_one(".info-card")
    if card is not None:
        names = card.select(".alliance-name")
        status["glory"] = clean(names[1]) if len(names) > 1 else ""
        status["rank"] = clean(card.select_one(".ranking-inline"))
        status["quick_xp"] = card.select_one(".quick-xp") is not None
        status["rolled_back"] = card.select_one(".rolled-back") is not None

    return wars, total, status


# ---------- Mensagens ----------

def describe_war(war):
    a, b = war["left"], war["right"]
    verb = {"winner": "venceu", "loser": "perdeu para"}.get(a["result"], "enfrentou")

    lines = [f"Nova guerra: {ALLIANCE_NAME} {verb} {b['name']}"]
    if a["name"] and a["name"] != ALLIANCE_NAME:
        lines.append(f"(nome na época: {a['name']})")
    if war["diff"] is not None:
        lines.append(f"Diferença de glory: +{war['diff']}")
    lines.append(f"{ALLIANCE_NAME}: {a['league']} | glory {a['glory']} | {a['rank']}")
    lines.append(f"Oponente: {b['league']} | glory {b['glory']} | {b['rank']}")
    if war["when"]:
        lines.append(f"Quando: {war['when']}")
    for stamp in war["stamps"]:
        lines.append(f"Selo: {stamp}")
    if war["mismatch"]:
        lines.append("Atenção: guerra marcada como desigual (mismatch)")
    lines.append(WARS_URL)
    return "\n".join(lines)


def describe_status_changes(old, new):
    changes = []
    if NOTIFY_RANK_CHANGES and old.get("rank") != new["rank"] and new["rank"]:
        changes.append(f"Ranking de {ALLIANCE_NAME}: {old.get('rank') or '-'} -> {new['rank']}")
    if NOTIFY_BADGE_CHANGES:
        for field, label in (("quick_xp", "Quick XP"), ("rolled_back", "Rolled-back XP")):
            if old.get(field) != new[field]:
                state = "apareceu" if new[field] else "sumiu"
                changes.append(f"Selo {label} {state} em {ALLIANCE_NAME}")
    return changes


# ---------- Notificações ----------

def notify(message):
    print(message)
    print("-" * 40)

    # Discord (webhook)
    webhook = os.environ.get("DISCORD_WEBHOOK")
    if webhook:
        try:
            resp = requests.post(webhook, json={"content": message[:1900]}, timeout=10)
            if resp.status_code >= 300:
                print(f"(Discord recusou a mensagem: {resp.status_code} {resp.text[:200]})")
        except requests.RequestException as error:
            print(f"(falha ao enviar para o Discord: {error})")

    # Telegram (bot)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": message[:4000]},
                timeout=10,
            )
            if resp.status_code != 200:
                print(f"(Telegram recusou a mensagem: {resp.status_code} {resp.text[:200]})")
        except requests.RequestException as error:
            print(f"(falha ao enviar para o Telegram: {error})")
    elif not webhook:
        print("(nenhuma notificação configurada: defina as variáveis do Telegram ou do Discord)")


# ---------- Estado ----------

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def save_state(state):
    """Só grava se algo mudou (ignora o horário), para não gerar alterações à toa."""
    old = load_state()
    if old is not None:
        a = {k: v for k, v in old.items() if k != "checked_at"}
        b = {k: v for k, v in state.items() if k != "checked_at"}
        if a == b:
            return
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- Verificação ----------

def check_once():
    now = datetime.now().isoformat(timespec="seconds")

    try:
        html = fetch(WARS_URL)
    except requests.RequestException as error:
        print(f"[{now}] Erro ao acessar o site: {error}")
        return

    wars, total, status = parse_page(html)
    if not wars:
        print(f"[{now}] Não consegui ler nenhuma guerra. O site pode ter mudado de layout "
              f"ou bloqueado o acesso. Nada foi alterado.")
        return

    previous = load_state()
    current_keys = [w["key"] for w in wars]

    # Primeira execução: só guarda o estado atual
    if previous is None:
        notify(f"Monitor iniciado para {ALLIANCE_NAME}. "
               f"Guerras no histórico: {total or len(wars)}.\n\nÚltima guerra:\n"
               + describe_war(wars[0]))
        save_state({"seen": current_keys, "total": total, "status": status, "checked_at": now})
        return

    seen = set(previous.get("seen", []))
    new_wars = [w for w in wars if w["key"] not in seen]

    # Se o total não aumentou, foi só uma correção de dados antigos
    prev_total = previous.get("total")
    if new_wars and total is not None and prev_total is not None and total <= prev_total:
        print(f"[{now}] Dados de guerras antigas foram ajustados no site (sem guerra nova).")
        new_wars = []

    for war in reversed(new_wars[:MAX_MESSAGES]):
        notify(describe_war(war))
    if len(new_wars) > MAX_MESSAGES:
        notify(f"... e mais {len(new_wars) - MAX_MESSAGES} guerras novas. Veja: {WARS_URL}")

    changes = describe_status_changes(previous.get("status", {}), status)
    if changes:
        notify("\n".join(changes) + f"\n{WARS_URL}")

    if not new_wars and not changes:
        print(f"[{now}] Sem novidades. Guerras no histórico: {total or len(wars)}.")

    older = [k for k in previous.get("seen", []) if k not in current_keys]
    save_state({
        "seen": (current_keys + older)[:MAX_SEEN],
        "total": total if total is not None else prev_total,
        "status": status,
        "checked_at": now,
    })


def send_test():
    html = fetch(WARS_URL)
    wars, total, status = parse_page(html)
    if not wars:
        print("Não consegui ler nenhuma guerra da página.")
        return
    notify("TESTE do monitor\n" + describe_war(wars[0]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, metavar="MIN",
                        help="repete a verificação a cada MIN minutos")
    parser.add_argument("--teste", action="store_true",
                        help="envia a guerra mais recente como teste de notificação")
    args = parser.parse_args()

    if args.teste:
        try:
            send_test()
        except requests.RequestException as error:
            print(f"Erro ao acessar o site: {error}")
        return

    if args.loop:
        try:
            while True:
                check_once()
                time.sleep(args.loop * 60)
        except KeyboardInterrupt:
            print("Monitor encerrado.")
    else:
        check_once()


if __name__ == "__main__":
    main()
