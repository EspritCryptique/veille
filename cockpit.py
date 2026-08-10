"""
Cockpit Telegram — ta console de gestion des news.

À chaque passage :
  1. envoie les brouillons en attente sous forme de cartes enrichies
     (catégorie, ancienneté, sources, liens d'origine) ;
  2. lit tes clics et tes réponses, puis agit :
       ✅ Publier     -> publie sur ta chaîne
       ✏️ Reformuler  -> redemande un texte au modèle (seul bouton payant)
       ❌ Rejeter     -> écarte le brouillon
       ⏸️ Différer    -> le représente plus tard
       (répondre au message = corriger le texte à la main)

Les cartes sont envoyées de la plus récente à la plus ancienne.
Tout est déterministe sauf "Reformuler", qui appelle le modèle.
"""

import os
import requests
from datetime import datetime, timedelta, timezone

from supabase import create_client

# On réutilise la fonction de rédaction (et donc TA charte) de redige.py
from redige import rediger

# --- Secrets (fournis par GitHub) ---
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]        # toi : où arrivent les cartes
CHANNEL = os.environ["TELEGRAM_CHANNEL"]        # ta chaîne : où sont publiés les posts
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# --- Réglages ajustables ---
CARTES_PAR_PASSAGE = 5        # nb de cartes envoyées par passage
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


