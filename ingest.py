"""
Ingestion Telegram — première brique du système de veille.

Ce que fait ce programme, à chaque réveil (toutes les 5 min via GitHub) :
  1. il lit la liste des chaînes actives dans la table 'sources' ;
  2. il se connecte à Telegram avec le compte userbot ;
  3. pour chaque chaîne, il récupère les messages récents ;
  4. il les enregistre dans la table 'messages' (sans créer de doublons).

Aucune intelligence artificielle ici : c'est purement déterministe.
La déduplication et la rédaction viendront dans les briques suivantes.
"""

import os
import re
import hashlib
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession
from supabase import create_client

# --- Secrets : lus depuis l'environnement, fournis par GitHub (jamais en clair ici) ---
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELEGRAM_SESSION"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# Nombre de messages récents qu'on regarde par chaîne à chaque passage.
# 30 est large pour un intervalle de 5 minutes ; on pourra l'ajuster.
MESSAGES_PAR_CHAINE = 30

# Connexion à la base de données
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def normaliser(texte: str) -> str:
    """Nettoie le texte pour pouvoir comparer deux messages identiques."""
    texte = texte.lower()
    texte = re.sub(r"http\S+", "", texte)   # enlève les liens
    texte = re.sub(r"\s+", " ", texte)       # réduit les espaces multiples
    return texte.strip()


def empreinte(texte: str) -> str:
    """Calcule l'empreinte unique du texte (servira au filtre anti-doublon)."""
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()


async def main():
    # 1. Récupérer les chaînes actives depuis la table 'sources'
    sources = (
        supabase.table("sources")
        .select("id, identifiant")
        .eq("type", "telegram")
        .eq("actif", True)
        .execute()
        .data
    )

    if not sources:
        print("Aucune source Telegram active. Rien à faire.")
        return

    # 2. Se connecter à Telegram avec le compte userbot (session déjà autorisée)
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()

    total = 0

    # 3. Parcourir chaque chaîne
    for source in sources:
        source_id = source["id"]
        canal = source["identifiant"]
        try:
            lignes = []
            async for msg in client.iter_messages(canal, limit=MESSAGES_PAR_CHAINE):
                if not msg.text:
                    continue  # on ignore les messages sans texte (image seule, etc.)

                texte_normalise = normaliser(msg.text)
                lignes.append({
                    "source_id": source_id,
                    "externe_id": str(msg.id),
                    "contenu": msg.text,
                    "hash": empreinte(texte_normalise),
                    "est_forward": msg.forward is not None,
                    "poste_le": msg.date.isoformat(),
                })

            # 4. Enregistrer : les nouveaux sont insérés, les déjà-connus ignorés
            if lignes:
                supabase.table("messages").upsert(
                    lignes,
                    on_conflict="source_id,externe_id",
                    ignore_duplicates=True,
                ).execute()
                total += len(lignes)
                print(f"{canal} : {len(lignes)} messages traités")

        except Exception as e:
            # Une chaîne en erreur ne doit pas bloquer les autres
            print(f"Erreur sur {canal} : {e}")

    await client.disconnect()
    print(f"Terminé. {total} messages traités au total.")


if __name__ == "__main__":
    asyncio.run(main())
