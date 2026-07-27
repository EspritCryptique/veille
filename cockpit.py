"""
Cockpit Telegram — ton interface de validation.

À chaque passage :
  1. envoie les brouillons en attente sous forme de cartes avec boutons ;
  2. lit tes clics et tes réponses, puis agit :
       ✅ Publier  -> publie sur ta chaîne
       ❌ Rejeter  -> écarte le brouillon
       ⏸️ Différer -> le représente plus tard
       (répondre au message = corriger le texte du brouillon)

100 % déterministe : aucun appel LLM, donc aucun coût.
"""

import os
import requests
from datetime import datetime, timedelta, timezone

from supabase import create_client

# --- Secrets (fournis par GitHub) ---
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]        # toi : où arrivent les cartes
CHANNEL = os.environ["TELEGRAM_CHANNEL"]        # ta chaîne : où sont publiés les posts
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# --- Réglages ajustables ---
CARTES_PAR_PASSAGE = 5        # nb de cartes envoyées par passage (évite le spam)
HEURES_AVANT_RAPPEL = 6       # un brouillon différé revient après ce délai
AGE_MAX_HEURES = 24           # au-delà, un brouillon non traité est périmé

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def telegram(methode, **params):
    """Appelle l'API Telegram et renvoie la réponse."""
    r = requests.post(f"{API}/{methode}", json=params, timeout=30)
    return r.json()


def maintenant():
    return datetime.now(timezone.utc)


def clavier(draft_id):
    """Les trois boutons affichés sous chaque carte."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Publier", "callback_data": f"ok:{draft_id}"},
            {"text": "❌ Rejeter", "callback_data": f"no:{draft_id}"},
            {"text": "⏸️ Différer", "callback_data": f"wait:{draft_id}"},
        ]]
    }


def lire_offset():
    """Reprend la lecture des clics là où on s'était arrêté."""
    res = supabase.table("etat").select("valeur").eq("cle", "offset").execute().data
    return int(res[0]["valeur"]) if res else 0


def ecrire_offset(valeur):
    supabase.table("etat").upsert({"cle": "offset", "valeur": str(valeur)}).execute()


# ---------------------------------------------------------------- 1. ENVOI

def envoyer_cartes():
    """Envoie les brouillons en attente qui n'ont pas encore de carte."""
    # Les brouillons différés depuis assez longtemps repassent en attente
    limite = (maintenant() - timedelta(hours=HEURES_AVANT_RAPPEL)).isoformat()
    supabase.table("drafts").update(
        {"statut": "en_attente", "message_id": None}
    ).eq("statut", "differe").lt("maj_le", limite).execute()

    # Les brouillons trop vieux jamais envoyés sont périmés : on ne les propose pas
    perime = (maintenant() - timedelta(hours=AGE_MAX_HEURES)).isoformat()
    supabase.table("drafts").update({"statut": "perime"}).eq(
        "statut", "en_attente"
    ).filter("message_id", "is", "null").lt("cree_le", perime).execute()

    drafts = (
        supabase.table("drafts")
        .select("id, contenu")
        .eq("statut", "en_attente")
        .filter("message_id", "is", "null")
        .order("cree_le", desc=True)   # les plus récentes d'abord
        .limit(CARTES_PAR_PASSAGE)
        .execute()
        .data
    )

    envoyees = 0
    for d in drafts:
        texte = f"{d['contenu']}\n\n— Réponds à ce message pour corriger le texte."
        rep = telegram(
            "sendMessage",
            chat_id=CHAT_ID,
            text=texte,
            reply_markup=clavier(d["id"]),
        )
        if rep.get("ok"):
            supabase.table("drafts").update(
                {"message_id": rep["result"]["message_id"]}
            ).eq("id", d["id"]).execute()
            envoyees += 1
        else:
            print(f"  Envoi impossible : {rep}")
    return envoyees


# ------------------------------------------------------- 2. LECTURE DES CLICS