def anciennete(date_iso):
    """Transforme une date en 'il y a 4 min' / 'il y a 3 h'."""
    if not date_iso:
        return ""
    quand = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
    minutes = int((maintenant() - quand).total_seconds() // 60)
    if minutes < 60:
        return f"il y a {max(minutes, 0)} min"
    if minutes < 1440:
        return f"il y a {minutes // 60} h"
    return f"il y a {minutes // 1440} j"


def clavier(draft_id):
    """Les boutons affichés sous chaque carte."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Publier", "callback_data": f"ok:{draft_id}"},
                {"text": "✏️ Reformuler", "callback_data": f"redo:{draft_id}"},
            ],
            [
                {"text": "❌ Rejeter", "callback_data": f"no:{draft_id}"},
                {"text": "⏸️ Différer", "callback_data": f"wait:{draft_id}"},
            ],
        ]
    }


def contexte_cluster(cluster_id):
    """Rassemble tout ce qui aide à décider : contexte, sources, liens."""
    infos = {"entete": "", "sources": "", "liens": [], "faits": "", "nb_sources": 0}

    cl = (
        supabase.table("clusters")
        .select("categorie, premier_vu_le")
        .eq("id", cluster_id)
        .execute()
        .data
    )
    msgs = (
        supabase.table("messages")
        .select("contenu, source_id, externe_id, poste_le")
        .eq("cluster_id", cluster_id)
        .limit(10)
        .execute()
        .data
    )
    srcs = supabase.table("sources").select("id, identifiant").execute().data
    noms = {s["id"]: s["identifiant"] for s in srcs}

    # En-tête : catégorie · ancienneté
    morceaux = []
    if cl:
        if cl[0].get("categorie"):
            morceaux.append(cl[0]["categorie"])
        morceaux.append(anciennete(cl[0].get("premier_vu_le")))
    infos["entete"] = " · ".join(m for m in morceaux if m)

    # Sources distinctes + liens vers les messages d'origine
    vues, liens = [], []
    for m in msgs:
        nom = noms.get(m.get("source_id"))
        if nom and nom not in vues:
            vues.append(nom)
        if nom and m.get("externe_id"):
            liens.append(f"https://t.me/{nom.lstrip('@')}/{m['externe_id']}")
    infos["nb_sources"] = len(vues)
    if vues:
        infos["sources"] = f"📎 {len(vues)} source(s) : " + ", ".join(vues)
    infos["liens"] = liens[:3]

    infos["faits"] = "\n\n".join((m["contenu"] or "")[:500] for m in msgs[:5] if m["contenu"])
    return infos


def construire_carte(draft, entete_resultat=""):
    """Assemble le texte complet de la carte."""
    ctx = contexte_cluster(draft["cluster_id"])
    lignes = []
    if entete_resultat:
        lignes.append(entete_resultat)
    if ctx["entete"]:
        lignes.append(f"📊 {ctx['entete']}")
    lignes.append("")
    lignes.append(draft["contenu"])
    lignes.append("")
    if ctx["sources"]:
        lignes.append(ctx["sources"])
    for lien in ctx["liens"]:
        lignes.append(f"🔗 {lien}")
    if not entete_resultat:
        lignes.append("")
        lignes.append("— Réponds à ce message pour corriger le texte.")
    return "\n".join(lignes)


def lire_offset():
    res = supabase.table("etat").select("valeur").eq("cle", "offset").execute().data
    return int(res[0]["valeur"]) if res else 0


def ecrire_offset(valeur):
    supabase.table("etat").upsert({"cle": "offset", "valeur": str(valeur)}).execute()


# ---------------------------------------------------------------- 1. ENVOI

def envoyer_cartes():
    """Envoie les brouillons en attente, les plus récents d'abord."""
    limite = (maintenant() - timedelta(hours=HEURES_AVANT_RAPPEL)).isoformat()
    supabase.table("drafts").update(
        {"statut": "en_attente", "message_id": None}
    ).eq("statut", "differe").lt("maj_le", limite).execute()

    perime = (maintenant() - timedelta(hours=AGE_MAX_HEURES)).isoformat()
    supabase.table("drafts").update({"statut": "perime"}).eq(
        "statut", "en_attente"
    ).filter("message_id", "is", "null").lt("cree_le", perime).execute()

    candidats = (
        supabase.table("drafts")
        .select("id, contenu, cluster_id")
        .eq("statut", "en_attente")
        .filter("message_id", "is", "null")
        .order("cree_le", desc=True)
        .limit(30)
        .execute()
        .data
    )
    if not candidats:
        return 0

    envoyees = 0
    for d in candidats[:CARTES_PAR_PASSAGE]:
        rep = telegram(
            "sendMessage",
            chat_id=CHAT_ID,
            text=construire_carte(d),
            reply_markup=clavier(d["id"]),
            disable_web_page_preview=True,
        )
        if rep.get("ok"):
            supabase.table("drafts").update({
                "message_id": rep["result"]["message_id"],
                "nb_sources": contexte_cluster(d["cluster_id"])["nb_sources"],
            }).eq("id", d["id"]).execute()
            envoyees += 1
        else:
            print(f"  Envoi impossible : {rep}")
    return envoyees


def rafraichir_cartes():
    """Met à jour les cartes déjà envoyées quand une source s'ajoute au dossier.

    Le dossier est "vivant" : si une nouvelle chaîne relaie la même actualité,
    la carte se met à jour toute seule. On ne touche PAS au texte du post,
    pour ne pas écraser tes corrections : utilise "Reformuler" pour cela.
    """
    envoyes = (
        supabase.table("drafts")
        .select("id, contenu, cluster_id, message_id, nb_sources")
        .eq("statut", "en_attente")
        .filter("message_id", "not.is", "null")
        .limit(30)
        .execute()
        .data
    )

    majs = 0
    for d in envoyes:
        actuel = contexte_cluster(d["cluster_id"])["nb_sources"]
        if actuel == (d.get("nb_sources") or 0):
            continue  # rien de neuf : on ne modifie pas le message
        rep = telegram(
            "editMessageText",
            chat_id=CHAT_ID,
            message_id=d["message_id"],
            text=construire_carte(d),
            reply_markup=clavier(d["id"]),
            disable_web_page_preview=True,
        )
        if rep.get("ok"):
            supabase.table("drafts").update(
                {"nb_sources": actuel}
            ).eq("id", d["id"]).execute()
            majs += 1
    return majs


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
    chat = cb["message"]["chat"]["id"]

    res = (
        supabase.table("drafts")
        .select("id, contenu, statut, cluster_id")
        .eq("id", draft_id)
        .execute()
        .data
    )
    if not res:
        telegram("answerCallbackQuery", callback_query_id=cb["id"], text="Brouillon introuvable.")
        return
    draft = res[0]

    if draft["statut"] != "en_attente":
        telegram("answerCallbackQuery", callback_query_id=cb["id"], text="Déjà traité.")
        return

    # --- Reformuler : on garde la carte active, on remplace juste le texte ---
    if action == "redo":
        ctx = contexte_cluster(draft["cluster_id"])
        try:
            nouveau = rediger(ctx["faits"])
        except Exception as e:
            telegram("answerCallbackQuery", callback_query_id=cb["id"],
                     text="Reformulation indisponible, réessaie.")
            print(f"  Reformulation échouée : {e}")
            return
        supabase.table("drafts").update(
            {"contenu": nouveau, "maj_le": maintenant().isoformat()}
        ).eq("id", draft_id).execute()
        draft["contenu"] = nouveau
        telegram(
            "editMessageText",
            chat_id=chat,
            message_id=message_id,
            text=construire_carte(draft),
            reply_markup=clavier(draft_id),
            disable_web_page_preview=True,
        )
        telegram("answerCallbackQuery", callback_query_id=cb["id"], text="Reformulé.")
        return

    # --- Les trois actions qui closent la carte ---
    if action == "ok":
        ok, rep = publier(draft)
        entete = "✅ Publié" if ok else f"⚠️ Échec : {rep.get('description', '')}"
        note = "Publié sur la chaîne." if ok else "Publication impossible."
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

    telegram(
        "editMessageText",
        chat_id=chat,
        message_id=message_id,
        text=construire_carte(draft, entete_resultat=entete),
        disable_web_page_preview=True,
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
        .select("id, cluster_id")
        .eq("message_id", original)
        .eq("statut", "en_attente")
        .execute()
        .data
    )
    if not res:
        return
    draft = {"id": res[0]["id"], "cluster_id": res[0]["cluster_id"], "contenu": nouveau_texte}

    supabase.table("drafts").update(
        {"contenu": nouveau_texte, "maj_le": maintenant().isoformat()}
    ).eq("id", draft["id"]).execute()

    telegram(
        "editMessageText",
        chat_id=msg["chat"]["id"],
        message_id=original,
        text=construire_carte(draft, entete_resultat="✏️ Corrigé"),
        reply_markup=clavier(draft["id"]),
        disable_web_page_preview=True,
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
    traites = lire_actions()       # d'abord tes actions
    majs = rafraichir_cartes()     # puis les dossiers qui se sont enrichis
    envoyees = envoyer_cartes()    # enfin les nouvelles cartes
    print(f"Terminé. {traites} actions traitées, {majs} cartes mises à jour, "
          f"{envoyees} cartes envoyées.")


if __name__ == "__main__":
    main()