def publier(draft):
    """Publie le brouillon sur la chaîne, puis enregistre la publication."""
    rep = telegram("sendMessage", chat_id=CHANNEL, text=draft["contenu"])
    if not rep.get("ok"):
        return False, rep
    supabase.table("drafts").update(
        {"statut": "publie", "maj_le": maintenant().isoformat()}
    ).eq("id", draft["id"]).execute()
    supabase.table("publications").insert({
        "draft_id": draft["id"],
        "reseau": "telegram",
        "post_externe_id": str(rep["result"]["message_id"]),
    }).execute()
    return True, rep


def traiter_clic(cb):
    """Traite un appui sur un bouton."""
    action, draft_id = cb["data"].split(":", 1)
    message_id = cb["message"]["message_id"]

    res = supabase.table("drafts").select("id, contenu, statut").eq("id", draft_id).execute().data
    if not res:
        telegram("answerCallbackQuery", callback_query_id=cb["id"], text="Brouillon introuvable.")
        return
    draft = res[0]

    if draft["statut"] not in ("en_attente",):
        telegram("answerCallbackQuery", callback_query_id=cb["id"], text="Déjà traité.")
        return

    if action == "ok":
        ok, rep = publier(draft)
        if ok:
            entete, note = "✅ Publié", "Publié sur la chaîne."
        else:
            entete, note = "⚠️ Échec", f"Publication impossible : {rep.get('description', '')}"
    elif action == "no":
        supabase.table("drafts").update(
            {"statut": "rejete", "maj_le": maintenant().isoformat()}
        ).eq("id", draft_id).execute()
        entete, note = "❌ Rejeté", "Écarté."
    else:  # wait
        supabase.table("drafts").update(
            {"statut": "differe", "maj_le": maintenant().isoformat()}
        ).eq("id", draft_id).execute()
        entete, note = "⏸️ Différé", f"Reviendra dans {HEURES_AVANT_RAPPEL} h."

    # On met à jour la carte : plus de boutons, et le résultat affiché
    telegram(
        "editMessageText",
        chat_id=cb["message"]["chat"]["id"],
        message_id=message_id,
        text=f"{entete}\n\n{draft['contenu']}",
    )
    telegram("answerCallbackQuery", callback_query_id=cb["id"], text=note)


def traiter_reponse(msg):
    """Traite une réponse à une carte : remplace le texte du brouillon."""
    original = msg["reply_to_message"]["message_id"]
    nouveau_texte = (msg.get("text") or "").strip()
    if not nouveau_texte:
        return

    res = (
        supabase.table("drafts")
        .select("id")
        .eq("message_id", original)
        .eq("statut", "en_attente")
        .execute()
        .data
    )
    if not res:
        return
    draft_id = res[0]["id"]

    supabase.table("drafts").update(
        {"contenu": nouveau_texte, "maj_le": maintenant().isoformat()}
    ).eq("id", draft_id).execute()

    telegram(
        "editMessageText",
        chat_id=msg["chat"]["id"],
        message_id=original,
        text=f"✏️ Corrigé\n\n{nouveau_texte}\n\n— Réponds à ce message pour corriger le texte.",
        reply_markup=clavier(draft_id),
    )


def lire_actions():
    """Récupère les clics et réponses depuis le dernier passage."""
    offset = lire_offset()
    rep = telegram("getUpdates", offset=offset, timeout=0, limit=50)
    if not rep.get("ok"):
        print(f"  Lecture impossible : {rep}")
        return 0

    updates = rep["result"]
    traites = 0
    for u in updates:
        try:
            if "callback_query" in u:
                traiter_clic(u["callback_query"])
                traites += 1
            elif "message" in u and "reply_to_message" in u["message"]:
                traiter_reponse(u["message"])
                traites += 1
        except Exception as e:
            print(f"  Action ignorée : {e}")

    if updates:
        ecrire_offset(updates[-1]["update_id"] + 1)
    return traites


def main():
    traites = lire_actions()     # d'abord tes actions en attente
    envoyees = envoyer_cartes()  # puis les nouvelles cartes
    print(f"Terminé. {traites} actions traitées, {envoyees} cartes envoyées.")


if __name__ == "__main__":
    main()
